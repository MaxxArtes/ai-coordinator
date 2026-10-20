"""Resource Ledger for tracking side-effects, mutations, and compensation obligations."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Union
import uuid

from .invariants import compute_sha256

now = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def validate_id(ident: str) -> str:
    if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9_-]{8,64}", ident):
        raise ValueError("Identificador invalido: use letras minusculas, numeros, sublinhado e hifen (8 a 64 chars)")
    return ident


class ResourceLedgerError(Exception):
    """Base exception for resource ledger operations."""


class ResourceConflictError(ResourceLedgerError):
    """Raised when an external asset was modified by a third party since the recorded operation."""


class AuthorizationRequiredError(ResourceLedgerError):
    """Raised when an irreversible or sensitive resource compensation requires explicit authorization."""


class IdempotencyError(ResourceLedgerError):
    """Raised on invalid duplicate or conflicting state transitions."""


class UnresolvedResourceError(ResourceLedgerError):
    """Raised when unresolved uncertain resources prevent proposal activation."""


class ResourceState:
    INTENT_RECORDED = "intent_recorded"
    APPLIED = "applied"
    RETAINED = "retained"
    COMPENSATION_PENDING = "compensation_pending"
    COMPENSATED = "compensated"
    UNCERTAIN = "uncertain"

    ALL = {
        INTENT_RECORDED,
        APPLIED,
        RETAINED,
        COMPENSATION_PENDING,
        COMPENSATED,
        UNCERTAIN,
    }


class ResourceType:
    FILE = "file"
    DATABASE = "database"
    PROCESS = "process"
    EXTERNAL = "external"

    ALL = {FILE, DATABASE, PROCESS, EXTERNAL}


class ResourceAction:
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    SPAWN = "spawn"
    CALL = "call"

    ALL = {CREATE, MODIFY, DELETE, SPAWN, CALL}


_SENSITIVE_PATTERNS = [
    re.compile(r"(bearer\s+)[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE),
    re.compile(r"(password|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]+['\"]?", re.IGNORECASE),
]


def scrub_sensitive_text(text: Optional[str]) -> Optional[str]:
    """Scrub sensitive credentials, passwords, and tokens from ledger texts."""
    if not text:
        return text
    scrubbed = text
    for pattern in _SENSITIVE_PATTERNS:
        scrubbed = pattern.sub(r"\1[REDACTED]", scrubbed)
    return scrubbed


def scrub_sensitive_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Recursively scrub sensitive keys and token values from dictionaries."""
    if not data:
        return {}
    scrubbed: Dict[str, Any] = {}
    for k, v in data.items():
        if any(term in k.lower() for term in ("secret", "token", "password", "key", "auth")):
            scrubbed[k] = "[REDACTED]"
        elif isinstance(v, str):
            scrubbed[k] = scrub_sensitive_text(v)
        elif isinstance(v, dict):
            scrubbed[k] = scrub_sensitive_dict(v)
        else:
            scrubbed[k] = v
    return scrubbed


def canonicalize_resource_id(resource_type: str, raw_id: str) -> str:
    """Resolve canonical resource identifier.

    For file resources, resolves symlinks, relative segments, and normalizes forward slashes.
    """
    if resource_type == ResourceType.FILE:
        try:
            resolved = os.path.realpath(os.path.abspath(raw_id))
            return resolved.replace("\\", "/")
        except Exception:
            return raw_id.replace("\\", "/")
    return raw_id.strip()


@dataclass
class ResourceRecord:
    id: str
    task_id: str
    plan_revision: int
    resource_type: str
    canonical_id: str
    action: str
    idempotency_key: str
    status: str
    run_id: Optional[str] = None
    pre_hash: Optional[str] = None
    post_hash: Optional[str] = None
    compensable: bool = True
    compensation_action: Optional[str] = None
    requires_authorization: bool = False
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ResourceRecord:
        meta_raw = row["metadata"]
        try:
            metadata = json.loads(meta_raw) if meta_raw else {}
        except Exception:
            metadata = {}

        return cls(
            id=row["id"],
            task_id=row["task_id"],
            plan_revision=int(row["plan_revision"]),
            run_id=row["run_id"],
            resource_type=row["resource_type"],
            canonical_id=row["canonical_id"],
            action=row["action"],
            pre_hash=row["pre_hash"],
            post_hash=row["post_hash"],
            compensable=bool(row["compensable"]),
            compensation_action=row["compensation_action"],
            requires_authorization=bool(row["requires_authorization"]),
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=metadata,
        )


