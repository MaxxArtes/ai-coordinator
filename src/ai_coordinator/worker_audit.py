"""Worker Execution Audit Subsystem for Phase 4B.5.4.

Provides an immutable, append-only, cryptographically chained audit log
of all worker lifecycle events and tool authorizations.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import uuid


class WorkerAuditEventType(str, Enum):
    SPAWN = "spawn"
    SANDBOX_PREPARED = "sandbox_prepared"
    SANDBOX_DENIED = "sandbox_denied"
    TOOL_REQUESTED = "tool_requested"
    TOOL_DENIED = "tool_denied"
    EXIT = "exit"
    TIMEOUT = "timeout"
    TERMINATION = "termination"


class WorkerAuditError(Exception):
    """Base error for worker execution audit."""


class WorkerAuditPersistenceError(WorkerAuditError):
    """Raised when audit log persistence fails under fail-closed policy."""


class WorkerAuditIntegrityError(WorkerAuditError):
    """Raised when audit chain verification detects tampering or gaps."""


GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"


@dataclass
class WorkerAuditRecord:
    event_id: str
    sequence_number: int
    task_id: str
    run_id: str
    worker_pid: Optional[int]
    process_identity: Optional[Dict[str, Any]]
    event_type: str
    correlation_id: str
    operational_revision: int
    limits: Optional[Dict[str, Any]]
    resource_usage: Optional[Dict[str, Any]]
    reason: Optional[str]
    error: Optional[str]
    details: Optional[Dict[str, Any]]
    chain_hash: str
    record_signature: str
    created_at: str
    id: Optional[int] = None


def sanitize_audit_payload(obj: Any) -> Any:
    """Recursively strip authentication tokens, private secrets, and credentials."""
    if isinstance(obj, dict):
        clean = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("token", "secret", "key", "password", "auth", "credential")):
                clean[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                clean[k] = sanitize_audit_payload(v)
            else:
                clean[k] = v
        return clean
    elif isinstance(obj, (list, tuple)):
        return [sanitize_audit_payload(x) for x in obj]
    return obj


class WorkerAuditLogger:
    """Manages append-only, HMAC-signed, hash-chained worker execution audit logs."""

    def __init__(self, store: Any, supervisor_key: Union[str, bytes]):
        self.store = store
        if isinstance(supervisor_key, str):
            self.supervisor_secret = supervisor_key.encode("utf-8")
        else:
            self.supervisor_secret = bytes(supervisor_key)
        if not self.supervisor_secret:
            raise ValueError("supervisor_key obrigatoria para assinatura da trilha de auditoria")

        self._init_table()

    def _init_table(self) -> None:
        """Create append-only worker_audit_log and immutable triggers in SQLite."""
        if not hasattr(self.store, "db") or not self.store.db:
            return

        self.store.db.execute("""
            CREATE TABLE IF NOT EXISTS worker_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                sequence_number INTEGER NOT NULL,
                task_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                worker_pid INTEGER,
                process_identity TEXT,
                event_type TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                operational_revision INTEGER NOT NULL DEFAULT 0,
                limits_json TEXT,
                resource_usage_json TEXT,
                reason TEXT,
                error TEXT,
                details_json TEXT,
                chain_hash TEXT NOT NULL,
                record_signature TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # Indexes for authorized queries
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_task ON worker_audit_log(task_id, sequence_number)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_run ON worker_audit_log(run_id)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_corr ON worker_audit_log(correlation_id)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_type ON worker_audit_log(event_type)")

        # Strict SQL immutability triggers (block UPDATE and DELETE)
        self.store.db.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_worker_audit_update
            BEFORE UPDATE ON worker_audit_log
            BEGIN
                SELECT RAISE(ABORT, 'worker_audit_log e append-only e imutavel: UPDATE proibido');
            END;
        """)
        self.store.db.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_worker_audit_delete
            BEFORE DELETE ON worker_audit_log
            BEGIN
                SELECT RAISE(ABORT, 'worker_audit_log e append-only e imutavel: DELETE proibido');
            END;
        """)
        self.store.db.commit()

    def record_event(
        self,
        event_type: Union[WorkerAuditEventType, str],
        task_id: str,
        run_id: str,
        correlation_id: Optional[str] = None,
        worker_pid: Optional[int] = None,
        process_identity: Optional[Dict[str, Any]] = None,
        operational_revision: int = 0,
        limits: Optional[Dict[str, Any]] = None,
        resource_usage: Optional[Dict[str, Any]] = None,
        reason: Optional[str] = None,
        error: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> WorkerAuditRecord:
        """Record an immutable, hash-chained worker lifecycle or security event."""
        if not hasattr(self.store, "db") or not self.store.db:
            raise WorkerAuditPersistenceError("Base de dados SQLite indisponivel para registrar auditoria de worker")

        evt_type_str = event_type.value if isinstance(event_type, WorkerAuditEventType) else str(event_type)
        corr_id = correlation_id or uuid.uuid4().hex[:12]
        event_id = uuid.uuid4().hex[:16]
        created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Sanitization
        clean_limits = sanitize_audit_payload(limits) if limits else None
        clean_usage = sanitize_audit_payload(resource_usage) if resource_usage else None
        clean_details = sanitize_audit_payload(details) if details else None
        clean_ident = sanitize_audit_payload(process_identity) if process_identity else None

        try:
            # Query last sequence_number and chain_hash for this task & run (or globally)
            row = self.store.db.execute("""
                SELECT sequence_number, chain_hash
                FROM worker_audit_log
                WHERE task_id = ? AND run_id = ?
                ORDER BY sequence_number DESC LIMIT 1
            """, (str(task_id), str(run_id))).fetchone()

            if row:
                prev_seq, prev_hash = row[0], row[1]
                seq_num = prev_seq + 1
            else:
                prev_hash = GENESIS_HASH
                seq_num = 1

            # Cryptographic hash chaining: H(prev_hash || seq || event_type || corr || task || run || created_at)
            chain_payload = f"{prev_hash}:{seq_num}:{evt_type_str}:{corr_id}:{task_id}:{run_id}:{created_at}"
            chain_hash = hashlib.sha256(chain_payload.encode("utf-8")).hexdigest()

            # Record HMAC signature
            sig = hmac.new(self.supervisor_secret, chain_hash.encode("ascii"), hashlib.sha256).hexdigest()

            self.store.db.execute("""
                INSERT INTO worker_audit_log (
                    event_id, sequence_number, task_id, run_id, worker_pid,
                    process_identity, event_type, correlation_id,
                    operational_revision, limits_json, resource_usage_json,
                    reason, error, details_json, chain_hash, record_signature,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id,
                seq_num,
                str(task_id),
                str(run_id),
                int(worker_pid) if worker_pid is not None else None,
                json.dumps(clean_ident) if clean_ident else None,
                evt_type_str,
                corr_id,
                int(operational_revision),
                json.dumps(clean_limits) if clean_limits else None,
                json.dumps(clean_usage) if clean_usage else None,
                reason,
                error,
                json.dumps(clean_details) if clean_details else None,
                chain_hash,
                sig,
                created_at,
            ))
            self.store.db.commit()

            return WorkerAuditRecord(
                event_id=event_id,
                sequence_number=seq_num,
                task_id=str(task_id),
                run_id=str(run_id),
                worker_pid=int(worker_pid) if worker_pid is not None else None,
                process_identity=clean_ident,
                event_type=evt_type_str,
                correlation_id=corr_id,
                operational_revision=int(operational_revision),
                limits=clean_limits,
                resource_usage=clean_usage,
                reason=reason,
                error=error,
                details=clean_details,
                chain_hash=chain_hash,
                record_signature=sig,
                created_at=created_at,
            )
        except Exception as e:
            # Fail-closed policy: audit persistence failures must abort privileged execution
            raise WorkerAuditPersistenceError(f"Falha ao persistir evento de auditoria '{evt_type_str}': {e}") from e

    def verify_integrity(
        self,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Verify cryptographic hash chaining and HMAC signatures across audit entries."""
        if not hasattr(self.store, "db") or not self.store.db:
            return False, ["Base de dados indisponivel"]

        query = "SELECT * FROM worker_audit_log WHERE 1=1"
        params: list[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(str(task_id))
        if run_id:
            query += " AND run_id = ?"
            params.append(str(run_id))

        query += " ORDER BY sequence_number ASC, id ASC"
        rows = self.store.db.execute(query, params).fetchall()

        if not rows:
            return True, []

        errors = []
        prev_hash = GENESIS_HASH
        expected_seq = 1

        for r in rows:
            # row mapping
            # (id, event_id, sequence_number, task_id, run_id, worker_pid, process_identity,
            #  event_type, correlation_id, operational_revision, limits_json, resource_usage_json,
            #  reason, error, details_json, chain_hash, record_signature, created_at)
            seq = r[2]
            t_id = r[3]
            r_id = r[4]
            evt_type = r[7]
            corr = r[8]
            rec_chain = r[15]
            rec_sig = r[16]
            created = r[17]

            if seq != expected_seq:
                errors.append(f"Quebra de sequencia no evento {r[1]}: esperado {expected_seq}, encontrado {seq}")

            # Recalculate hash
            expected_payload = f"{prev_hash}:{seq}:{evt_type}:{corr}:{t_id}:{r_id}:{created}"
            expected_chain = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
            if rec_chain != expected_chain:
                errors.append(f"Adulteracao de chain_hash no evento {r[1]} (seq {seq})")

            # Verify HMAC
            expected_sig = hmac.new(self.supervisor_secret, rec_chain.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(rec_sig, expected_sig):
                errors.append(f"Assinatura HMAC invalida no evento {r[1]} (seq {seq})")

            prev_hash = rec_chain
            expected_seq += 1

        return len(errors) == 0, errors

    def query_events(
        self,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        event_type: Optional[str] = None,
        correlation_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[WorkerAuditRecord]:
        """Query audited worker events with optional scoping."""
        if not hasattr(self.store, "db") or not self.store.db:
            return []

        query = "SELECT * FROM worker_audit_log WHERE 1=1"
        params: list[Any] = []
        if task_id:
            query += " AND task_id = ?"
            params.append(str(task_id))
        if run_id:
            query += " AND run_id = ?"
            params.append(str(run_id))
        if event_type:
            query += " AND event_type = ?"
            params.append(str(event_type))
        if correlation_id:
            query += " AND correlation_id = ?"
            params.append(str(correlation_id))

        query += " ORDER BY sequence_number ASC LIMIT ?"
        params.append(max(1, int(limit)))

        rows = self.store.db.execute(query, params).fetchall()
        records = []
        for r in rows:
            records.append(WorkerAuditRecord(
                id=r[0],
                event_id=r[1],
                sequence_number=r[2],
                task_id=r[3],
                run_id=r[4],
                worker_pid=r[5],
                process_identity=json.loads(r[6]) if r[6] else None,
                event_type=r[7],
                correlation_id=r[8],
                operational_revision=r[9],
                limits=json.loads(r[10]) if r[10] else None,
                resource_usage=json.loads(r[11]) if r[11] else None,
                reason=r[12],
                error=r[13],
                details=json.loads(r[14]) if r[14] else None,
                chain_hash=r[15],
                record_signature=r[16],
                created_at=r[17],
            ))
        return records
