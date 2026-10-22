"""Phase 4A: local-first, permission-scoped unified knowledge with auditable decisions.

This module is deliberately independent of Notion, vector stores and model APIs.
Authorization contexts MUST be supplied by a trusted supervisor/broker; an
untrusted worker must never have direct write access to the SQLite database.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import sqlite3
import uuid
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KnowledgeError(Exception):
    """Base error for the unified knowledge core."""


class KnowledgeAccessDenied(KnowledgeError):
    """Principal is not allowed to perform the requested operation."""


class KnowledgeConflict(KnowledgeError):
    """Stale revision or duplicate knowledge reference."""


class KnowledgeTransitionError(KnowledgeError):
    """Invalid knowledge lifecycle transition."""


class KnowledgeKind:
    THOUGHT = "thought"
    HYPOTHESIS = "hypothesis"
    QUESTION = "question"
    OBSERVATION = "observation"
    DECISION = "decision"
    ARCHITECTURE_DECISION = "architecture_decision"
    SOLUTION_PATTERN = "solution_pattern"
    ERROR_PATTERN = "error_pattern"
    USER_PREFERENCE = "user_preference"
    LESSON = "lesson"
    ALL = frozenset((
        THOUGHT, HYPOTHESIS, QUESTION, OBSERVATION, DECISION,
        ARCHITECTURE_DECISION, SOLUTION_PATTERN, ERROR_PATTERN,
        USER_PREFERENCE, LESSON,
    ))


class KnowledgeState:
    CAPTURED = "captured"
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"

    TRANSITIONS = {
        CAPTURED: frozenset((PROPOSED, REJECTED, ARCHIVED)),
        PROPOSED: frozenset((ACCEPTED, REJECTED, ARCHIVED)),
        ACCEPTED: frozenset((SUPERSEDED,)),
        REJECTED: frozenset(),
        SUPERSEDED: frozenset(),
        ARCHIVED: frozenset(),
    }


class KnowledgeNature:
    """Epistemological nature of knowledge statements."""
    SPECULATIVE_DRAFT = "speculative_draft"
    OBSERVATION = "observation"
    VALIDATED_FINDING = "validated_finding"
    DECISION_RECORD = "decision_record"
    ALL = frozenset((
        SPECULATIVE_DRAFT,
        OBSERVATION,
        VALIDATED_FINDING,
        DECISION_RECORD,
    ))


class KnowledgeEvidenceLevel:
    """Degree of empirical or physical verification supporting a statement."""
    UNVERIFIED = "unverified"
    HUMAN_REVIEWED = "human_reviewed"
    ORACLE_VERIFIED = "oracle_verified"
    ALL = frozenset((
        UNVERIFIED,
        HUMAN_REVIEWED,
        ORACLE_VERIFIED,
    ))


class KnowledgeVisibility:
    PRIVATE_HUMAN = "private_human"
    PROJECT_SHARED = "project_shared"
    GLOBAL_AUTHORIZED = "global_authorized"
    ALL = frozenset((PRIVATE_HUMAN, PROJECT_SHARED, GLOBAL_AUTHORIZED))


@dataclass(frozen=True)
class KnowledgePrincipal:
    """Permission snapshot issued by a *trusted* coordinator boundary, not a worker.

    The dataclass is a transport contract, NOT cryptographic authentication.
    """
    principal_id: str
    role: str
    authorized_projects: frozenset[str] = field(default_factory=frozenset)
    can_read_global: bool = False
    can_publish_global: bool = False

    def __post_init__(self) -> None:
        if not self.principal_id.strip() or self.role not in ("human", "agent", "supervisor"):
            raise ValueError("Identidade ou papel de principal invalido")
        object.__setattr__(self, "authorized_projects", frozenset(self.authorized_projects))


class UnifiedKnowledgeCore:
    """Versioned local knowledge store; SQLite remains the source of truth.

    All read paths apply visibility/project authorization in SQL BEFORE ranking.
    AI-generated text is never automatically marked as an accepted decision.
    """

    MAX_CONTENT = 10000
    MAX_TAGS = 30

    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self._init_schema()

    def _init_schema(self) -> None:
        in_tx = self.db.in_transaction
        table_statements = (
            """CREATE TABLE IF NOT EXISTS knowledge_entries (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_id TEXT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                author_id TEXT NOT NULL,
                author_role TEXT NOT NULL,
                visibility TEXT NOT NULL,
                state TEXT NOT NULL,
                nature TEXT NOT NULL DEFAULT 'speculative_draft',
                evidence_level TEXT NOT NULL DEFAULT 'unverified',
                revision INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL,
                source_id TEXT,
                tags_json TEXT NOT NULL,
                context_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                superseded_by TEXT,
                FOREIGN KEY (superseded_by) REFERENCES knowledge_entries(id),
                UNIQUE (project_id, source_kind, source_id)
            )""",
            """CREATE TABLE IF NOT EXISTS knowledge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL,
                entry_revision INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                reason TEXT,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (entry_id) REFERENCES knowledge_entries(id)
            )""",
            """CREATE TABLE IF NOT EXISTS knowledge_links (
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                PRIMARY KEY (source_id, target_id, relation),
                FOREIGN KEY (source_id) REFERENCES knowledge_entries(id),
                FOREIGN KEY (target_id) REFERENCES knowledge_entries(id)
            )""",
        )
        for stmt in table_statements:
            self.db.execute(stmt)

        columns = {row[1] for row in self.db.execute("PRAGMA table_info(knowledge_entries)").fetchall()}
        if "nature" not in columns:
            self.db.execute("ALTER TABLE knowledge_entries ADD COLUMN nature TEXT NOT NULL DEFAULT 'speculative_draft'")
        if "evidence_level" not in columns:
            self.db.execute("ALTER TABLE knowledge_entries ADD COLUMN evidence_level TEXT NOT NULL DEFAULT 'unverified'")

        aux_statements = (
            "CREATE INDEX IF NOT EXISTS idx_knowledge_scope ON knowledge_entries(project_id, visibility, state)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_nature_evidence ON knowledge_entries(project_id, nature, evidence_level)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_events ON knowledge_events(entry_id, entry_revision)",
            """CREATE TRIGGER IF NOT EXISTS knowledge_events_no_update
               BEFORE UPDATE ON knowledge_events BEGIN SELECT RAISE(ABORT, 'knowledge events append-only'); END""",
            """CREATE TRIGGER IF NOT EXISTS knowledge_events_no_delete
               BEFORE DELETE ON knowledge_events BEGIN SELECT RAISE(ABORT, 'knowledge events append-only'); END""",
        )
        for stmt in aux_statements:
            self.db.execute(stmt)

        if not in_tx:
            self.db.commit()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Nested-safe transaction without committing the caller's outer transaction."""
        name = "knowledge_" + uuid.uuid4().hex
        nested = self.db.in_transaction
        if nested:
            self.db.execute("SAVEPOINT " + name)
        else:
            self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            if nested:
                self.db.execute("RELEASE SAVEPOINT " + name)
            else:
                self.db.commit()
        except BaseException:
            if nested:
                self.db.execute("ROLLBACK TO SAVEPOINT " + name)
                self.db.execute("RELEASE SAVEPOINT " + name)
            else:
                self.db.rollback()
            raise

    @staticmethod
    def _validate_text(text: str, field_name: str, max_length: int = 10000) -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > max_length:
            raise ValueError(f"{field_name} precisa ser texto nao vazio de ate {max_length} caracteres")
        return text.strip()

    @staticmethod
    def _json_object(value: Optional[Mapping[str, Any]], field_name: str) -> str:
        if value is None:
            return "{}"
        if not isinstance(value, Mapping):
            raise ValueError(f"{field_name} deve ser objeto")
        result = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)
        if len(result) > 12000:
            raise ValueError(f"{field_name} excedeu o limite")
        return result

    @staticmethod
    def _can_read_sql(principal: KnowledgePrincipal) -> Tuple[str, List[Any]]:
        parts = []
        params: List[Any] = []
        if principal.role == "human":
            parts.append("(visibility = ? AND author_id = ? AND author_role = 'human')")
            params.extend((KnowledgeVisibility.PRIVATE_HUMAN, principal.principal_id))
        if principal.authorized_projects:
            placeholders = ",".join("?" for _ in principal.authorized_projects)
            parts.append(f"(visibility = ? AND project_id IN ({placeholders}))")
            params.append(KnowledgeVisibility.PROJECT_SHARED)
            params.extend(sorted(principal.authorized_projects))
        if principal.can_read_global:
            parts.append("(visibility = ?)")
            params.append(KnowledgeVisibility.GLOBAL_AUTHORIZED)
        if not parts:
            return "0", []
        return "(" + " OR ".join(parts) + ")", params

    @staticmethod
    def _can_write(principal: KnowledgePrincipal, project_id: str, visibility: str) -> bool:
        if visibility == KnowledgeVisibility.PRIVATE_HUMAN:
            return principal.role == "human" and project_id in principal.authorized_projects
        if visibility == KnowledgeVisibility.PROJECT_SHARED:
            return project_id in principal.authorized_projects
        if visibility == KnowledgeVisibility.GLOBAL_AUTHORIZED:
            return principal.role in ("human", "supervisor") and principal.can_publish_global
        return False

    @staticmethod
    def _decode(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        for name in ("tags_json", "context_json", "evidence_json"):
            result[name.removesuffix("_json")] = json.loads(result.pop(name))
        return result

    def get(self, principal: KnowledgePrincipal, entry_id: str) -> Optional[Dict[str, Any]]:
        clause, params = self._can_read_sql(principal)
        row = self.db.execute(
            f"SELECT * FROM knowledge_entries WHERE id = ? AND {clause}",
            [entry_id] + params,
        ).fetchone()
        return self._decode(row) if row is not None else None

    def capture(
        self,
        principal: KnowledgePrincipal,
        project_id: str,
        kind: str,
        content: str,
        *,
        task_id: Optional[str] = None,
        visibility: str = KnowledgeVisibility.PRIVATE_HUMAN,
        nature: Optional[str] = None,
        evidence_level: Optional[str] = None,
        source_kind: str = "manual",
        source_id: Optional[str] = None,
        tags: Sequence[str] = (),
        context: Optional[Mapping[str, Any]] = None,
        evidence: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._validate_text(project_id, "project_id", 500)
        self._validate_text(content, "content", self.MAX_CONTENT)
        self._validate_text(source_kind, "source_kind", 80)
        if kind not in KnowledgeKind.ALL:
            raise ValueError("Tipo de conhecimento invalido")
        if visibility not in KnowledgeVisibility.ALL or not self._can_write(principal, project_id, visibility):
            raise KnowledgeAccessDenied("Principal sem autorizacao para gravar neste escopo")

        if nature is None:
            if kind in (KnowledgeKind.DECISION, KnowledgeKind.ARCHITECTURE_DECISION, KnowledgeKind.USER_PREFERENCE):
                nature = KnowledgeNature.DECISION_RECORD
            elif kind == KnowledgeKind.OBSERVATION:
                nature = KnowledgeNature.OBSERVATION
            else:
                nature = KnowledgeNature.SPECULATIVE_DRAFT
        elif nature not in KnowledgeNature.ALL:
            raise ValueError("Natureza epistemologica invalida")

        if evidence_level is None:
            evidence_level = KnowledgeEvidenceLevel.UNVERIFIED
        elif evidence_level not in KnowledgeEvidenceLevel.ALL:
            raise ValueError("Grau de evidencia invalido")

        if evidence_level == KnowledgeEvidenceLevel.ORACLE_VERIFIED and principal.role != "supervisor":
            raise KnowledgeAccessDenied("Apenas o supervisor/oraculo pode emitir ou validar comprovacoes oracle_verified")
        if nature == KnowledgeNature.VALIDATED_FINDING and principal.role != "supervisor":
            raise KnowledgeAccessDenied("Apenas o supervisor/oraculo pode certificar validated_finding")

        clean_tags = sorted({self._validate_text(t, "tag", 80).casefold() for t in tags})
        if len(clean_tags) > self.MAX_TAGS:
            raise ValueError("Quantidade excessiva de tags")
        stamp = _now()
        entry_id = uuid.uuid4().hex
        with self._transaction():
            try:
                self.db.execute(
                    """INSERT INTO knowledge_entries (
                        id, project_id, task_id, kind, content, author_id, author_role,
                        visibility, state, nature, evidence_level, revision, source_kind, source_id,
                        tags_json, context_json, evidence_json, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                    (
                        entry_id, project_id, task_id, kind, content.strip(),
                        principal.principal_id, principal.role, visibility,
                        KnowledgeState.CAPTURED, nature, evidence_level, source_kind, source_id,
                        json.dumps(clean_tags), self._json_object(context, "context"),
                        self._json_object(evidence, "evidence"), stamp, stamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KnowledgeConflict("Referencia de origem duplicada ou invalida") from exc
            self._event(entry_id, 1, "capture", principal.principal_id, None, KnowledgeState.CAPTURED, None)
        result = self.get(principal, entry_id)
        assert result is not None
        return result

    def _event(
        self, entry_id: str, revision: int, event_type: str, actor: str,
        old: Optional[str], new: str, reason: Optional[str],
    ) -> None:
        self.db.execute(
            """INSERT INTO knowledge_events
                (entry_id, entry_revision, event_type, actor_id, from_state, to_state, reason, recorded_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (entry_id, revision, event_type, actor, old, new, reason, _now()),
        )

    def transition(
        self,
        principal: KnowledgePrincipal,
        entry_id: str,
        *,
        expected_revision: int,
        new_state: str,
        reason: str,
        replacement_id: Optional[str] = None,
        new_nature: Optional[str] = None,
        new_evidence_level: Optional[str] = None,
    ) -> Dict[str, Any]:
        reason = self._validate_text(reason, "reason", 2000)
        with self._transaction():
            row = self.get(principal, entry_id)
            if row is None:
                raise KnowledgeAccessDenied("Registro ausente ou fora do escopo")
            if row["revision"] != expected_revision:
                raise KnowledgeConflict("Revisao obsoleta; releia a entrada")
            if new_state not in KnowledgeState.TRANSITIONS.get(row["state"], ()):
                raise KnowledgeTransitionError(f"Transicao invalida: {row['state']} -> {new_state}")
            if new_state in (KnowledgeState.ACCEPTED, KnowledgeState.REJECTED, KnowledgeState.SUPERSEDED):
                if principal.role != "human":
                    raise KnowledgeAccessDenied("Apenas o humano pode aprovar, rejeitar ou substituir uma decisao")
            if new_state == KnowledgeState.ACCEPTED and row["kind"] not in (
                KnowledgeKind.DECISION, KnowledgeKind.ARCHITECTURE_DECISION, KnowledgeKind.USER_PREFERENCE
            ):
                raise KnowledgeTransitionError("Somente decisoes e preferencias podem receber estado accepted")
            if new_state == KnowledgeState.SUPERSEDED:
                if row["kind"] not in (
                    KnowledgeKind.DECISION, KnowledgeKind.ARCHITECTURE_DECISION, KnowledgeKind.USER_PREFERENCE
                ):
                    raise KnowledgeTransitionError("Apenas decisoes aceitas podem ser substituidas")
                replacement = self.get(principal, replacement_id or "")
                if (replacement is None or replacement["id"] == entry_id
                        or replacement["project_id"] != row["project_id"]
                        or replacement["state"] != KnowledgeState.ACCEPTED):
                    raise KnowledgeTransitionError("Substituicao requer outra decisao aceita no mesmo projeto")
            elif replacement_id is not None:
                raise KnowledgeTransitionError("replacement_id so e permitido em superseded")

            target_nature = row.get("nature", KnowledgeNature.SPECULATIVE_DRAFT)
            target_evidence = row.get("evidence_level", KnowledgeEvidenceLevel.UNVERIFIED)

            if new_nature is not None:
                if new_nature not in KnowledgeNature.ALL:
                    raise ValueError("Natureza epistemologica invalida")
                if new_nature == KnowledgeNature.VALIDATED_FINDING and principal.role != "supervisor":
                    raise KnowledgeAccessDenied("Apenas o supervisor/oraculo pode certificar validated_finding")
                target_nature = new_nature
            elif new_state == KnowledgeState.ACCEPTED and target_nature == KnowledgeNature.SPECULATIVE_DRAFT:
                if row["kind"] in (KnowledgeKind.DECISION, KnowledgeKind.ARCHITECTURE_DECISION, KnowledgeKind.USER_PREFERENCE):
                    target_nature = KnowledgeNature.DECISION_RECORD

            if new_evidence_level is not None:
                if new_evidence_level not in KnowledgeEvidenceLevel.ALL:
                    raise ValueError("Grau de evidencia invalido")
                if new_evidence_level == KnowledgeEvidenceLevel.ORACLE_VERIFIED and principal.role != "supervisor":
                    raise KnowledgeAccessDenied("Apenas o supervisor/oraculo pode emitir ou validar comprovacoes oracle_verified")
                target_evidence = new_evidence_level
            elif new_state == KnowledgeState.ACCEPTED and target_evidence == KnowledgeEvidenceLevel.UNVERIFIED and principal.role == "human":
                target_evidence = KnowledgeEvidenceLevel.HUMAN_REVIEWED

            cur = self.db.execute(
                """UPDATE knowledge_entries
                   SET state = ?, nature = ?, evidence_level = ?, revision = revision + 1,
                       updated_at = ?, superseded_by = ?
                   WHERE id = ? AND revision = ? AND state = ?""",
                (new_state, target_nature, target_evidence, _now(), replacement_id, entry_id, expected_revision, row["state"]),
            )
            if cur.rowcount != 1:
                raise KnowledgeConflict("Conflito concorrente de revisao")
            self._event(entry_id, expected_revision + 1, "transition", principal.principal_id, row["state"], new_state, reason)
        result = self.get(principal, entry_id)
        assert result is not None
        return result

    def verify_with_oracle(
        self,
        principal: KnowledgePrincipal,
        entry_id: str,
        *,
        expected_revision: int,
        oracle_token: str,
        verification_evidence: Mapping[str, Any],
        nature: str = KnowledgeNature.VALIDATED_FINDING,
        reason: str = "Verificacao formal concluida pelo Oraculo",
    ) -> Dict[str, Any]:
        """Certify an entry as oracle_verified; strictly restricted to a supervisor principal.

        Requires an authentic oracle_token and verification_evidence mapping.
        """
        if principal.role != "supervisor":
            raise KnowledgeAccessDenied("Apenas o supervisor/oraculo pode certificar oracle_verified")
        if not isinstance(oracle_token, str) or len(oracle_token.strip()) < 16:
            raise ValueError("oracle_token invalido ou insuficiente")
        if not isinstance(verification_evidence, Mapping) or not verification_evidence:
            raise ValueError("verification_evidence obrigatorio para certificacao pelo Oraculo")
        if nature not in (KnowledgeNature.VALIDATED_FINDING, KnowledgeNature.DECISION_RECORD, KnowledgeNature.OBSERVATION):
            raise ValueError("Natureza epistemologica incompativel com verificacao do Oraculo")
        reason = self._validate_text(reason, "reason", 2000)

        with self._transaction():
            row = self.get(principal, entry_id)
            if row is None:
                raise KnowledgeAccessDenied("Registro ausente ou fora do escopo")
            if row["revision"] != expected_revision:
                raise KnowledgeConflict("Revisao obsoleta; releia a entrada")

            current_evidence = dict(row["evidence"])
            current_evidence["oracle_verification"] = {
                "token": oracle_token.strip(),
                "evidence": dict(verification_evidence),
                "verified_at": _now(),
                "verified_by": principal.principal_id,
            }
            cur = self.db.execute(
                """UPDATE knowledge_entries
                   SET nature = ?, evidence_level = ?, evidence_json = ?,
                       revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (
                    nature,
                    KnowledgeEvidenceLevel.ORACLE_VERIFIED,
                    self._json_object(current_evidence, "evidence"),
                    _now(),
                    entry_id,
                    expected_revision,
                ),
            )
            if cur.rowcount != 1:
                raise KnowledgeConflict("Conflito concorrente de revisao")
            self._event(
                entry_id,
                expected_revision + 1,
                "oracle_verify",
                principal.principal_id,
                row["state"],
                row["state"],
                reason,
            )
        result = self.get(principal, entry_id)
        assert result is not None
        return result

    def find(
        self,
        principal: KnowledgePrincipal,
        *,
        query: str = "",
        project_id: Optional[str] = None,
        kinds: Sequence[str] = (),
        states: Sequence[str] = (),
        natures: Sequence[str] = (),
        evidence_levels: Sequence[str] = (),
        context: Optional[Mapping[str, Any]] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Filter access in SQL before lexical scoring and context compatibility.

        Context mismatches are excluded, not treated as reusable precedents.
        Missing context does not imply compatibility; returned records are marked
        'context_unconfirmed' when not all caller constraints were covered.
        """
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit deve estar entre 1 e 100")
        if any(k not in KnowledgeKind.ALL for k in kinds):
            raise ValueError("kind invalido")
        if any(s not in KnowledgeState.TRANSITIONS for s in states):
            raise ValueError("state invalido")
        if any(n not in KnowledgeNature.ALL for n in natures):
            raise ValueError("nature invalida")
        if any(e not in KnowledgeEvidenceLevel.ALL for e in evidence_levels):
            raise ValueError("evidence_level invalido")
        clause, params = self._can_read_sql(principal)
        clauses = [clause]
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            params.extend(kinds)
        if states:
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            params.extend(states)
        if natures:
            clauses.append("nature IN (" + ",".join("?" for _ in natures) + ")")
            params.extend(natures)
        if evidence_levels:
            clauses.append("evidence_level IN (" + ",".join("?" for _ in evidence_levels) + ")")
            params.extend(evidence_levels)
        sql = "SELECT * FROM knowledge_entries WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC, id LIMIT 1000"
        rows = self.db.execute(sql, params).fetchall()
        needle = set(re.findall(r"\w+", query.casefold(), flags=re.UNICODE))
        context = context or {}
        results: List[Dict[str, Any]] = []
        for raw in rows:
            rec = self._decode(raw)
            saved_ctx = rec["context"]
            if any(k in saved_ctx and saved_ctx[k] != v for k, v in context.items()):
                continue
            haystack = set(re.findall(r"\w+", (rec["content"] + " " + " ".join(rec["tags"])).casefold(), flags=re.UNICODE))
            overlap = needle & haystack
            if needle and not overlap:
                continue
            rec["context_match"] = "confirmed" if context and all(k in saved_ctx for k in context) else "unconfirmed"
            rec["relevance_score"] = len(overlap) / max(len(needle), 1)
            if rec["state"] == KnowledgeState.SUPERSEDED:
                rec["relevance_score"] *= 0.25
            results.append(rec)
        results.sort(key=lambda r: (-r["relevance_score"], r["created_at"], r["id"]))
        return results[:limit]

    def events(self, principal: KnowledgePrincipal, entry_id: str) -> List[Dict[str, Any]]:
        if self.get(principal, entry_id) is None:
            raise KnowledgeAccessDenied("Registro ausente ou fora do escopo")
        return [
            dict(r) for r in self.db.execute(
                "SELECT * FROM knowledge_events WHERE entry_id = ? ORDER BY id ASC", (entry_id,)
            ).fetchall()
        ]

    def link(
        self, principal: KnowledgePrincipal, source_id: str, target_id: str, relation: str,
    ) -> None:
        relation = self._validate_text(relation, "relation", 80)
        if source_id == target_id:
            raise ValueError("Nao e permitido vincular uma entrada a ela mesma")
        with self._transaction():
            source, target = self.get(principal, source_id), self.get(principal, target_id)
            if source is None or target is None:
                raise KnowledgeAccessDenied("Nao foi autorizado acesso a ambas as entradas")
            if source["project_id"] != target["project_id"]:
                raise KnowledgeAccessDenied("Vinculos entre projetos requerem contrato explicito futuro")
            if principal.role != "human":
                raise KnowledgeAccessDenied("Vinculos entre conhecimentos exigem triagem humana na 4A")
            self.db.execute(
                """INSERT OR IGNORE INTO knowledge_links
                    (source_id, target_id, relation, created_at, actor_id) VALUES (?,?,?,?,?)""",
                (source_id, target_id, relation, _now(), principal.principal_id),
            )

    def links(self, principal: KnowledgePrincipal, entry_id: str) -> List[Dict[str, Any]]:
        if self.get(principal, entry_id) is None:
            raise KnowledgeAccessDenied("Registro ausente ou fora do escopo")
        rows = self.db.execute(
            "SELECT * FROM knowledge_links WHERE source_id = ? OR target_id = ? ORDER BY created_at",
            (entry_id, entry_id),
        ).fetchall()
        return [
            dict(r) for r in rows
            if self.get(principal, r["source_id"]) is not None
            and self.get(principal, r["target_id"]) is not None
        ]