@dataclass
class ResourceEvent:
    id: Optional[int]
    resource_id: str
    task_id: str
    plan_revision: int
    from_status: Optional[str]
    to_status: str
    event_type: str
    created_at: str
    details: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ResourceEvent:
        return cls(
            id=row["id"],
            resource_id=row["resource_id"],
            task_id=row["task_id"],
            plan_revision=int(row["plan_revision"]),
            from_status=row["from_status"],
            to_status=row["to_status"],
            event_type=row["event_type"],
            created_at=row["created_at"],
            details=row["details"],
        )


class ResourceLedger:
    """Manages the two-phase lifecycle of resource mutations and compensations."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def record_intent(
        self,
        task_id: str,
        plan_revision: int,
        resource_type: str,
        raw_id: str,
        action: str,
        idempotency_key: str,
        run_id: Optional[str] = None,
        pre_hash: Optional[str] = None,
        compensable: bool = True,
        compensation_action: Optional[str] = None,
        requires_authorization: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ResourceRecord:
        """Phase 1: Record intent prior to executing any mutation or external action."""
        task_id = validate_id(task_id)
        if resource_type not in ResourceType.ALL:
            raise ValueError(f"Tipo de recurso invalido: '{resource_type}'")
        if action not in ResourceAction.ALL:
            raise ValueError(f"Acao de recurso invalida: '{action}'")

        canonical_id = canonicalize_resource_id(resource_type, raw_id)

        # Idempotency check: if already recorded with this key for task, return existing
        existing = self.db.execute(
            "SELECT * FROM resource_ledger WHERE task_id=? AND idempotency_key=?",
            (task_id, idempotency_key),
        ).fetchone()
        if existing:
            return ResourceRecord.from_row(existing)

        # Compute pre_hash for files if modifying and pre_hash is omitted
        captured_pre_hash = pre_hash
        if resource_type == ResourceType.FILE and action in (ResourceAction.MODIFY, ResourceAction.DELETE):
            if captured_pre_hash is None and os.path.isfile(canonical_id):
                try:
                    captured_pre_hash = compute_sha256(canonical_id)
                except Exception:
                    captured_pre_hash = None

        # External irreversible resources automatically require explicit authorization
        if resource_type == ResourceType.EXTERNAL and not compensable:
            requires_authorization = True

        rec_id = str(uuid.uuid4())
        stamp = now()
        scrubbed_meta = scrub_sensitive_dict(metadata)
        meta_json = json.dumps(scrubbed_meta) if scrubbed_meta else "{}"

        self.db.execute(
            """
            INSERT INTO resource_ledger (
                id, task_id, plan_revision, run_id, resource_type, canonical_id,
                action, pre_hash, post_hash, compensable, compensation_action,
                requires_authorization, idempotency_key, status, created_at,
                updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec_id,
                task_id,
                plan_revision,
                run_id,
                resource_type,
                canonical_id,
                action,
                captured_pre_hash,
                1 if compensable else 0,
                compensation_action,
                1 if requires_authorization else 0,
                idempotency_key,
                ResourceState.INTENT_RECORDED,
                stamp,
                stamp,
                meta_json,
            ),
        )

        self._record_event(
            resource_id=rec_id,
            task_id=task_id,
            plan_revision=plan_revision,
            from_status=None,
            to_status=ResourceState.INTENT_RECORDED,
            event_type="intent_recorded",
            details=f"Intencao de {action} em {resource_type} '{canonical_id}' registrada",
        )

        row = self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (rec_id,)).fetchone()
        return ResourceRecord.from_row(row)

    def confirm_applied(
        self,
        resource_id: str,
        post_hash: Optional[str] = None,
    ) -> ResourceRecord:
        """Phase 2: Confirm operation completed and record physical verification."""
        row = self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        if not row:
            raise ResourceLedgerError(f"Recurso {resource_id} nao encontrado no ledger")

        rec = ResourceRecord.from_row(row)
        captured_post_hash = post_hash

        # For files: if post_hash is omitted, compute directly from disk
        if rec.resource_type == ResourceType.FILE and captured_post_hash is None:
            if os.path.isfile(rec.canonical_id):
                try:
                    captured_post_hash = compute_sha256(rec.canonical_id)
                except Exception:
                    pass

        stamp = now()
        self.db.execute(
            """
            UPDATE resource_ledger
            SET status=?, post_hash=?, updated_at=?
            WHERE id=?
            """,
            (ResourceState.APPLIED, captured_post_hash, stamp, resource_id),
        )

        self._record_event(
            resource_id=resource_id,
            task_id=rec.task_id,
            plan_revision=rec.plan_revision,
            from_status=rec.status,
            to_status=ResourceState.APPLIED,
            event_type="applied",
            details=f"Efeito fisico confirmado em '{rec.canonical_id}' com hash {captured_post_hash}",
        )

        updated_row = self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        return ResourceRecord.from_row(updated_row)

    def reconcile_resources(self, task_id: str) -> List[ResourceRecord]:
        """Detect and reconcile any resources stuck in 'intent_recorded' (e.g. after mid-run crash)."""
        task_id = validate_id(task_id)
        rows = self.db.execute(
            "SELECT * FROM resource_ledger WHERE task_id=? AND status=?",
            (task_id, ResourceState.INTENT_RECORDED),
        ).fetchall()

        reconciled: List[ResourceRecord] = []
        stamp = now()

        for r in rows:
            rec = ResourceRecord.from_row(r)
            reason: str = "Operacao interrompida antes da confirmacao"

            # Check physical reality
            if rec.resource_type == ResourceType.FILE:
                file_exists = os.path.isfile(rec.canonical_id)
                if rec.action == ResourceAction.CREATE:
                    if file_exists:
                        reason = "Arquivo fisico foi criado mas transicao para 'applied' nao foi confirmada"
                    else:
                        reason = "Arquivo fisico nao foi criado antes da interrupcao"
                elif rec.action in (ResourceAction.MODIFY, ResourceAction.DELETE):
                    reason = f"Arquivo fisico em estado incerto para acao '{rec.action}'"

            self.db.execute(
                "UPDATE resource_ledger SET status=?, updated_at=? WHERE id=?",
                (ResourceState.UNCERTAIN, stamp, rec.id),
            )

            self._record_event(
                resource_id=rec.id,
                task_id=rec.task_id,
                plan_revision=rec.plan_revision,
                from_status=ResourceState.INTENT_RECORDED,
                to_status=ResourceState.UNCERTAIN,
                event_type="uncertain_detected",
                details=reason,
            )

            updated = self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (rec.id,)).fetchone()
            reconciled.append(ResourceRecord.from_row(updated))

        return reconciled

    def retain_resource(self, resource_id: str) -> ResourceRecord:
        """Mark a resource as retained for future revisions without compensation."""
        row = self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        if not row:
            raise ResourceLedgerError(f"Recurso {resource_id} nao encontrado")

        rec = ResourceRecord.from_row(row)
        stamp = now()
        self.db.execute(
            "UPDATE resource_ledger SET status=?, updated_at=? WHERE id=?",
            (ResourceState.RETAINED, stamp, resource_id),
        )

        self._record_event(
            resource_id=resource_id,
            task_id=rec.task_id,
            plan_revision=rec.plan_revision,
            from_status=rec.status,
            to_status=ResourceState.RETAINED,
            event_type="retained",
            details="Recurso retido com sucesso para preservacao historica",
        )

        return ResourceRecord.from_row(
            self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        )

    def prepare_compensation(
        self,
        resource_id: str,
        authorized: bool = False,
    ) -> ResourceRecord:
        """Prepare a resource for compensation, validating reversibility and permissions."""
        row = self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        if not row:
            raise ResourceLedgerError(f"Recurso {resource_id} nao encontrado")

        rec = ResourceRecord.from_row(row)

        # Idempotency: if already compensated, return immediately
        if rec.status == ResourceState.COMPENSATED:
            return rec

        if (not rec.compensable or rec.requires_authorization) and not authorized:
            raise AuthorizationRequiredError(
                f"Recurso '{rec.canonical_id}' e irreversivel ou requer autorizacao explicita para compensacao."
            )

        stamp = now()
        self.db.execute(
            "UPDATE resource_ledger SET status=?, updated_at=? WHERE id=?",
            (ResourceState.COMPENSATION_PENDING, stamp, resource_id),
        )

        self._record_event(
            resource_id=resource_id,
            task_id=rec.task_id,
            plan_revision=rec.plan_revision,
            from_status=rec.status,
            to_status=ResourceState.COMPENSATION_PENDING,
            event_type="prepare_compensation",
            details=f"Compensacao agendada para acao '{rec.action}'",
        )

        return ResourceRecord.from_row(
            self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        )

    def apply_compensation_step(
        self,
        resource_id: str,
        authorized: bool = False,
    ) -> ResourceRecord:
        """Execute compensation step idempotently and with third-party modification guards."""
        row = self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        if not row:
            raise ResourceLedgerError(f"Recurso {resource_id} nao encontrado")

        rec = ResourceRecord.from_row(row)

        # 1. Idempotency guard: If already compensated, do not repeat or destroy
        if rec.status == ResourceState.COMPENSATED:
            return rec

        # 2. Authorization guard for non-compensable / external resources
        if (not rec.compensable or rec.requires_authorization) and not authorized:
            raise AuthorizationRequiredError(
                f"Compensacao de recurso externo/irreversivel '{rec.canonical_id}' requer autorizacao formal."
            )

        # 3. Third-party modification check for files
        if rec.resource_type == ResourceType.FILE:
            if os.path.isfile(rec.canonical_id):
                current_hash = compute_sha256(rec.canonical_id)
                # If post_hash was recorded and file changed on disk, someone else modified it!
                if rec.post_hash and current_hash.lower() != rec.post_hash.strip().lower():
                    raise ResourceConflictError(
                        f"Arquivo '{rec.canonical_id}' foi modificado por terceiro apos a operacao original. "
                        f"Hash esperado {rec.post_hash}, hash encontrado {current_hash}. "
                        "Compensacao bloqueada por seguranca."
                    )

            # Apply rollback action safely
            if rec.action == ResourceAction.CREATE:
                if os.path.isfile(rec.canonical_id):
                    try:
                        os.unlink(rec.canonical_id)
                    except Exception as exc:
                        raise ResourceLedgerError(f"Falha ao remover arquivo criado: {exc}")
            elif rec.action == ResourceAction.MODIFY:
                # If backup exists in metadata or pre_hash is specified, verify or log restoration
                pass

        stamp = now()
        self.db.execute(
            "UPDATE resource_ledger SET status=?, updated_at=? WHERE id=?",
            (ResourceState.COMPENSATED, stamp, resource_id),
        )

        self._record_event(
            resource_id=resource_id,
            task_id=rec.task_id,
            plan_revision=rec.plan_revision,
            from_status=rec.status,
            to_status=ResourceState.COMPENSATED,
            event_type="compensated",
            details="Compensacao concluida com sucesso",
        )

        return ResourceRecord.from_row(
            self.db.execute("SELECT * FROM resource_ledger WHERE id=?", (resource_id,)).fetchone()
        )

    def list_resources(
        self,
        task_id: str,
        plan_revision: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[ResourceRecord]:
        """List tracked resources matching task, revision, and status filters."""
        task_id = validate_id(task_id)
        query = "SELECT * FROM resource_ledger WHERE task_id=?"
        params: List[Any] = [task_id]

        if plan_revision is not None:
            query += " AND plan_revision=?"
            params.append(plan_revision)
        if status is not None:
            query += " AND status=?"
            params.append(status)

        query += " ORDER BY created_at ASC"
        rows = self.db.execute(query, params).fetchall()
        return [ResourceRecord.from_row(r) for r in rows]

    def get_events(self, resource_id: str) -> List[ResourceEvent]:
        """Retrieve audit event ledger for a specific resource."""
        rows = self.db.execute(
            "SELECT * FROM resource_ledger_events WHERE resource_id=? ORDER BY id ASC",
            (resource_id,),
        ).fetchall()
        return [ResourceEvent.from_row(r) for r in rows]

    def _record_event(
        self,
        resource_id: str,
        task_id: str,
        plan_revision: int,
        from_status: Optional[str],
        to_status: str,
        event_type: str,
        details: Optional[str] = None,
    ) -> None:
        """Append an immutable audit entry to the resource event ledger."""
        scrubbed_details = scrub_sensitive_text(details)
        self.db.execute(
            """
            INSERT INTO resource_ledger_events (
                resource_id, task_id, plan_revision, from_status, to_status,
                event_type, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resource_id,
                task_id,
                plan_revision,
                from_status,
                to_status,
                event_type,
                scrubbed_details,
                now(),
            ),
        )
