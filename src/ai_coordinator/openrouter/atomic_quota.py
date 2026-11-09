"""Atomic Quota Store, Transactional Outbox, and Governed Multi-Model Fallback (Phase 4C.2.3).

Implements:
1. P0.1 — Local At-Most-Once Guarantee & Point of Irreversible Commitment (SEND_COMMITTED).
2. P0.2 — Audit Atomicity via SQLite Transactional Outbox projected with fsync to JSONL.
3. P0.3 — Finite State Machine and Conservative Crash Recovery (fail-closed UNKNOWN state).
4. Governed Multi-Model Fallback Engine with two-level dispatch policy.
5. Persistent Anti-Replay Ledger bound to Ed25519 canonical authorization manifests.

Zero live network inference: strictly offline and local SQLite/filesystem operation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ai_coordinator.manifest_auth import (
    CanonicalAuthorizationManifest,
    HumanOperationalAuthorizationError,
    KeyAuthorityError,
    ReplayAuthorizationError,
    SignedAuthorizationManifest,
    TripartiteAuthorizationVerifier,
    TrustStore,
)
from ai_coordinator.openrouter.cost_guard import FreeCostGuard
from ai_coordinator.openrouter.registry import FreeModelRegistry

# Reservation finite states
STATE_RESERVED = "RESERVED"
STATE_SEND_COMMITTED = "SEND_COMMITTED"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"
STATE_UNKNOWN = "UNKNOWN"

VALID_RESERVATION_STATES = {
    STATE_RESERVED,
    STATE_SEND_COMMITTED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_UNKNOWN,
}

# Reconciliation states
RECON_NONE = "NONE"
RECON_ABORTED = "RECONCILED_ABORTED"
RECON_TRANSMITTED = "RECONCILED_TRANSMITTED"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class QuotaSecurityError(Exception):
    """Base exception for atomic dispatch quota and outbox errors."""


class BudgetExhaustedError(QuotaSecurityError):
    """Raised when max_dispatches limit has been reached and no further dispatches are permitted."""


class IllegalStateTransitionError(QuotaSecurityError):
    """Raised when an invalid or prohibited state transition is attempted on a reservation."""


class ReservationNotFoundError(QuotaSecurityError):
    """Raised when a referenced reservation_id does not exist."""


class OutboxPersistenceError(QuotaSecurityError):
    """Raised when persisting or projecting an outbox event fails."""


class PersistentReplayAttackError(QuotaSecurityError):
    """Raised when an authorization_id has already been consumed in the persistent SQLite store."""


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchReservation:
    """Represents an atomic reservation for a single physical model dispatch attempt."""
    reservation_id: str
    budget_id: str
    logical_attempt_id: str
    sequence_index: int
    idempotency_key: str
    target_model: str
    state: str
    reserved_at_epoch: float
    send_committed_at_epoch: Optional[float] = None
    dispatched_at_epoch: Optional[float] = None
    completed_at_epoch: Optional[float] = None
    http_status_code: Optional[int] = None
    reconciliation_state: str = RECON_NONE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OutboxEvent:
    """Represents a durable audit event in the transactional outbox."""
    event_id: str
    reservation_id: str
    aggregate_id: str
    event_type: str
    payload_json: str
    created_at_epoch: float
    projected_at_epoch: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FallbackDecision:
    """Result of evaluating fallback contingency options."""
    action: str  # "RECOMMENDED_ONLY", "DISPATCH_AUTHORIZED", "BLOCKED_BUDGET", "NO_CANDIDATE"
    target_model: Optional[str]
    sequence_index: Optional[int]
    reservation_id: Optional[str]
    reason: str
    details: Dict[str, Any]


# ---------------------------------------------------------------------------
# SQLite Atomic Quota Store & Transactional Outbox
# ---------------------------------------------------------------------------

class AtomicDispatchQuotaStore:
    """Atomic Dispatch Quota Store backed by SQLite WAL with Transactional Outbox.

    Enforces local at-most-once semantics, the Point of Irreversible Commitment (PIC),
    persistent anti-replay protection, and crash-recovery to UNKNOWN state.
    """

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self._is_memory = db_path == ":memory:"
        self._conn: Optional[sqlite3.Connection] = None
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        if self._is_memory:
            if self._conn is None:
                self._conn = sqlite3.connect(
                    ":memory:",
                    isolation_level=None,  # autocommit / manual transactions
                    check_same_thread=False,
                )
            return self._conn
        else:
            conn = sqlite3.connect(
                self.db_path,
                isolation_level=None,
                timeout=5.0,
                check_same_thread=False,
            )
            return conn

    def _init_database(self) -> None:
        """Initialize database pragmas and DDL schema."""
        conn = self._get_connection()
        try:
            conn.execute("PRAGMA busy_timeout = 5000;")
            conn.execute("PRAGMA foreign_keys = ON;")
            if not self._is_memory:
                conn.execute("PRAGMA journal_mode = WAL;")
                conn.execute("PRAGMA synchronous = FULL;")

            # 1. Global dispatch budgets table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS global_dispatch_budgets (
                budget_id TEXT PRIMARY KEY,
                manifest_id TEXT NOT NULL UNIQUE,
                canonical_sha256 TEXT NOT NULL,
                code_commit TEXT NOT NULL,
                max_dispatches INTEGER NOT NULL CHECK (max_dispatches >= 1),
                max_budget_usd REAL NOT NULL CHECK (max_budget_usd = 0.0),
                allowed_models_json TEXT NOT NULL,
                authorizer_identity TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                valid_until_utc TEXT NOT NULL
            );
            """)

            # 2. Persistent consumed authorizations table (Anti-Replay Ledger)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS consumed_authorizations (
                authorization_id TEXT PRIMARY KEY,
                execution_scope_id TEXT NOT NULL,
                consumed_at_epoch REAL NOT NULL,
                authorizer_identity TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """)

            # 3. Dispatch reservations table with irreversible states
            conn.execute("""
            CREATE TABLE IF NOT EXISTS dispatch_reservations (
                reservation_id TEXT PRIMARY KEY,
                budget_id TEXT NOT NULL REFERENCES global_dispatch_budgets(budget_id),
                logical_attempt_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL CHECK (sequence_index >= 0),
                idempotency_key TEXT NOT NULL UNIQUE,
                target_model TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('RESERVED', 'SEND_COMMITTED', 'COMPLETED', 'FAILED', 'UNKNOWN')),
                reserved_at_epoch REAL NOT NULL,
                send_committed_at_epoch REAL,
                dispatched_at_epoch REAL,
                completed_at_epoch REAL,
                http_status_code INTEGER,
                reconciliation_state TEXT NOT NULL DEFAULT 'NONE' CHECK (reconciliation_state IN ('NONE', 'RECONCILED_ABORTED', 'RECONCILED_TRANSMITTED'))
            );
            """)

            # 4. Transactional Outbox table
            conn.execute("""
            CREATE TABLE IF NOT EXISTS outbox_events (
                event_id TEXT PRIMARY KEY,
                reservation_id TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at_epoch REAL NOT NULL,
                projected_at_epoch REAL
            );
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_unprojected ON outbox_events(projected_at_epoch) WHERE projected_at_epoch IS NULL;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_res_budget_state ON dispatch_reservations(budget_id, state);")
        finally:
            if not self._is_memory:
                conn.close()

    def register_budget_from_signed_manifest(
        self,
        signed_manifest: SignedAuthorizationManifest,
        trust_store: TrustStore,
        expected_commit: str,
        human_operator_consent: bool = True,
        check_time_utc: Optional[dt.datetime] = None,
    ) -> str:
        """Validate signed manifest using Tripartite Verifier and atomically register budget.

        Persists authorization_id in consumed_authorizations to guarantee cross-process,
        cross-restart anti-replay protection.
        """
        # 1. Enforce tripartite verification (Tiers 1, 2, and 3)
        verifier = TripartiteAuthorizationVerifier(trust_store=trust_store)
        res = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=expected_commit,
            requested_model=signed_manifest.manifest.allowed_models[0],
            requested_modality="text",
            requested_dispatches=signed_manifest.manifest.max_dispatches,
            human_operator_consent=human_operator_consent,
            at_time_utc=check_time_utc,
            check_replay=False,  # Replay is verified persistently via SQLite UNIQUE constraint below
        )
        if not res.is_fully_authorized:
            if not res.tier1_crypto_valid:
                raise QuotaSecurityError(f"Tier 1 (Crypto) verification failed: {res.tier1_reason}")
            if not res.tier2_key_authority:
                raise KeyAuthorityError(f"Tier 2 (Key Authority) verification failed: {res.tier2_reason}")
            if not res.tier3_human_authorized:
                raise HumanOperationalAuthorizationError(f"Tier 3 (Human Ops) verification failed: {res.tier3_reason}")

        manifest = signed_manifest.manifest
        budget_id = f"budget_{manifest.authorization_id}"
        now_epoch = time.time()

        conn = self._get_connection()
        in_tx = False
        try:
            conn.execute("BEGIN IMMEDIATE;")
            in_tx = True
            # Check persistent anti-replay
            cur = conn.execute("SELECT consumed_at_epoch FROM consumed_authorizations WHERE authorization_id = ?", (manifest.authorization_id,))
            row = cur.fetchone()
            if row is not None:
                raise PersistentReplayAttackError(
                    f"Persistent replay attack prevented: authorization_id '{manifest.authorization_id}' "
                    f"was already consumed at epoch {row[0]}"
                )

            # Record in consumed_authorizations
            conn.execute("""
            INSERT INTO consumed_authorizations (
                authorization_id, execution_scope_id, consumed_at_epoch, authorizer_identity, details_json
            ) VALUES (?, ?, ?, ?, ?);
            """, (
                manifest.authorization_id,
                manifest.execution_scope_id,
                now_epoch,
                manifest.authorizer_identity,
                json.dumps({
                    "code_commit": manifest.code_commit,
                    "max_dispatches": manifest.max_dispatches,
                    "allowed_models": list(manifest.allowed_models),
                    "canonical_sha256": manifest.canonical_sha256(),
                    "key_id": signed_manifest.signature.key_id,
                }),
            ))

            # Record in global_dispatch_budgets
            conn.execute("""
            INSERT INTO global_dispatch_budgets (
                budget_id, manifest_id, canonical_sha256, code_commit, max_dispatches,
                max_budget_usd, allowed_models_json, authorizer_identity, created_at_utc, valid_until_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                budget_id,
                manifest.authorization_id,
                manifest.canonical_sha256(),
                manifest.code_commit,
                manifest.max_dispatches,
                0.0,
                json.dumps(list(manifest.allowed_models)),
                manifest.authorizer_identity,
                manifest.issued_at_utc,
                manifest.valid_until_utc,
            ))

            # Insert outbox event for budget registration
            outbox_id = f"evt_budget_reg_{uuid.uuid4().hex[:12]}"
            conn.execute("""
            INSERT INTO outbox_events (
                event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, NULL);
            """, (
                outbox_id,
                budget_id,
                budget_id,
                "BUDGET_REGISTERED",
                json.dumps({
                    "budget_id": budget_id,
                    "authorization_id": manifest.authorization_id,
                    "max_dispatches": manifest.max_dispatches,
                    "allowed_models": list(manifest.allowed_models),
                }),
                now_epoch,
            ))

            conn.execute("COMMIT;")
            in_tx = False
            return budget_id
        except sqlite3.IntegrityError as err:
            raise PersistentReplayAttackError(f"Integrity violation registering manifest (replay or duplicate): {err}") from err
        finally:
            if in_tx:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
            if not self._is_memory:
                conn.close()

    def acquire_reservation(
        self,
        budget_id: str,
        logical_attempt_id: str,
        sequence_index: int,
        target_model: str,
    ) -> DispatchReservation:
        """Atomically acquire a unit dispatch reservation if remaining budget > 0.

        Guarantees local at-most-once: total dispatches <= max_dispatches.
        """
        idempotency_raw = f"{budget_id}:{logical_attempt_id}:{sequence_index}:{target_model}".encode("utf-8")
        idempotency_key = hashlib.sha256(idempotency_raw).hexdigest()
        reservation_id = f"res_{idempotency_key[:16]}"
        now_epoch = time.time()

        conn = self._get_connection()
        in_tx = False
        try:
            conn.execute("BEGIN IMMEDIATE;")
            in_tx = True
            # 1. Query budget configuration
            cur = conn.execute("SELECT max_dispatches, allowed_models_json FROM global_dispatch_budgets WHERE budget_id = ?", (budget_id,))
            b_row = cur.fetchone()
            if b_row is None:
                raise QuotaSecurityError(f"Budget '{budget_id}' not found")
            max_dispatches, allowed_models_raw = b_row[0], b_row[1]
            allowed_models = json.loads(allowed_models_raw)
            if target_model not in allowed_models:
                raise QuotaSecurityError(f"Target model '{target_model}' not authorized in budget allowed_models: {allowed_models}")

            # 2. Check if this exact reservation already exists (idempotency check)
            cur = conn.execute("SELECT reservation_id, state FROM dispatch_reservations WHERE idempotency_key = ?", (idempotency_key,))
            existing = cur.fetchone()
            if existing is not None:
                raise QuotaSecurityError(f"Idempotency collision: reservation with key '{idempotency_key[:12]}' already exists in state '{existing[1]}'")

            # 3. Check total consumed reservations
            cur = conn.execute("""
            SELECT COUNT(*) FROM dispatch_reservations
            WHERE budget_id = ? AND state IN ('RESERVED', 'SEND_COMMITTED', 'COMPLETED', 'FAILED', 'UNKNOWN')
            """, (budget_id,))
            consumed_count = cur.fetchone()[0]

            if consumed_count >= max_dispatches:
                raise BudgetExhaustedError(
                    f"Global dispatch budget exhausted: {consumed_count}/{max_dispatches} dispatches consumed"
                )

            # 4. Insert new reservation in RESERVED state
            conn.execute("""
            INSERT INTO dispatch_reservations (
                reservation_id, budget_id, logical_attempt_id, sequence_index,
                idempotency_key, target_model, state, reserved_at_epoch, reconciliation_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NONE');
            """, (
                reservation_id, budget_id, logical_attempt_id, sequence_index,
                idempotency_key, target_model, STATE_RESERVED, now_epoch
            ))

            # 5. Insert outbox event for reservation
            outbox_id = f"evt_res_created_{uuid.uuid4().hex[:12]}"
            conn.execute("""
            INSERT INTO outbox_events (
                event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, NULL);
            """, (
                outbox_id,
                reservation_id,
                budget_id,
                "DISPATCH_RESERVED",
                json.dumps({
                    "reservation_id": reservation_id,
                    "target_model": target_model,
                    "sequence_index": sequence_index,
                    "consumed_count": consumed_count + 1,
                    "max_dispatches": max_dispatches,
                }),
                now_epoch,
            ))

            conn.execute("COMMIT;")
            in_tx = False

            return DispatchReservation(
                reservation_id=reservation_id,
                budget_id=budget_id,
                logical_attempt_id=logical_attempt_id,
                sequence_index=sequence_index,
                idempotency_key=idempotency_key,
                target_model=target_model,
                state=STATE_RESERVED,
                reserved_at_epoch=now_epoch,
            )
        finally:
            if in_tx:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
            if not self._is_memory:
                conn.close()

    def commit_send(self, reservation_id: str) -> None:
        """Advance reservation from RESERVED to SEND_COMMITTED (Point of Irreversible Commitment - PIC).

        Persisted with fsync in SQLite BEFORE opening TLS socket.
        From this point onward, budget is permanently drained and cannot be returned.
        """
        now_epoch = time.time()
        conn = self._get_connection()
        in_tx = False
        try:
            conn.execute("BEGIN IMMEDIATE;")
            in_tx = True
            cur = conn.execute("SELECT state, budget_id, target_model FROM dispatch_reservations WHERE reservation_id = ?", (reservation_id,))
            row = cur.fetchone()
            if row is None:
                raise ReservationNotFoundError(f"Reservation '{reservation_id}' not found")
            current_state, budget_id, target_model = row[0], row[1], row[2]

            if current_state != STATE_RESERVED:
                raise IllegalStateTransitionError(
                    f"Cannot commit send on reservation '{reservation_id}' in state '{current_state}', expected '{STATE_RESERVED}'"
                )

            # Update state to SEND_COMMITTED
            conn.execute("""
            UPDATE dispatch_reservations
            SET state = ?, send_committed_at_epoch = ?
            WHERE reservation_id = ?;
            """, (STATE_SEND_COMMITTED, now_epoch, reservation_id))

            # Record outbox event for SEND_COMMITTED
            outbox_id = f"evt_send_comm_{uuid.uuid4().hex[:12]}"
            conn.execute("""
            INSERT INTO outbox_events (
                event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, NULL);
            """, (
                outbox_id,
                reservation_id,
                budget_id,
                "SEND_COMMITTED",
                json.dumps({
                    "reservation_id": reservation_id,
                    "target_model": target_model,
                    "send_committed_at_epoch": now_epoch,
                }),
                now_epoch,
            ))

            conn.execute("COMMIT;")
            in_tx = False
        finally:
            if in_tx:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
            if not self._is_memory:
                conn.close()

    def record_completion(
        self,
        reservation_id: str,
        http_status_code: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record outcome of physical dispatch: HTTP 200 -> COMPLETED; HTTP 4xx/5xx -> FAILED."""
        now_epoch = time.time()
        conn = self._get_connection()
        in_tx = False
        try:
            conn.execute("BEGIN IMMEDIATE;")
            in_tx = True
            cur = conn.execute("SELECT state, budget_id, target_model FROM dispatch_reservations WHERE reservation_id = ?", (reservation_id,))
            row = cur.fetchone()
            if row is None:
                raise ReservationNotFoundError(f"Reservation '{reservation_id}' not found")
            current_state, budget_id, target_model = row[0], row[1], row[2]

            if current_state != STATE_SEND_COMMITTED:
                raise IllegalStateTransitionError(
                    f"Cannot complete reservation '{reservation_id}' in state '{current_state}', must be '{STATE_SEND_COMMITTED}'"
                )

            new_state = STATE_COMPLETED if http_status_code == 200 else STATE_FAILED
            event_type = "DISPATCH_COMPLETED_200" if http_status_code == 200 else f"DISPATCH_FAILED_{http_status_code}"

            conn.execute("""
            UPDATE dispatch_reservations
            SET state = ?, completed_at_epoch = ?, http_status_code = ?
            WHERE reservation_id = ?;
            """, (new_state, now_epoch, http_status_code, reservation_id))

            payload = {
                "reservation_id": reservation_id,
                "target_model": target_model,
                "http_status_code": http_status_code,
                "state": new_state,
                "completed_at_epoch": now_epoch,
            }
            if details:
                payload["details"] = details

            outbox_id = f"evt_compl_{uuid.uuid4().hex[:12]}"
            conn.execute("""
            INSERT INTO outbox_events (
                event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, NULL);
            """, (outbox_id, reservation_id, budget_id, event_type, json.dumps(payload), now_epoch))

            conn.execute("COMMIT;")
            in_tx = False
        finally:
            if in_tx:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
            if not self._is_memory:
                conn.close()

    def record_failure_unknown(self, reservation_id: str, reason: str) -> None:
        """Transition reservation directly to UNKNOWN on unhandled socket error or timeout."""
        now_epoch = time.time()
        conn = self._get_connection()
        in_tx = False
        try:
            conn.execute("BEGIN IMMEDIATE;")
            in_tx = True
            cur = conn.execute("SELECT state, budget_id, target_model FROM dispatch_reservations WHERE reservation_id = ?", (reservation_id,))
            row = cur.fetchone()
            if row is None:
                raise ReservationNotFoundError(f"Reservation '{reservation_id}' not found")
            current_state, budget_id, target_model = row[0], row[1], row[2]

            if current_state not in (STATE_RESERVED, STATE_SEND_COMMITTED):
                raise IllegalStateTransitionError(
                    f"Cannot mark UNKNOWN on reservation in terminal state '{current_state}'"
                )

            conn.execute("""
            UPDATE dispatch_reservations
            SET state = ?, completed_at_epoch = ?
            WHERE reservation_id = ?;
            """, (STATE_UNKNOWN, now_epoch, reservation_id))

            outbox_id = f"evt_unkn_{uuid.uuid4().hex[:12]}"
            conn.execute("""
            INSERT INTO outbox_events (
                event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, NULL);
            """, (
                outbox_id,
                reservation_id,
                budget_id,
                "DISPATCH_TERMINATED_UNKNOWN",
                json.dumps({
                    "reservation_id": reservation_id,
                    "target_model": target_model,
                    "reason": reason,
                    "at_epoch": now_epoch,
                }),
                now_epoch,
            ))

            conn.execute("COMMIT;")
            in_tx = False
        finally:
            if in_tx:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
            if not self._is_memory:
                conn.close()

    def recover_interrupted_reservations(self) -> List[str]:
        """P0.3 Conservative Crash Recovery:

        Finds any reservation left in SEND_COMMITTED without completed_at_epoch and
        transitions them immediately to UNKNOWN, permanently draining budget fail-closed.
        """
        now_epoch = time.time()
        conn = self._get_connection()
        recovered_ids: List[str] = []
        in_tx = False
        try:
            conn.execute("BEGIN IMMEDIATE;")
            in_tx = True
            cur = conn.execute("""
            SELECT reservation_id, budget_id, target_model
            FROM dispatch_reservations
            WHERE state = ? AND completed_at_epoch IS NULL
            """, (STATE_SEND_COMMITTED,))
            dangling = cur.fetchall()

            for res_id, b_id, model in dangling:
                conn.execute("""
                UPDATE dispatch_reservations
                SET state = ?, completed_at_epoch = ?
                WHERE reservation_id = ?;
                """, (STATE_UNKNOWN, now_epoch, res_id))

                outbox_id = f"evt_rec_unkn_{uuid.uuid4().hex[:12]}"
                conn.execute("""
                INSERT INTO outbox_events (
                    event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, NULL);
                """, (
                    outbox_id,
                    res_id,
                    b_id,
                    "CRASH_RECOVERY_UNKNOWN_DRAIN",
                    json.dumps({
                        "reservation_id": res_id,
                        "target_model": model,
                        "recovered_at_epoch": now_epoch,
                        "action": "Drained budget to UNKNOWN state to prevent duplicate transmission",
                    }),
                    now_epoch,
                ))
                recovered_ids.append(res_id)

            conn.execute("COMMIT;")
            in_tx = False
            return recovered_ids
        finally:
            if in_tx:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
            if not self._is_memory:
                conn.close()

    def get_reservation(self, reservation_id: str) -> Optional[DispatchReservation]:
        """Look up reservation by ID."""
        conn = self._get_connection()
        try:
            cur = conn.execute("""
            SELECT reservation_id, budget_id, logical_attempt_id, sequence_index,
                   idempotency_key, target_model, state, reserved_at_epoch,
                   send_committed_at_epoch, dispatched_at_epoch, completed_at_epoch,
                   http_status_code, reconciliation_state
            FROM dispatch_reservations
            WHERE reservation_id = ?
            """, (reservation_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return DispatchReservation(
                reservation_id=row[0],
                budget_id=row[1],
                logical_attempt_id=row[2],
                sequence_index=row[3],
                idempotency_key=row[4],
                target_model=row[5],
                state=row[6],
                reserved_at_epoch=row[7],
                send_committed_at_epoch=row[8],
                dispatched_at_epoch=row[9],
                completed_at_epoch=row[10],
                http_status_code=row[11],
                reconciliation_state=row[12],
            )
        finally:
            if not self._is_memory:
                conn.close()

    def get_budget_status(self, budget_id: str) -> Dict[str, Any]:
        """Return status summary of dispatches and quota consumed."""
        conn = self._get_connection()
        try:
            cur = conn.execute("""
            SELECT max_dispatches, allowed_models_json FROM global_dispatch_budgets WHERE budget_id = ?
            """, (budget_id,))
            b_row = cur.fetchone()
            if not b_row:
                raise QuotaSecurityError(f"Budget '{budget_id}' not found")
            max_dispatches, models_raw = b_row[0], b_row[1]

            cur = conn.execute("""
            SELECT state, COUNT(*) FROM dispatch_reservations
            WHERE budget_id = ?
            GROUP BY state
            """, (budget_id,))
            state_counts: Dict[str, int] = dict(cur.fetchall())

            consumed_total = sum(state_counts.get(s, 0) for s in VALID_RESERVATION_STATES)
            return {
                "budget_id": budget_id,
                "max_dispatches": max_dispatches,
                "consumed_total": consumed_total,
                "remaining_dispatches": max(0, max_dispatches - consumed_total),
                "is_exhausted": consumed_total >= max_dispatches,
                "state_breakdown": state_counts,
                "allowed_models": json.loads(models_raw),
            }
        finally:
            if not self._is_memory:
                conn.close()


# ---------------------------------------------------------------------------
# Transactional Outbox Projector (Projetor para pilot_audit.jsonl)
# ---------------------------------------------------------------------------

class TransactionalOutboxProjector:
    """Projects unprojected outbox events from SQLite into append-only JSONL with fsync."""

    def __init__(self, store: AtomicDispatchQuotaStore, ledger_path: str):
        self.store = store
        self.ledger_path = ledger_path

    def project_pending_events(self) -> int:
        """Read unprojected events, append to ledger file with fsync, and mark projected."""
        conn = self.store._get_connection()
        now_epoch = time.time()
        projected_count = 0
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("""
            SELECT event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch
            FROM outbox_events
            WHERE projected_at_epoch IS NULL
            ORDER BY created_at_epoch ASC
            """)
            pending = cur.fetchall()

            if not pending:
                conn.execute("ROLLBACK;")
                return 0

            # Ensure parent directory exists
            os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)), exist_ok=True)

            # Append to ledger with fsync
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                for ev_id, res_id, agg_id, ev_type, payload_str, created_epoch in pending:
                    record = {
                        "event_id": ev_id,
                        "reservation_id": res_id,
                        "aggregate_id": agg_id,
                        "event_type": ev_type,
                        "payload": json.loads(payload_str),
                        "created_at_epoch": created_epoch,
                        "projected_at_epoch": now_epoch,
                    }
                    f.write(json.dumps(record) + "\n")
                f.flush()
                os.fsync(f.fileno())

            # Mark all as projected in SQLite
            for ev_id, _, _, _, _, _ in pending:
                conn.execute("""
                UPDATE outbox_events
                SET projected_at_epoch = ?
                WHERE event_id = ?
                """, (now_epoch, ev_id))

            conn.execute("COMMIT;")
            return len(pending)
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            if not self.store._is_memory:
                conn.close()

    def verify_event_projected(self, event_id: str) -> bool:
        """Check whether specific outbox event has been durably projected to disk."""
        conn = self.store._get_connection()
        try:
            cur = conn.execute("SELECT projected_at_epoch FROM outbox_events WHERE event_id = ?", (event_id,))
            row = cur.fetchone()
            return row is not None and row[0] is not None
        finally:
            if not self.store._is_memory:
                conn.close()


# ---------------------------------------------------------------------------
# Governed Multi-Model Fallback Engine
# ---------------------------------------------------------------------------

class GovernedFallbackEngine:
    """Evaluates fallback contingency after an HTTP 429 response under strict governance."""

    def __init__(
        self,
        quota_store: AtomicDispatchQuotaStore,
        projector: TransactionalOutboxProjector,
        cost_guard: FreeCostGuard,
    ):
        self.store = quota_store
        self.projector = projector
        self.cost_guard = cost_guard

    def evaluate_fallback(
        self,
        budget_id: str,
        failed_reservation: DispatchReservation,
        allowed_models: Sequence[str],
        logical_attempt_id: str,
        catalog_registry: FreeModelRegistry,
    ) -> FallbackDecision:
        """Evaluate contingency candidates upon HTTP 429 from primary model.

        Policy:
        - If max_dispatches == 1: Recommend best alternative model in outbox ledger, ZERO network transmission.
        - If max_dispatches > 1 and remaining budget > 0: Atomically acquire reservation #2, commit SEND_COMMITTED, and authorize physical dispatch.
        - If max_dispatches > 1 and budget exhausted: Block further dispatch fail-closed.
        """
        budget_status = self.store.get_budget_status(budget_id)
        max_dispatches = budget_status["max_dispatches"]
        consumed = budget_status["consumed_total"]

        # 1. Identify contingency candidates from allowed_models excluding failed model
        failed_model = failed_reservation.target_model
        candidate_models = [m for m in allowed_models if m != failed_model]

        verified_candidates: List[str] = []
        for candidate in candidate_models:
            # Check candidate model in cost guard and registry
            if catalog_registry.is_model_free(candidate):
                entry = catalog_registry.get_model(candidate)
                if entry and any(m.value.upper() == "TEXT" for m in entry.modalities):
                    verified_candidates.append(candidate)

        if not verified_candidates:
            return FallbackDecision(
                action="NO_CANDIDATE",
                target_model=None,
                sequence_index=None,
                reservation_id=None,
                reason=f"No verified zero-cost alternative models available in allowed_models: {allowed_models}",
                details={"failed_model": failed_model, "allowed_models": list(allowed_models)},
            )

        selected_candidate = verified_candidates[0]

        # 2. Case: max_dispatches == 1 (Single Dispatch Safety Ceiling)
        if max_dispatches == 1:
            # Emit FALLBACK_PROPOSED_BLOCKED to outbox and project
            now_epoch = time.time()
            conn = self.store._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                outbox_id = f"evt_fb_prop_{uuid.uuid4().hex[:12]}"
                conn.execute("""
                INSERT INTO outbox_events (
                    event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, NULL);
                """, (
                    outbox_id,
                    failed_reservation.reservation_id,
                    budget_id,
                    "FALLBACK_PROPOSED_BLOCKED",
                    json.dumps({
                        "failed_model": failed_model,
                        "recommended_contingency_model": selected_candidate,
                        "all_verified_candidates": verified_candidates,
                        "action": "RECOMMENDED_ONLY",
                        "max_dispatches": 1,
                        "reason": "Single-dispatch budget limit (max_dispatches=1) prohibits automatic secondary dispatch",
                    }),
                    now_epoch,
                ))
                conn.execute("COMMIT;")
            finally:
                if not self.store._is_memory:
                    conn.close()

            self.projector.project_pending_events()

            return FallbackDecision(
                action="RECOMMENDED_ONLY",
                target_model=selected_candidate,
                sequence_index=None,
                reservation_id=None,
                reason="Single-dispatch budget limit (max_dispatches=1) prohibits automatic secondary dispatch",
                details={
                    "failed_model": failed_model,
                    "recommended_contingency_model": selected_candidate,
                    "candidates": verified_candidates,
                },
            )

        # 3. Case: max_dispatches > 1
        if consumed >= max_dispatches:
            # Budget exhausted
            now_epoch = time.time()
            conn = self.store._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                outbox_id = f"evt_fb_exh_{uuid.uuid4().hex[:12]}"
                conn.execute("""
                INSERT INTO outbox_events (
                    event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, NULL);
                """, (
                    outbox_id,
                    failed_reservation.reservation_id,
                    budget_id,
                    "FALLBACK_BUDGET_EXHAUSTED",
                    json.dumps({
                        "failed_model": failed_model,
                        "candidate_model": selected_candidate,
                        "consumed": consumed,
                        "max_dispatches": max_dispatches,
                    }),
                    now_epoch,
                ))
                conn.execute("COMMIT;")
            finally:
                if not self.store._is_memory:
                    conn.close()

            self.projector.project_pending_events()

            return FallbackDecision(
                action="BLOCKED_BUDGET",
                target_model=selected_candidate,
                sequence_index=None,
                reservation_id=None,
                reason=f"Multi-dispatch quota exhausted: {consumed}/{max_dispatches} consumed",
                details={"consumed": consumed, "max_dispatches": max_dispatches},
            )

        # 4. Multi-dispatch permitted: acquire reservation and advance to SEND_COMMITTED
        next_seq = failed_reservation.sequence_index + 1
        new_reservation = self.store.acquire_reservation(
            budget_id=budget_id,
            logical_attempt_id=logical_attempt_id,
            sequence_index=next_seq,
            target_model=selected_candidate,
        )
        self.store.commit_send(new_reservation.reservation_id)
        self.projector.project_pending_events()

        return FallbackDecision(
            action="DISPATCH_AUTHORIZED",
            target_model=selected_candidate,
            sequence_index=next_seq,
            reservation_id=new_reservation.reservation_id,
            reason=f"Secondary dispatch authorized for contingency model '{selected_candidate}' under quota ({consumed + 1}/{max_dispatches})",
            details={
                "previous_model": failed_model,
                "selected_model": selected_candidate,
                "reservation_id": new_reservation.reservation_id,
                "sequence_index": next_seq,
            },
        )
