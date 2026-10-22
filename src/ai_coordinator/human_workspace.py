"""Human-AI Cognitive Workspace - Human Inbox, private thought capture, and audited event logs.

Phase 4B.1 implementation:
- Single canonical SQLite store with WAL mode.
- Strict owner_id privacy fencing; project_id is optional and does not widen access.
- Separation of item lifecycle (captured, triaged, converted, discarded) from read state (unread, read).
- Append-only event auditing (human_inbox_events) with SQLite triggers preventing UPDATE/DELETE.
- Worker process lockout: workers are strictly denied access to private human storage.
- Session authentication bound via supervisor HMAC tokens.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _scrub(text: str) -> str:
    if not isinstance(text, str):
        return text
    text = re.sub(r"(?i)\b(?:sk-[a-z0-9_-]{20,}|AIza[a-z0-9_-]{30,}|gh[pousr]_[a-z0-9]{30,})\b", "[REDACTED]", text)
    return text


class HumanWorkspaceError(Exception):
    """Base error for human workspace operations."""


class HumanWorkspaceAccessDenied(HumanWorkspaceError):
    """Raised when an operation violates owner authorization or worker boundary."""


class HumanWorkspaceConflict(HumanWorkspaceError):
    """Raised when concurrent modification revision conflicts occur."""


class HumanWorkspaceNotFoundError(HumanWorkspaceError):
    """Raised when a requested inbox record does not exist or is out of scope."""


class HumanInboxState:
    CAPTURED = "captured"
    TRIAGED = "triaged"
    CONVERTED = "converted"
    DISCARDED = "discarded"
    ALL = (CAPTURED, TRIAGED, CONVERTED, DISCARDED)

    VALID_TRANSITIONS = {
        CAPTURED: {TRIAGED, CONVERTED, DISCARDED},
        TRIAGED: {CONVERTED, DISCARDED, CAPTURED},
        CONVERTED: {DISCARDED},
        DISCARDED: {CAPTURED, TRIAGED},
    }


class HumanInboxReadState:
    UNREAD = "unread"
    READ = "read"
    ALL = (UNREAD, READ)


class HumanInboxItemKind:
    THOUGHT = "thought"
    IDEA = "idea"
    TODO = "todo"
    NOTE = "note"
    FEEDBACK = "feedback"
    ALL = (THOUGHT, IDEA, TODO, NOTE, FEEDBACK)


@dataclass(frozen=True)
class HumanPrincipal:
    """Authenticated identity for human workspace operations."""
    owner_id: str
    role: str = "human"
    session_token: Optional[str] = None

    def __post_init__(self):
        if not self.owner_id or not isinstance(self.owner_id, str):
            raise ValueError("owner_id obrigatorio para HumanPrincipal")
        cleaned = self.owner_id.strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", cleaned):
            raise ValueError("owner_id contem caracteres invalidos (use letras, numeros, '.', '-' ou '_')")
        object.__setattr__(self, "owner_id", cleaned)


class HumanWorkspace:
    """Private Human Inbox and cognitive workspace manager backed by SQLite WAL."""

    def __init__(
        self,
        db: sqlite3.Connection,
        supervisor_secret: Optional[bytes] = None,
        supervisor_key: Optional[str] = None,
    ):
        self.db = db
        self.supervisor_secret = supervisor_secret or secrets.token_bytes(32)
        self.supervisor_key = str(supervisor_key).strip() if supervisor_key else None
        self._init_schema()

    def _init_schema(self) -> None:
        """Create inbox tables, indexes, and append-only audit triggers."""
        with self._transaction():
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS human_inbox (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    project_id TEXT,
                    kind TEXT NOT NULL DEFAULT 'thought',
                    content TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'captured',
                    read_state TEXT NOT NULL DEFAULT 'unread',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    converted_task_id TEXT
                )"""
            )
            existing_cols = {r[1] for r in self.db.execute("PRAGMA table_info(human_inbox)").fetchall()}
            if "converted_task_id" not in existing_cols:
                try:
                    self.db.execute("ALTER TABLE human_inbox ADD COLUMN converted_task_id TEXT")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

            self.db.execute(
                """CREATE INDEX IF NOT EXISTS idx_human_inbox_owner
                   ON human_inbox(owner_id, state, created_at DESC)"""
            )
            self.db.execute(
                """CREATE INDEX IF NOT EXISTS idx_human_inbox_owner_proj
                   ON human_inbox(owner_id, project_id)"""
            )
            self.db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_human_inbox_converted_task
                   ON human_inbox(converted_task_id)
                   WHERE converted_task_id IS NOT NULL"""
            )

            self.db.execute(
                """CREATE TABLE IF NOT EXISTS human_inbox_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inbox_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    old_state TEXT,
                    new_state TEXT,
                    old_read_state TEXT,
                    new_read_state TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )"""
            )
            self.db.execute(
                """CREATE INDEX IF NOT EXISTS idx_human_inbox_events_inbox
                   ON human_inbox_events(inbox_id, id ASC)"""
            )

            # Strict append-only triggers: prevent UPDATE and DELETE on audit log
            self.db.execute(
                """CREATE TRIGGER IF NOT EXISTS trg_prevent_update_human_inbox_events
                   BEFORE UPDATE ON human_inbox_events
                   BEGIN
                       SELECT RAISE(ABORT, 'human_inbox_events e estritamente append-only; atualizacao proibida');
                   END;"""
            )
            self.db.execute(
                """CREATE TRIGGER IF NOT EXISTS trg_prevent_delete_human_inbox_events
                   BEFORE DELETE ON human_inbox_events
                   BEGIN
                       SELECT RAISE(ABORT, 'human_inbox_events e estritamente append-only; exclusao proibida');
                   END;"""
            )

    @contextlib.contextmanager
    def _transaction(self):
        cursor = self.db.cursor()
        is_nested = self.db.in_transaction
        if is_nested:
            sp_name = f"sp_{secrets.token_hex(8)}"
            cursor.execute(f"SAVEPOINT {sp_name}")
            try:
                yield cursor
                cursor.execute(f"RELEASE SAVEPOINT {sp_name}")
            except Exception:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                cursor.execute(f"RELEASE SAVEPOINT {sp_name}")
                raise
            finally:
                cursor.close()
        else:
            cursor.execute("BEGIN IMMEDIATE")
            try:
                yield cursor
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            finally:
                cursor.close()

    def issue_session_token(self, owner_id: str, supervisor_key: str) -> str:
        """Issue an authentic HMAC session token binding the owner identity.
        Requires genuine supervisor authentication key.
        """
        if os.environ.get("AI_COORDINATOR_ROLE") == "worker":
            raise HumanWorkspaceAccessDenied("Workers subordinados estao proibidos de solicitar tokens de sessao")
        if not supervisor_key or not self.supervisor_key or not hmac.compare_digest(str(supervisor_key).strip(), self.supervisor_key):
            raise HumanWorkspaceAccessDenied("Emissao de token negada: chave de supervisor ausente ou invalida")
        if not owner_id or not isinstance(owner_id, str):
            raise ValueError("owner_id obrigatorio")
        cleaned = owner_id.strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", cleaned):
            raise ValueError("owner_id invalido")
        digest = hmac.new(self.supervisor_secret, f"human_session:{cleaned}".encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hsess_{cleaned}_{digest}"

    def verify_principal(self, principal: HumanPrincipal, require_session_token: bool = True) -> None:
        """Enforce strict caller authorization, worker lockout, and cryptographic token validity."""
        if not isinstance(principal, HumanPrincipal):
            raise HumanWorkspaceAccessDenied("Principal invalido para operacao do Human Workspace")

        # Worker fail-closed check: process environment
        if os.environ.get("AI_COORDINATOR_ROLE") == "worker":
            raise HumanWorkspaceAccessDenied("Workers subordinados estao estritamente proibidos de acessar o Human Workspace")

        # Worker role check
        if principal.role == "worker":
            raise HumanWorkspaceAccessDenied("Identidade com papel 'worker' nao possui acesso ao Human Workspace")

        if principal.role not in ("human", "supervisor"):
            raise HumanWorkspaceAccessDenied(f"Papel '{principal.role}' nao autorizado para Human Workspace")

        if require_session_token or principal.session_token is not None:
            if not principal.session_token:
                raise HumanWorkspaceAccessDenied("Token de sessao autenticado e obrigatorio para operacoes do Human Workspace")
            expected_prefix = f"hsess_{principal.owner_id}_"
            if not principal.session_token.startswith(expected_prefix):
                raise HumanWorkspaceAccessDenied("Token de sessao invalido: proprietario divergente")
            token_digest = principal.session_token[len(expected_prefix):]
            expected_digest = hmac.new(
                self.supervisor_secret,
                f"human_session:{principal.owner_id}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(token_digest, expected_digest):
                raise HumanWorkspaceAccessDenied("Token de sessao invalido ou falsificado")

    def _decode_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "owner_id": row["owner_id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "content": row["content"],
            "tags": json.loads(row["tags_json"]),
            "context": json.loads(row["context_json"]),
            "state": row["state"],
            "read_state": row["read_state"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "converted_task_id": row["converted_task_id"] if "converted_task_id" in row.keys() else None,
        }

    def _record_event(
        self,
        inbox_id: str,
        revision: int,
        action: str,
        actor_id: str,
        old_state: Optional[str] = None,
        new_state: Optional[str] = None,
        old_read_state: Optional[str] = None,
        new_read_state: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO human_inbox_events
               (inbox_id, revision, action, actor_id, old_state, new_state,
                old_read_state, new_read_state, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                inbox_id,
                revision,
                action,
                actor_id,
                old_state,
                new_state,
                old_read_state,
                new_read_state,
                json.dumps(dict(details or {}), ensure_ascii=False),
                _now(),
            ),
        )

    def capture(
        self,
        principal: HumanPrincipal,
        content: str,
        *,
        item_kind: str = HumanInboxItemKind.THOUGHT,
        project_id: Optional[str] = None,
        tags: Sequence[str] = (),
        context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Capture a new private thought, idea, or note into human inbox."""
        self.verify_principal(principal)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Conteudo obrigatorio para captura no Inbox")
        if len(content) > 10000:
            raise ValueError("Conteudo excede o limite de 10000 caracteres")
        if item_kind not in HumanInboxItemKind.ALL:
            raise ValueError(f"kind invalido: '{item_kind}'. Esperado um de {HumanInboxItemKind.ALL}")

        clean_content = _scrub(content.strip())
        clean_tags = sorted(list({str(t).strip().casefold() for t in tags if str(t).strip()}))
        clean_proj = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None
        item_id = f"inbox_{secrets.token_hex(8)}"
        stamp = _now()

        with self._transaction():
            self.db.execute(
                """INSERT INTO human_inbox
                   (id, owner_id, project_id, kind, content, tags_json, context_json,
                    state, read_state, revision, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    item_id,
                    principal.owner_id,
                    clean_proj,
                    item_kind,
                    clean_content,
                    json.dumps(clean_tags, ensure_ascii=False),
                    json.dumps(dict(context or {}), ensure_ascii=False),
                    HumanInboxState.CAPTURED,
                    HumanInboxReadState.UNREAD,
                    stamp,
                    stamp,
                ),
            )
            self._record_event(
                inbox_id=item_id,
                revision=1,
                action="capture",
                actor_id=principal.owner_id,
                new_state=HumanInboxState.CAPTURED,
                new_read_state=HumanInboxReadState.UNREAD,
                details={"kind": item_kind, "project_id": clean_proj, "tags": clean_tags},
            )

        item = self.get(principal, item_id)
        assert item is not None
        return item

    def get(self, principal: HumanPrincipal, item_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a private item by id strictly enforcing owner authorization."""
        self.verify_principal(principal)
        row = self.db.execute("SELECT * FROM human_inbox WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        # Strict privacy check: only the owner can read
        if row["owner_id"] != principal.owner_id:
            raise HumanWorkspaceAccessDenied("Acesso negado: o item pertence a outro proprietario")
        return self._decode_row(row)

    def list_inbox(
        self,
        principal: HumanPrincipal,
        *,
        project_id: Optional[str] = None,
        state: Optional[str] = None,
        read_state: Optional[str] = None,
        item_kind: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List inbox entries strictly restricted to the caller's owner_id."""
        self.verify_principal(principal)
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit deve estar entre 1 e 200")
        if state is not None and state not in HumanInboxState.ALL:
            raise ValueError(f"state invalido: '{state}'")
        if read_state is not None and read_state not in HumanInboxReadState.ALL:
            raise ValueError(f"read_state invalido: '{read_state}'")
        if item_kind is not None and item_kind not in HumanInboxItemKind.ALL:
            raise ValueError(f"item_kind invalido: '{item_kind}'")

        clauses = ["owner_id = ?"]
        params: List[Any] = [principal.owner_id]

        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id.strip())
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if read_state is not None:
            clauses.append("read_state = ?")
            params.append(read_state)
        if item_kind is not None:
            clauses.append("kind = ?")
            params.append(item_kind)

        sql = f"SELECT * FROM human_inbox WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, id LIMIT ?"
        params.append(limit)
        rows = self.db.execute(sql, params).fetchall()

        results = [self._decode_row(r) for r in rows]
        if tag is not None:
            norm_tag = tag.strip().casefold()
            results = [r for r in results if norm_tag in r["tags"]]
        return results

    def mark_read_state(
        self,
        principal: HumanPrincipal,
        item_id: str,
        *,
        read_state: str,
        expected_revision: int,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Update read status (unread -> read or read -> unread) independently of lifecycle."""
        self.verify_principal(principal)
        if read_state not in HumanInboxReadState.ALL:
            raise ValueError(f"read_state invalido: '{read_state}'")

        with self._transaction():
            current = self.get(principal, item_id)
            if current is None:
                raise HumanWorkspaceNotFoundError("Item nao encontrado no inbox do proprietario")
            if current["revision"] != expected_revision:
                raise HumanWorkspaceConflict("Revisao concorrente detectada; releia o item")

            stamp = _now()
            cur = self.db.execute(
                """UPDATE human_inbox
                   SET read_state = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND owner_id = ? AND revision = ?""",
                (read_state, stamp, item_id, principal.owner_id, expected_revision),
            )
            if cur.rowcount != 1:
                raise HumanWorkspaceConflict("Falha ao atualizar read_state por conflito concorrente")

            self._record_event(
                inbox_id=item_id,
                revision=expected_revision + 1,
                action="mark_read" if read_state == HumanInboxReadState.READ else "mark_unread",
                actor_id=principal.owner_id,
                old_state=current["state"],
                new_state=current["state"],
                old_read_state=current["read_state"],
                new_read_state=read_state,
                details={"reason": reason},
            )

        updated = self.get(principal, item_id)
        assert updated is not None
        return updated

    def transition_state(
        self,
        principal: HumanPrincipal,
        item_id: str,
        *,
        new_state: str,
        expected_revision: int,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Transition item lifecycle (captured, triaged, converted, discarded) with optimistic fencing."""
        self.verify_principal(principal)
        if new_state not in HumanInboxState.ALL:
            raise ValueError(f"new_state invalido: '{new_state}'")
        if new_state == HumanInboxState.CONVERTED:
            raise ValueError(
                "Transicao direta para 'converted' proibida; utilize convert_to_task() para conversao com tarefa vinculada"
            )

        with self._transaction():
            current = self.get(principal, item_id)
            if current is None:
                raise HumanWorkspaceNotFoundError("Item nao encontrado no inbox do proprietario")
            if current["revision"] != expected_revision:
                raise HumanWorkspaceConflict("Revisao concorrente detectada; releia o item")

            current_state = current["state"]
            if new_state != current_state:
                allowed = HumanInboxState.VALID_TRANSITIONS.get(current_state, set())
                if new_state not in allowed:
                    raise ValueError(f"Transicao de '{current_state}' para '{new_state}' nao permitida")

            stamp = _now()
            cur = self.db.execute(
                """UPDATE human_inbox
                   SET state = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND owner_id = ? AND revision = ?""",
                (new_state, stamp, item_id, principal.owner_id, expected_revision),
            )
            if cur.rowcount != 1:
                raise HumanWorkspaceConflict("Falha ao atualizar estado por conflito concorrente")

            self._record_event(
                inbox_id=item_id,
                revision=expected_revision + 1,
                action="transition",
                actor_id=principal.owner_id,
                old_state=current_state,
                new_state=new_state,
                old_read_state=current["read_state"],
                new_read_state=current["read_state"],
                details={"reason": reason},
            )

        updated = self.get(principal, item_id)
        assert updated is not None
        return updated

    def update_content(
        self,
        principal: HumanPrincipal,
        item_id: str,
        *,
        content: str,
        expected_revision: int,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Update content of an existing item with optimistic concurrency control."""
        self.verify_principal(principal)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Conteudo obrigatorio")
        if len(content) > 10000:
            raise ValueError("Conteudo excede limite de 10000 caracteres")

        clean_content = _scrub(content.strip())
        with self._transaction():
            current = self.get(principal, item_id)
            if current is None:
                raise HumanWorkspaceNotFoundError("Item nao encontrado no inbox do proprietario")
            if current["revision"] != expected_revision:
                raise HumanWorkspaceConflict("Revisao concorrente detectada; releia o item")

            stamp = _now()
            cur = self.db.execute(
                """UPDATE human_inbox
                   SET content = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND owner_id = ? AND revision = ?""",
                (clean_content, stamp, item_id, principal.owner_id, expected_revision),
            )
            if cur.rowcount != 1:
                raise HumanWorkspaceConflict("Falha ao atualizar conteudo por conflito concorrente")

            self._record_event(
                inbox_id=item_id,
                revision=expected_revision + 1,
                action="update_content",
                actor_id=principal.owner_id,
                old_state=current["state"],
                new_state=current["state"],
                old_read_state=current["read_state"],
                new_read_state=current["read_state"],
                details={"reason": reason},
            )

        updated = self.get(principal, item_id)
        assert updated is not None
        return updated

    def update_metadata(
        self,
        principal: HumanPrincipal,
        item_id: str,
        *,
        project_id: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        expected_revision: int,
        reason: str = "",
    ) -> Dict[str, Any]:
        """Update project association or tags of an existing item."""
        self.verify_principal(principal)

        with self._transaction():
            current = self.get(principal, item_id)
            if current is None:
                raise HumanWorkspaceNotFoundError("Item nao encontrado no inbox do proprietario")
            if current["revision"] != expected_revision:
                raise HumanWorkspaceConflict("Revisao concorrente detectada; releia o item")

            clean_proj = current["project_id"]
            if project_id is not None:
                clean_proj = project_id.strip() if project_id.strip() else None

            clean_tags = current["tags"]
            if tags is not None:
                clean_tags = sorted(list({str(t).strip().casefold() for t in tags if str(t).strip()}))

            stamp = _now()
            cur = self.db.execute(
                """UPDATE human_inbox
                   SET project_id = ?, tags_json = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND owner_id = ? AND revision = ?""",
                (
                    clean_proj,
                    json.dumps(clean_tags, ensure_ascii=False),
                    stamp,
                    item_id,
                    principal.owner_id,
                    expected_revision,
                ),
            )
            if cur.rowcount != 1:
                raise HumanWorkspaceConflict("Falha ao atualizar metadados por conflito concorrente")

            self._record_event(
                inbox_id=item_id,
                revision=expected_revision + 1,
                action="update_metadata",
                actor_id=principal.owner_id,
                old_state=current["state"],
                new_state=current["state"],
                old_read_state=current["read_state"],
                new_read_state=current["read_state"],
                details={"project_id": clean_proj, "tags": clean_tags, "reason": reason},
            )

        updated = self.get(principal, item_id)
        assert updated is not None
        return updated

    def events(self, principal: HumanPrincipal, item_id: str) -> List[Dict[str, Any]]:
        """Retrieve strictly audited event history for an owned inbox item."""
        self.verify_principal(principal)
        current = self.get(principal, item_id)
        if current is None:
            raise HumanWorkspaceNotFoundError("Item nao encontrado no inbox do proprietario")

        rows = self.db.execute(
            "SELECT * FROM human_inbox_events WHERE inbox_id = ? ORDER BY id ASC",
            (item_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "inbox_id": r["inbox_id"],
                "revision": r["revision"],
                "action": r["action"],
                "actor_id": r["actor_id"],
                "old_state": r["old_state"],
                "new_state": r["new_state"],
                "old_read_state": r["old_read_state"],
                "new_read_state": r["new_read_state"],
                "details": json.loads(r["details_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def convert_to_task(
        self,
        principal: HumanPrincipal,
        item_id: str,
        *,
        store: Any,
        project: Optional[Union[str, Path]] = None,
        goal: Optional[str] = None,
        expected_revision: Optional[int] = None,
        status: str = "backlog",
        flight_plan: Optional[Union[Dict[str, Any], Any]] = None,
        ai_memory_workstream_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Convert a private human inbox item into a backlog task atomically and idempotently.

        Enforces HumanPrincipal authentication, privacy fencing, optimistic revision checking,
        and append-only audit event recording.
        """
        self.verify_principal(principal)

        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("item_id obrigatorio para conversao em tarefa")

        if status != "backlog":
            raise ValueError("Tarefas convertidas a partir do Human Inbox devem obrigatoriamente possuir status 'backlog'")

        with self._transaction():
            current = self.get(principal, item_id)
            if current is None:
                raise HumanWorkspaceNotFoundError("Item nao encontrado no inbox do proprietario")

            if current["state"] == HumanInboxState.DISCARDED:
                raise HumanWorkspaceError("Item descartado nao pode ser convertido em tarefa")

            # Strict idempotency check: item already converted
            if current["state"] == HumanInboxState.CONVERTED and current.get("converted_task_id"):
                if expected_revision is not None and expected_revision not in (current["revision"], current["revision"] - 1):
                    raise HumanWorkspaceConflict("Revisao concorrente detectada; releia o item")
                existing_task = store.task(current["converted_task_id"])
                return {"task": existing_task, "inbox_item": current, "created": False}

            # Optimistic revision fencing for items not yet converted
            if expected_revision is not None and current["revision"] != expected_revision:
                raise HumanWorkspaceConflict("Revisao concorrente detectada; releia o item")

            target_proj = project or current.get("project_id")
            if not target_proj:
                raise ValueError("Diretorio do projeto obrigatorio para conversao em tarefa")

            task_goal = (goal.strip() if isinstance(goal, str) and goal.strip() else current["content"]).strip()
            workstream = ai_memory_workstream_id or current.get("context", {}).get("ai_memory_workstream_id")

            # Create task in Store in backlog status (not active) without immediate disk mirror
            task = store.new(
                project=target_proj,
                goal=task_goal,
                ai_memory_workstream_id=workstream,
                flight_plan=flight_plan,
                status=status,
                mirror=False,
            )
            task_id = task["id"]

            stamp = _now()
            cur = self.db.execute(
                """UPDATE human_inbox
                   SET state = ?, converted_task_id = ?, revision = revision + 1, updated_at = ?
                   WHERE id = ? AND owner_id = ? AND revision = ?""",
                (
                    HumanInboxState.CONVERTED,
                    task_id,
                    stamp,
                    item_id,
                    principal.owner_id,
                    current["revision"],
                ),
            )
            if cur.rowcount != 1:
                raise HumanWorkspaceConflict("Falha ao atualizar estado por conflito concorrente")

            self._record_event(
                inbox_id=item_id,
                revision=current["revision"] + 1,
                action="convert_to_task",
                actor_id=principal.owner_id,
                old_state=current["state"],
                new_state=HumanInboxState.CONVERTED,
                old_read_state=current["read_state"],
                new_read_state=current["read_state"],
                details={
                    "task_id": task_id,
                    "project": str(target_proj),
                    "status": status,
                },
            )

        # Safely mirror task to disk ONLY AFTER the SQLite transaction has committed
        store.mirror(task)

        updated_item = self.get(principal, item_id)
        assert updated_item is not None
        return {"task": task, "inbox_item": updated_item, "created": True}
