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
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
import uuid


class WorkerAuditEventType(str, Enum):
    SPAWN = "spawn"
    SANDBOX_PREPARED = "sandbox_prepared"
    SANDBOX_DENIED = "sandbox_denied"
    TOOL_REQUESTED = "tool_requested"
    TOOL_ALLOWED = "tool_allowed"
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
    prev_hash: str = GENESIS_HASH
    id: Optional[int] = None


def compute_canonical_record_hash(
    prev_hash: str,
    sequence_number: int,
    task_id: str,
    run_id: str,
    worker_pid: Optional[int],
    process_identity_json: Optional[str],
    event_type: str,
    correlation_id: str,
    operational_revision: int,
    limits_json: Optional[str],
    resource_usage_json: Optional[str],
    reason: Optional[str],
    error: Optional[str],
    details_json: Optional[str],
    created_at: str,
) -> str:
    """Compute deterministic SHA-256 hash over canonical representation of all 15 audit fields."""
    elements = [
        prev_hash,
        sequence_number,
        task_id,
        run_id,
        worker_pid,
        process_identity_json,
        event_type,
        correlation_id,
        operational_revision,
        limits_json,
        resource_usage_json,
        reason,
        error,
        details_json,
        created_at,
    ]
    raw = json.dumps(elements, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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

        self._lock = getattr(store, "_lock", None) or threading.RLock()
        self.anchor_file_path: Optional[Path] = None
        if hasattr(self.store, "root") and self.store.root:
            auth_dir = Path(self.store.root) / "auth"
            try:
                auth_dir.mkdir(parents=True, exist_ok=True)
                self.anchor_file_path = auth_dir / "audit.anchor"
            except Exception:
                self.anchor_file_path = None

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
                created_at TEXT NOT NULL,
                prev_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'
            )
        """)

        # Backwards compatibility check for existing tables without prev_hash
        try:
            cursor = self.store.db.execute("PRAGMA table_info(worker_audit_log)")
            cols = [row[1] for row in cursor.fetchall()]
            if "prev_hash" not in cols:
                self.store.db.execute("ALTER TABLE worker_audit_log ADD COLUMN prev_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000'")
        except Exception:
            pass

        # Indexes for authorized queries
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_task ON worker_audit_log(task_id, sequence_number)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_run ON worker_audit_log(run_id)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_corr ON worker_audit_log(correlation_id)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_type ON worker_audit_log(event_type)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS idx_worker_audit_seq ON worker_audit_log(sequence_number)")

        # Strict SQL immutability triggers on worker_audit_log (block UPDATE and DELETE)
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

        # Anchoring table for tail-truncation defense
        self.store.db.execute("""
            CREATE TABLE IF NOT EXISTS worker_audit_anchors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                last_sequence_number INTEGER NOT NULL,
                last_chain_hash TEXT NOT NULL,
                anchor_signature TEXT NOT NULL,
                anchored_at TEXT NOT NULL
            )
        """)
        self.store.db.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_worker_audit_anchor_update
            BEFORE UPDATE ON worker_audit_anchors
            BEGIN
                SELECT RAISE(ABORT, 'worker_audit_anchors e append-only e imutavel: UPDATE proibido');
            END;
        """)
        self.store.db.execute("""
            CREATE TRIGGER IF NOT EXISTS prevent_worker_audit_anchor_delete
            BEFORE DELETE ON worker_audit_anchors
            BEGIN
                SELECT RAISE(ABORT, 'worker_audit_anchors e append-only e imutavel: DELETE proibido');
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
            with self._lock:
                # Query last sequence_number and chain_hash globally across the store
                row = self.store.db.execute("""
                    SELECT sequence_number, chain_hash
                    FROM worker_audit_log
                    ORDER BY sequence_number DESC, id DESC LIMIT 1
                """).fetchone()

                if row:
                    prev_seq, prev_hash = row[0], row[1]
                    seq_num = prev_seq + 1
                else:
                    prev_hash = GENESIS_HASH
                    seq_num = 1

                ident_json = json.dumps(clean_ident, sort_keys=True, separators=(",", ":")) if clean_ident else None
                limits_json = json.dumps(clean_limits, sort_keys=True, separators=(",", ":")) if clean_limits else None
                usage_json = json.dumps(clean_usage, sort_keys=True, separators=(",", ":")) if clean_usage else None
                details_json = json.dumps(clean_details, sort_keys=True, separators=(",", ":")) if clean_details else None

                # Canonical deterministic hash chaining over ALL 15 fields
                chain_hash = compute_canonical_record_hash(
                    prev_hash=prev_hash,
                    sequence_number=seq_num,
                    task_id=str(task_id),
                    run_id=str(run_id),
                    worker_pid=int(worker_pid) if worker_pid is not None else None,
                    process_identity_json=ident_json,
                    event_type=evt_type_str,
                    correlation_id=corr_id,
                    operational_revision=int(operational_revision),
                    limits_json=limits_json,
                    resource_usage_json=usage_json,
                    reason=reason,
                    error=error,
                    details_json=details_json,
                    created_at=created_at,
                )

                # Record HMAC signature
                sig = hmac.new(self.supervisor_secret, chain_hash.encode("ascii"), hashlib.sha256).hexdigest()

                # Record tail-anchoring entry
                anchor_payload = f"ANCHOR:{seq_num}:{chain_hash}:{created_at}"
                anchor_sig = hmac.new(self.supervisor_secret, anchor_payload.encode("ascii"), hashlib.sha256).hexdigest()

                self.store.db.execute("""
                    INSERT INTO worker_audit_log (
                        event_id, sequence_number, task_id, run_id, worker_pid,
                        process_identity, event_type, correlation_id,
                        operational_revision, limits_json, resource_usage_json,
                        reason, error, details_json, chain_hash, record_signature,
                        created_at, prev_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    event_id,
                    seq_num,
                    str(task_id),
                    str(run_id),
                    int(worker_pid) if worker_pid is not None else None,
                    ident_json,
                    evt_type_str,
                    corr_id,
                    int(operational_revision),
                    limits_json,
                    usage_json,
                    reason,
                    error,
                    details_json,
                    chain_hash,
                    sig,
                    created_at,
                    prev_hash,
                ))

                self.store.db.execute("""
                    INSERT INTO worker_audit_anchors (
                        last_sequence_number, last_chain_hash, anchor_signature, anchored_at
                    ) VALUES (?, ?, ?, ?)
                """, (
                    seq_num,
                    chain_hash,
                    anchor_sig,
                    created_at,
                ))

                self.store.db.commit()

                # Decoupled external anchor file persistence
                if self.anchor_file_path:
                    try:
                        anchor_payload_line = f"ANCHOR_V1|{seq_num}|{chain_hash}|{created_at}|{anchor_sig}\n"
                        tmp_anchor = self.anchor_file_path.with_suffix(".tmp")
                        tmp_anchor.write_text(anchor_payload_line, encoding="utf-8")
                        tmp_anchor.replace(self.anchor_file_path)
                    except Exception as ext_anc_err:
                        raise WorkerAuditPersistenceError(f"Falha ao persistir ancora externa desacoplada: {ext_anc_err}") from ext_anc_err

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
                prev_hash=prev_hash,
            )
        except Exception as e:
            # Fail-closed policy: audit persistence failures must abort privileged execution
            raise WorkerAuditPersistenceError(f"Falha ao persistir evento de auditoria '{evt_type_str}': {e}") from e

    def verify_integrity(
        self,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Tuple[bool, List[str]]:
        """Verify cryptographic hash chaining, canonical payload integrity, HMAC, and tail truncation."""
        if not hasattr(self.store, "db") or not self.store.db:
            return False, ["Base de dados indisponivel"]

        errors: List[str] = []

        # 1. Verify Anchors against tail truncation
        try:
            anchor_rows = self.store.db.execute("""
                SELECT last_sequence_number, last_chain_hash, anchor_signature, anchored_at
                FROM worker_audit_anchors
                ORDER BY id DESC LIMIT 1
            """).fetchall()

            max_log_row = self.store.db.execute("""
                SELECT sequence_number, chain_hash
                FROM worker_audit_log
                ORDER BY sequence_number DESC, id DESC LIMIT 1
            """).fetchone()

            if anchor_rows:
                last_anc_seq, last_anc_hash, last_anc_sig, last_anc_time = anchor_rows[0]
                expected_anc_payload = f"ANCHOR:{last_anc_seq}:{last_anc_hash}:{last_anc_time}"
                expected_anc_sig = hmac.new(self.supervisor_secret, expected_anc_payload.encode("ascii"), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(last_anc_sig, expected_anc_sig):
                    errors.append(f"Assinatura HMAC invalida na tabela de ancoragem (ancora seq {last_anc_seq})")

                if not max_log_row:
                    errors.append(f"Truncamento total detectado: tabela worker_audit_log vazia, mas ancora seq {last_anc_seq} existe")
                else:
                    max_seq, max_hash = max_log_row[0], max_log_row[1]
                    if max_seq < last_anc_seq:
                        errors.append(f"Truncamento de log detectado: ultima sequencia no log ({max_seq}) e inferior a sequencia ancorada ({last_anc_seq})")
                    elif max_hash != last_anc_hash and max_seq == last_anc_seq:
                        errors.append(f"Divergencia entre o chain_hash do log ({max_hash}) e a ancora ({last_anc_hash}) na seq {last_anc_seq}")
        except Exception as anchor_err:
            errors.append(f"Falha ao consultar tabela de ancoragem: {anchor_err}")

        # Check Decoupled External Anchor File
        if self.anchor_file_path and self.anchor_file_path.is_file():
            try:
                raw_ext = self.anchor_file_path.read_text(encoding="utf-8").strip()
                parts = raw_ext.split("|")
                if len(parts) == 5 and parts[0] == "ANCHOR_V1":
                    ext_seq = int(parts[1])
                    ext_hash = parts[2]
                    ext_time = parts[3]
                    ext_sig = parts[4]

                    expected_ext_payload = f"ANCHOR:{ext_seq}:{ext_hash}:{ext_time}"
                    expected_ext_sig = hmac.new(self.supervisor_secret, expected_ext_payload.encode("ascii"), hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(ext_sig, expected_ext_sig):
                        errors.append(f"Assinatura HMAC invalida no arquivo desacoplado de ancoragem ({self.anchor_file_path.name})")

                    if not max_log_row:
                        errors.append(f"Truncamento total detectado via ancora desacoplada: log vazio, mas ancora externa seq {ext_seq} existe")
                    else:
                        max_seq, max_hash = max_log_row[0], max_log_row[1]
                        if max_seq < ext_seq:
                            errors.append(f"Truncamento de log detectado via ancora desacoplada: ultima sequencia ({max_seq}) e inferior a ancora externa ({ext_seq})")
                        elif max_hash != ext_hash and max_seq == ext_seq:
                            errors.append(f"Divergencia entre chain_hash do log ({max_hash}) e ancora desacoplada ({ext_hash}) na seq {ext_seq}")
                else:
                    errors.append(f"Formato invalido no arquivo desacoplado de ancoragem ({self.anchor_file_path.name})")
            except Exception as ext_err:
                errors.append(f"Falha ao consultar arquivo desacoplado de ancoragem: {ext_err}")

        # 2. Sequential linear verification across the whole log
        all_rows = self.store.db.execute("""
            SELECT
                id, event_id, sequence_number, task_id, run_id, worker_pid,
                process_identity, event_type, correlation_id, operational_revision,
                limits_json, resource_usage_json, reason, error, details_json,
                chain_hash, record_signature, created_at, prev_hash
            FROM worker_audit_log
            ORDER BY sequence_number ASC, id ASC
        """).fetchall()

        if not all_rows:
            return len(errors) == 0, errors

        prev_hash = GENESIS_HASH
        expected_seq = 1
        scoped_found = False

        for r in all_rows:
            (
                r_id, event_id, seq, t_id, r_un_id, w_pid,
                proc_ident, evt_type, corr, op_rev,
                lim_json, res_json, reas, err, det_json,
                rec_chain, rec_sig, created, stored_prev_hash
            ) = r

            if task_id and t_id == str(task_id):
                scoped_found = True
            if run_id and r_un_id == str(run_id):
                scoped_found = True

            if seq != expected_seq:
                errors.append(f"Quebra de sequencia no evento {event_id}: esperado {expected_seq}, encontrado {seq}")

            if stored_prev_hash and stored_prev_hash != prev_hash:
                errors.append(f"Divergencia de prev_hash no evento {event_id} (seq {seq})")

            # Canonical hash recomputation over ALL fields
            expected_chain = compute_canonical_record_hash(
                prev_hash=prev_hash,
                sequence_number=seq,
                task_id=t_id,
                run_id=r_un_id,
                worker_pid=w_pid,
                process_identity_json=proc_ident,
                event_type=evt_type,
                correlation_id=corr,
                operational_revision=op_rev,
                limits_json=lim_json,
                resource_usage_json=res_json,
                reason=reas,
                error=err,
                details_json=det_json,
                created_at=created,
            )

            if rec_chain != expected_chain:
                errors.append(f"Adulteracao de chain_hash no evento {event_id} (seq {seq})")

            # Verify HMAC
            expected_sig = hmac.new(self.supervisor_secret, rec_chain.encode("ascii"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(rec_sig, expected_sig):
                errors.append(f"Assinatura HMAC invalida no evento {event_id} (seq {seq})")

            prev_hash = rec_chain
            expected_seq += 1

        if (task_id or run_id) and not scoped_found:
            errors.append(f"Nenhum evento localizado para o escopo task_id='{task_id}' run_id='{run_id}'")

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

        query = """
            SELECT
                id, event_id, sequence_number, task_id, run_id, worker_pid,
                process_identity, event_type, correlation_id, operational_revision,
                limits_json, resource_usage_json, reason, error, details_json,
                chain_hash, record_signature, created_at, prev_hash
            FROM worker_audit_log WHERE 1=1
        """
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
                prev_hash=r[18] if len(r) > 18 and r[18] else GENESIS_HASH,
            ))
        return records
