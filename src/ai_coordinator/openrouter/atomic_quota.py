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
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from ai_coordinator.human_workspace import HumanPrincipal
from ai_coordinator.manifest_auth import (
    CanonicalAuthorizationManifest,
    HumanOperationalApproval,
    HumanOperationalAuthority,
    HumanOperationalAuthorizationError,
    KeyAuthorityError,
    PERMISSION_DISPATCH_OPENROUTER_FREE,
    ReplayAuthorizationError,
    SignedAuthorizationManifest,
    TripartiteAuthorizationVerifier,
    TrustStore,
)
from ai_coordinator.process import (
    PosixProcessController,
    WindowsProcessController,
    safe_process_alive,
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


class LedgerCorruptionError(QuotaSecurityError):
    """Raised when pilot_audit.jsonl ledger exhibits syntax corruption, tampering, or missing required fields."""


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

    def __init__(
        self,
        db_path: str = ":memory:",
        default_trust_store: Optional[TrustStore] = None,
        expected_commit: Optional[str] = None,
        human_authority: Optional[HumanOperationalAuthority] = None,
        default_human_principal: Optional[HumanPrincipal] = None,
        default_human_session: Optional[Union[HumanPrincipal, HumanOperationalApproval]] = None,
    ):
        self.db_path = db_path
        self._is_memory = db_path == ":memory:"
        self._conn: Optional[sqlite3.Connection] = None
        self.default_trust_store = default_trust_store
        self.expected_commit = expected_commit
        self.human_authority = human_authority or HumanOperationalAuthority()

        if isinstance(default_human_session, HumanPrincipal):
            self.default_human_principal = default_human_session
            self.default_human_approval = None
        elif isinstance(default_human_session, HumanOperationalApproval):
            self.default_human_approval = default_human_session
            self.default_human_principal = default_human_principal
        else:
            self.default_human_principal = default_human_principal
            self.default_human_approval = None

        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        if self._is_memory:
            if self._conn is None:
                self._conn = sqlite3.connect(
                    ":memory:",
                    isolation_level=None,  # autocommit / manual transactions
                    check_same_thread=False,
                )
                self._conn.execute("PRAGMA busy_timeout = 30000;")
                self._conn.execute("PRAGMA foreign_keys = ON;")
            return self._conn
        else:
            conn = sqlite3.connect(
                self.db_path,
                isolation_level=None,
                timeout=30.0,
                check_same_thread=False,
            )
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("PRAGMA foreign_keys = ON;")
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
        human_operator_consent: Optional[bool] = None,
        human_operator_identity: Optional[str] = None,
        human_session_id: Optional[str] = None,
        human_approval: Optional[Union[HumanOperationalApproval, Dict[str, Any]]] = None,
        check_time_utc: Optional[dt.datetime] = None,
    ) -> str:
        """Validate signed manifest using Tripartite Verifier and atomically register budget.

        Persists authorization_id in consumed_authorizations to guarantee cross-process,
        cross-restart anti-replay protection.
        Requires authenticated human operator authorization (HumanOperationalApproval).
        Isolated boolean flags without authenticated human session are strictly prohibited.
        """
        # 1. Enforce human consent and authenticated operator identity
        if human_operator_consent is False:
            raise HumanOperationalAuthorizationError(
                "Human operational consent is missing or false: explicit consent (True) is mandatory by operator"
            )

        resolved_approval: Optional[HumanOperationalApproval] = None
        if human_approval is not None:
            if isinstance(human_approval, HumanOperationalApproval):
                resolved_approval = human_approval
            elif isinstance(human_approval, dict):
                resolved_approval = HumanOperationalApproval.from_dict(human_approval)
            else:
                raise HumanOperationalAuthorizationError(
                    f"human_approval must be HumanOperationalApproval or dict, got: {type(human_approval)}"
                )
        elif self.default_human_principal is not None:
            if human_operator_consent is not True:
                raise HumanOperationalAuthorizationError(
                    "Human operational consent is missing or false: explicit consent (True) is mandatory by operator"
                )
            # Operator principal configured on store issues authentic approval for this exact manifest
            resolved_approval = self.human_authority.issue_approval(
                principal=self.default_human_principal,
                authorization_id=signed_manifest.manifest.authorization_id,
                at_time_utc=check_time_utc,
            )
        elif self.default_human_approval is not None:
            if human_operator_consent is not True and human_operator_consent is not None:
                raise HumanOperationalAuthorizationError(
                    "Human operational consent is missing or false: explicit consent (True) is mandatory by operator"
                )
            resolved_approval = self.default_human_approval
        else:
            raise HumanOperationalAuthorizationError(
                "Isolated boolean consent flag is prohibited: verifiable human operator identity and authenticated "
                "approval (HumanOperationalApproval) issued by HumanOperationalAuthority are mandatory to prove human authorization"
            )

        # 2. Cryptographic verification of human approval fail-closed
        self.human_authority.verify_approval(
            approval=resolved_approval,
            expected_authorization_id=signed_manifest.manifest.authorization_id,
            expected_operator_identity=signed_manifest.manifest.authorizer_identity,
            expected_purpose=PERMISSION_DISPATCH_OPENROUTER_FREE,
            at_time_utc=check_time_utc,
        )

        # 3. Enforce tripartite verification (Tiers 1, 2, and 3)
        verifier = TripartiteAuthorizationVerifier(trust_store=trust_store)
        res = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=expected_commit,
            requested_model=signed_manifest.manifest.allowed_models[0],
            requested_modality="text",
            requested_dispatches=signed_manifest.manifest.max_dispatches,
            human_operator_consent=True,
            at_time_utc=check_time_utc,
            check_replay=False,  # Replay is verified persistently via SQLite UNIQUE constraint below
            human_approval=resolved_approval,
            human_authority=self.human_authority,
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
                    "operator_identity": resolved_approval.operator_identity,
                    "session_token": resolved_approval.session_token,
                    "approval_token": resolved_approval.approval_token,
                    "valid_from_utc": resolved_approval.valid_from_utc,
                    "valid_until_utc": resolved_approval.valid_until_utc,
                    "purpose": resolved_approval.purpose,
                }, sort_keys=True),
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

    def commit_send(
        self,
        reservation_id: str,
        trust_store: Optional[TrustStore] = None,
        expected_commit: Optional[str] = None,
        at_time_utc: Optional[dt.datetime] = None,
    ) -> None:
        """Advance reservation from RESERVED to SEND_COMMITTED (Point of Irreversible Commitment - PIC).

        Revalidates critical authorization invariants at the PIC:
        1. Availability of verified Trust Store and code commit HEAD (mandatory).
        2. Temporal validity: manifest valid_until_utc must not be expired.
        3. Code commit: runtime HEAD must match manifest code_commit.
        4. Key authority: authorizer public key in Trust Store must not be revoked or expired.
        5. Model & scope: target_model must be in allowed_models.

        Persisted with fsync in SQLite BEFORE opening TLS socket.
        From this point onward, budget is permanently drained and cannot be returned.
        Fail-closed: any missing evidence, inconsistency, or invalidity transitions to FAILED and raises.
        """
        now_epoch = time.time()
        now_dt = at_time_utc or dt.datetime.now(dt.timezone.utc)
        effective_trust_store = trust_store or self.default_trust_store
        effective_commit = expected_commit or self.expected_commit

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

            # Revalidate critical authorization invariants at PIC from SQLite budget record
            cur = conn.execute("""
            SELECT valid_until_utc, code_commit, authorizer_identity, allowed_models_json, manifest_id
            FROM global_dispatch_budgets
            WHERE budget_id = ?
            """, (budget_id,))
            b_row = cur.fetchone()
            if b_row is None:
                raise QuotaSecurityError(f"Budget '{budget_id}' not found at PIC")
            valid_until_str, budget_code_commit, authorizer_id, allowed_models_raw, manifest_id = b_row[0], b_row[1], b_row[2], b_row[3], b_row[4]

            # 1. Mandatory Trust Store Check
            if effective_trust_store is None:
                conn.execute("UPDATE dispatch_reservations SET state = ?, completed_at_epoch = ? WHERE reservation_id = ?;", (STATE_FAILED, now_epoch, reservation_id))
                conn.execute("INSERT INTO outbox_events (event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch) VALUES (?, ?, ?, ?, ?, ?, NULL);",
                             (f"evt_pic_block_{uuid.uuid4().hex[:12]}", reservation_id, budget_id, "PIC_BLOCKED_MISSING_TRUST_STORE", json.dumps({"reason": "Trust Store is required at PIC but missing/unconfigured"}), now_epoch))
                conn.execute("COMMIT;")
                in_tx = False
                raise QuotaSecurityError("Commit send blocked at PIC: Trust Store is mandatory for key revalidation but missing or unconfigured")

            # 2. Mandatory Effective Commit Check
            if not effective_commit or not effective_commit.strip():
                conn.execute("UPDATE dispatch_reservations SET state = ?, completed_at_epoch = ? WHERE reservation_id = ?;", (STATE_FAILED, now_epoch, reservation_id))
                conn.execute("INSERT INTO outbox_events (event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch) VALUES (?, ?, ?, ?, ?, ?, NULL);",
                             (f"evt_pic_block_{uuid.uuid4().hex[:12]}", reservation_id, budget_id, "PIC_BLOCKED_MISSING_COMMIT", json.dumps({"reason": "Runtime code commit HEAD is required at PIC but missing/unconfigured"}), now_epoch))
                conn.execute("COMMIT;")
                in_tx = False
                raise QuotaSecurityError("Commit send blocked at PIC: runtime code commit is mandatory for commit verification but missing or unconfigured")

            # 3. Temporal revalidation
            try:
                exp_dt = dt.datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
                if now_dt > exp_dt:
                    conn.execute("UPDATE dispatch_reservations SET state = ?, completed_at_epoch = ? WHERE reservation_id = ?;", (STATE_FAILED, now_epoch, reservation_id))
                    conn.execute("INSERT INTO outbox_events (event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch) VALUES (?, ?, ?, ?, ?, ?, NULL);",
                                 (f"evt_pic_block_{uuid.uuid4().hex[:12]}", reservation_id, budget_id, "PIC_BLOCKED_MANIFEST_EXPIRED", json.dumps({"reason": f"Manifest expired at {valid_until_str}"}), now_epoch))
                    conn.execute("COMMIT;")
                    in_tx = False
                    raise QuotaSecurityError(f"Commit send blocked at PIC: manifest authorization expired at {valid_until_str}")
            except (ValueError, TypeError) as err:
                raise QuotaSecurityError(f"Malformed valid_until timestamp at PIC: {err}") from err

            # 4. Code commit revalidation
            if effective_commit.strip().lower() != budget_code_commit.strip().lower():
                conn.execute("UPDATE dispatch_reservations SET state = ?, completed_at_epoch = ? WHERE reservation_id = ?;", (STATE_FAILED, now_epoch, reservation_id))
                conn.execute("INSERT INTO outbox_events (event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch) VALUES (?, ?, ?, ?, ?, ?, NULL);",
                             (f"evt_pic_block_{uuid.uuid4().hex[:12]}", reservation_id, budget_id, "PIC_BLOCKED_COMMIT_MISMATCH", json.dumps({"reason": f"Commit divergence: {effective_commit} != {budget_code_commit}"}), now_epoch))
                conn.execute("COMMIT;")
                in_tx = False
                raise QuotaSecurityError(f"Commit send blocked at PIC: runtime commit '{effective_commit}' diverges from manifest commit '{budget_code_commit}'")

            # 5. Model revalidation
            allowed_models = json.loads(allowed_models_raw)
            if target_model not in allowed_models:
                conn.execute("UPDATE dispatch_reservations SET state = ?, completed_at_epoch = ? WHERE reservation_id = ?;", (STATE_FAILED, now_epoch, reservation_id))
                conn.execute("INSERT INTO outbox_events (event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch) VALUES (?, ?, ?, ?, ?, ?, NULL);",
                             (f"evt_pic_block_{uuid.uuid4().hex[:12]}", reservation_id, budget_id, "PIC_BLOCKED_MODEL_UNAUTHORIZED", json.dumps({"target_model": target_model, "allowed": allowed_models}), now_epoch))
                conn.execute("COMMIT;")
                in_tx = False
                raise QuotaSecurityError(f"Commit send blocked at PIC: target model '{target_model}' not authorized in {allowed_models}")

            # 6. Authorizer Key Identity & Trust Store revalidation
            cur = conn.execute("SELECT details_json FROM consumed_authorizations WHERE authorization_id = ?", (manifest_id,))
            c_row = cur.fetchone()
            key_id = None
            if c_row and c_row[0]:
                try:
                    key_id = json.loads(c_row[0]).get("key_id")
                except Exception:
                    pass

            if not key_id or not key_id.strip():
                conn.execute("UPDATE dispatch_reservations SET state = ?, completed_at_epoch = ? WHERE reservation_id = ?;", (STATE_FAILED, now_epoch, reservation_id))
                conn.execute("INSERT INTO outbox_events (event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch) VALUES (?, ?, ?, ?, ?, ?, NULL);",
                             (f"evt_pic_block_{uuid.uuid4().hex[:12]}", reservation_id, budget_id, "PIC_BLOCKED_MISSING_KEY_ID", json.dumps({"reason": "Authorizer key identity is missing or unverified"}), now_epoch))
                conn.execute("COMMIT;")
                in_tx = False
                raise KeyAuthorityError("Commit send blocked at PIC: authorizer key identity is missing or unverified")

            is_key_ok, key_reason = effective_trust_store.is_key_authorized(
                key_id=key_id,
                required_permission="dispatch:openrouter_free",
                at_time_utc=now_dt,
            )
            if not is_key_ok:
                conn.execute("UPDATE dispatch_reservations SET state = ?, completed_at_epoch = ? WHERE reservation_id = ?;", (STATE_FAILED, now_epoch, reservation_id))
                conn.execute("INSERT INTO outbox_events (event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch, projected_at_epoch) VALUES (?, ?, ?, ?, ?, ?, NULL);",
                             (f"evt_pic_block_{uuid.uuid4().hex[:12]}", reservation_id, budget_id, "PIC_BLOCKED_KEY_REVOKED", json.dumps({"key_id": key_id, "reason": key_reason}), now_epoch))
                conn.execute("COMMIT;")
                in_tx = False
                raise KeyAuthorityError(f"Commit send blocked at PIC: authorizer key '{key_id}' is revoked or expired: {key_reason}")

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
# Cross-Process File Lock for Safe JSONL Ledger Writes
# ---------------------------------------------------------------------------

def _get_process_create_time_ns(pid: int) -> Optional[int]:
    """Query OS kernel process creation time in Unix nanoseconds."""
    if pid <= 0:
        return None
    try:
        if os.name == "nt":
            return WindowsProcessController.get_creation_time(pid)
        else:
            return PosixProcessController.get_creation_time(pid)
    except Exception:
        return None


class _InterProcessFileLock:
    """Robust cross-process file lock using native OS kernel locking on a permanent lock file.

    Guarantees:
    - Eliminates TOCTOU race conditions: no os.unlink() between inspection and deletion.
    - Uses OS kernel-level byte/file locking (msvcrt.locking on Windows, fcntl.flock on POSIX).
    - Automatic kernel-level release: when a process terminates or crashes unexpectedly, the
      OS kernel immediately releases the file lock upon handle closure.
    - Mutual exclusion is guaranteed at the kernel level: zero window where two processes can
      both acquire or believe they own the lock.
    - Writes owner metadata (PID, kernel create_time_ns, timestamp) for telemetry, diagnostics,
      and fail-closed timeout reporting.
    """

    def __init__(self, lock_path: str, timeout_sec: float = 10.0, retry_delay_sec: float = 0.02):
        self.lock_path = lock_path
        self.timeout_sec = timeout_sec
        self.retry_delay_sec = retry_delay_sec
        self._fd: Optional[int] = None
        self._acquired = False

    def __enter__(self) -> "_InterProcessFileLock":
        start = time.time()
        my_pid = os.getpid()
        my_create_time_ns = _get_process_create_time_ns(my_pid)

        # Ensure parent directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self.lock_path)), exist_ok=True)
        self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR)
        try:
            while True:
                try:
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                    # Exclusive lock acquired via OS kernel!
                    payload = json.dumps({
                        "pid": my_pid,
                        "create_time_ns": my_create_time_ns,
                        "acquired_at_epoch": time.time(),
                    }).encode("utf-8")
                    os.lseek(self._fd, 1, os.SEEK_SET)
                    os.write(self._fd, payload)
                    os.ftruncate(self._fd, 1 + len(payload))
                    os.fsync(self._fd)
                    self._acquired = True
                    return self
                except (OSError, PermissionError):
                    # Locked by another active process; inspect owner metadata
                    owner_pid: Optional[int] = None
                    owner_create_time_ns: Optional[int] = None
                    try:
                        os.lseek(self._fd, 1, os.SEEK_SET)
                        raw = os.read(self._fd, 512).strip()
                        if raw:
                            data = json.loads(raw.decode("utf-8"))
                            if isinstance(data, dict):
                                owner_pid = data.get("pid")
                                owner_create_time_ns = data.get("create_time_ns")
                    except Exception:
                        pass

                    if time.time() - start > self.timeout_sec:
                        if owner_pid is not None and isinstance(owner_pid, int):
                            owner_desc = f"active PID {owner_pid} (create_time_ns={owner_create_time_ns})"
                        else:
                            owner_desc = "unknown/indeterminate owner"
                        raise TimeoutError(
                            f"Timed out waiting for inter-process lock '{self.lock_path}' "
                            f"(held by {owner_desc})"
                        )
                    time.sleep(self.retry_delay_sec)
        except Exception:
            if not self._acquired and self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._acquired and self._fd is not None:
            try:
                # Truncate metadata
                os.lseek(self._fd, 1, os.SEEK_SET)
                os.ftruncate(self._fd, 1)
                os.fsync(self._fd)
            except OSError:
                pass
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            self._acquired = False


# ---------------------------------------------------------------------------
# Transactional Outbox Projector (Projetor para pilot_audit.jsonl)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LedgerRecoveryResult:
    """Outcome of ledger quarantine and recovery operation."""
    recovered_count: int
    quarantined_count: int
    quarantine_path: str
    full_backup_path: str

    def __iter__(self):
        return iter((self.recovered_count, self.quarantined_count, self.quarantine_path, self.full_backup_path))

    def __getitem__(self, idx: int) -> Any:
        return (self.recovered_count, self.quarantined_count, self.quarantine_path, self.full_backup_path)[idx]

    def __len__(self) -> int:
        return 4


class TransactionalOutboxProjector:
    """Projects unprojected outbox events from SQLite into append-only JSONL with fsync and integrity validation."""

    def __init__(self, store: AtomicDispatchQuotaStore, ledger_path: str):
        self.store = store
        self.ledger_path = ledger_path

    def verify_ledger_integrity(self) -> Tuple[bool, List[str], Set[str]]:
        """Inspect JSONL ledger file for structural, JSON, and schema integrity.

        Returns (is_valid, error_list, existing_event_ids).
        Fail-closed: returns is_valid=False on any unparseable line, missing required fields, or corruption.
        """
        if not os.path.exists(self.ledger_path):
            return True, [], set()

        errors: List[str] = []
        valid_ids: Set[str] = set()
        line_num = 0

        with open(self.ledger_path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line_num += 1
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                    if not isinstance(record, dict):
                        errors.append(f"Line {line_num}: JSON root is not an object ({type(record).__name__})")
                        continue
                    if "event_id" not in record or not record["event_id"]:
                        errors.append(f"Line {line_num}: missing or empty 'event_id'")
                        continue
                    if "event_type" not in record:
                        errors.append(f"Line {line_num}: missing 'event_type'")
                        continue
                    ev_id = str(record["event_id"])
                    if ev_id in valid_ids:
                        errors.append(f"Line {line_num}: duplicate event_id '{ev_id}' found in ledger")
                        continue
                    valid_ids.add(ev_id)
                except json.JSONDecodeError as err:
                    errors.append(f"Line {line_num}: invalid JSON syntax ({err})")

        return len(errors) == 0, errors, valid_ids

    def recover_and_quarantine_corrupt_ledger(self) -> LedgerRecoveryResult:
        """Explicit verified recovery tool: preserves raw full copy, quarantines corrupted lines,
        and rewrites valid audit records.

        Synchronized via _InterProcessFileLock across processes.
        Returns LedgerRecoveryResult(recovered_count, quarantined_count, quarantine_path, full_backup_path).
        """
        if not os.path.exists(self.ledger_path):
            return LedgerRecoveryResult(0, 0, "", "")

        lock_path = f"{self.ledger_path}.lock"
        os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)), exist_ok=True)

        with _InterProcessFileLock(lock_path):
            ts_str = int(time.time())
            uid_str = uuid.uuid4().hex[:6]
            full_backup_path = f"{self.ledger_path}.full_backup_{ts_str}_{uid_str}.bak"
            quarantine_path = f"{self.ledger_path}.corrupt_{ts_str}_{uid_str}.bak"

            # 1. Preserve 100% full raw byte-for-byte copy of the corrupted ledger with fsync
            with open(self.ledger_path, "rb") as src_f, open(full_backup_path, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
                dst_f.flush()
                os.fsync(dst_f.fileno())

            # 2. Parse lines to identify valid records vs corrupted lines
            recovered_records: List[Dict[str, Any]] = []
            quarantined_lines: List[Tuple[int, str]] = []
            line_num = 0

            with open(self.ledger_path, "r", encoding="utf-8", errors="replace") as f:
                for raw_line in f:
                    line_num += 1
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                        if isinstance(record, dict) and "event_id" in record and record["event_id"] and "event_type" in record:
                            recovered_records.append(record)
                        else:
                            quarantined_lines.append((line_num, raw_line))
                    except Exception:
                        quarantined_lines.append((line_num, raw_line))

            # 3. Write quarantined lines with fsync
            if quarantined_lines:
                with open(quarantine_path, "w", encoding="utf-8") as qf:
                    for ln, ltxt in quarantined_lines:
                        qf.write(f"LINE {ln}: {ltxt}")
                    qf.flush()
                    os.fsync(qf.fileno())

            # 4. Rewrite ledger atomically with only valid records
            temp_ledger = f"{self.ledger_path}.tmp_{uuid.uuid4().hex[:8]}"
            with open(temp_ledger, "w", encoding="utf-8") as f:
                for rec in recovered_records:
                    f.write(json.dumps(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_ledger, self.ledger_path)

            return LedgerRecoveryResult(
                recovered_count=len(recovered_records),
                quarantined_count=len(quarantined_lines),
                quarantine_path=quarantine_path if quarantined_lines else "",
                full_backup_path=full_backup_path,
            )

    def project_pending_events(self) -> int:
        """Read unprojected events, append to ledger file with fsync idempotently, and mark projected.

        Protected by _InterProcessFileLock across verification, file append, and SQLite mark-as-projected.
        Fail-closed: if ledger corruption is detected, raises LedgerCorruptionError
        and refuses to append onto corrupted evidence until verified recovery.
        """
        lock_path = f"{self.ledger_path}.lock"
        os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)), exist_ok=True)

        with _InterProcessFileLock(lock_path):
            # 1. Enforce ledger integrity check fail-closed UNDER LOCK
            is_ok, errors, existing_ids = self.verify_ledger_integrity()
            if not is_ok:
                raise LedgerCorruptionError(
                    f"Ledger corruption detected in '{self.ledger_path}' ({len(errors)} error(s)): "
                    f"{'; '.join(errors[:3])}. Projection blocked fail-closed until verified recovery."
                )

            conn = self.store._get_connection()
            now_epoch = time.time()
            in_tx = False
            try:
                conn.execute("BEGIN IMMEDIATE;")
                in_tx = True
                cur = conn.execute("""
                SELECT event_id, reservation_id, aggregate_id, event_type, payload_json, created_at_epoch
                FROM outbox_events
                WHERE projected_at_epoch IS NULL
                ORDER BY created_at_epoch ASC
                """)
                pending = cur.fetchall()

                if not pending:
                    conn.execute("COMMIT;")
                    in_tx = False
                    return 0

                # Deduplicate against already projected events in JSONL file (Crash recovery safety)
                new_events = [p for p in pending if p[0] not in existing_ids]

                if new_events:
                    with open(self.ledger_path, "a", encoding="utf-8") as f:
                        for ev_id, res_id, agg_id, ev_type, payload_str, created_epoch in new_events:
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

                # Mark all as projected in SQLite (both newly appended and previously flushed)
                for ev_id, _, _, _, _, _ in pending:
                    conn.execute("""
                    UPDATE outbox_events
                    SET projected_at_epoch = ?
                    WHERE event_id = ?
                    """, (now_epoch, ev_id))

                conn.execute("COMMIT;")
                in_tx = False
                return len(pending)
            finally:
                if in_tx:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
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
        default_trust_store: Optional[TrustStore] = None,
        expected_commit: Optional[str] = None,
    ):
        self.store = quota_store
        self.projector = projector
        self.cost_guard = cost_guard
        self.default_trust_store = default_trust_store or quota_store.default_trust_store
        self.expected_commit = expected_commit or quota_store.expected_commit

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
        # 1. Provenance and Correlation Verification
        if failed_reservation.budget_id != budget_id:
            raise QuotaSecurityError(
                f"Fallback rejected: failed reservation budget_id '{failed_reservation.budget_id}' "
                f"diverges from requested budget_id '{budget_id}'"
            )
        if failed_reservation.logical_attempt_id != logical_attempt_id:
            raise QuotaSecurityError(
                f"Fallback rejected: failed reservation logical_attempt_id '{failed_reservation.logical_attempt_id}' "
                f"diverges from requested logical_attempt_id '{logical_attempt_id}'"
            )

        stored_res = self.store.get_reservation(failed_reservation.reservation_id)
        if stored_res is None:
            raise QuotaSecurityError(
                f"Fallback rejected: failed reservation '{failed_reservation.reservation_id}' does not exist in store"
            )

        # Check divergence between supplied reservation and persisted reservation
        if (
            failed_reservation.budget_id != stored_res.budget_id
            or failed_reservation.sequence_index != stored_res.sequence_index
            or failed_reservation.target_model != stored_res.target_model
            or failed_reservation.logical_attempt_id != stored_res.logical_attempt_id
        ):
            raise QuotaSecurityError(
                f"Fallback rejected: supplied reservation metadata diverges from persisted store record for reservation '{failed_reservation.reservation_id}'"
            )

        if failed_reservation.http_status_code is not None and failed_reservation.http_status_code != stored_res.http_status_code:
            raise QuotaSecurityError(
                f"Fallback rejected: supplied HTTP status ({failed_reservation.http_status_code}) diverges from persisted HTTP status ({stored_res.http_status_code})"
            )

        if failed_reservation.state == STATE_UNKNOWN or stored_res.state == STATE_UNKNOWN:
            raise QuotaSecurityError(
                f"Fallback rejected: reservation '{failed_reservation.reservation_id}' is in UNKNOWN state "
                "(ambiguous outcome prohibits automatic fallback)"
            )

        # Strictly require state == STATE_FAILED and http_status_code in (429, 503)
        if stored_res.state != STATE_FAILED:
            raise QuotaSecurityError(
                f"Fallback rejected: reservation '{failed_reservation.reservation_id}' is in state '{stored_res.state}', "
                f"only '{STATE_FAILED}' with proven HTTP 429/503 is eligible (UNKNOWN or other states prohibit automatic fallback)"
            )
        if stored_res.http_status_code not in (429, 503):
            raise QuotaSecurityError(
                f"Fallback rejected: HTTP status code is {stored_res.http_status_code}, "
                f"only proven HTTP 429 and 503 are eligible fallback triggers"
            )

        budget_status = self.store.get_budget_status(budget_id)
        max_dispatches = budget_status["max_dispatches"]
        consumed = budget_status["consumed_total"]

        # 2. Identify contingency candidates from allowed_models excluding failed model
        failed_model = failed_reservation.target_model
        candidate_models = [m for m in allowed_models if m != failed_model]

        verified_candidates: List[str] = []
        for candidate in candidate_models:
            try:
                # Enforce FreeCostGuard authorization with catalog freshness
                entry = self.cost_guard.authorize_model_execution(candidate, verify_freshness=True)
                if any(m.value.upper() == "TEXT" for m in entry.modalities):
                    verified_candidates.append(candidate)
            except Exception:
                # Candidate model is paid, unverified, stale, or lacks :free suffix
                continue

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

        # 3. Case: max_dispatches == 1 (Single Dispatch Safety Ceiling)
        if max_dispatches == 1:
            # Emit FALLBACK_PROPOSED_BLOCKED to outbox and project
            now_epoch = time.time()
            conn = self.store._get_connection()
            in_tx = False
            try:
                conn.execute("BEGIN IMMEDIATE;")
                in_tx = True
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
                in_tx = False
            finally:
                if in_tx:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
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

        # 4. Case: max_dispatches > 1
        if consumed >= max_dispatches:
            # Budget exhausted
            now_epoch = time.time()
            conn = self.store._get_connection()
            in_tx = False
            try:
                conn.execute("BEGIN IMMEDIATE;")
                in_tx = True
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
                in_tx = False
            finally:
                if in_tx:
                    try:
                        conn.execute("ROLLBACK;")
                    except Exception:
                        pass
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

        # 5. Multi-dispatch permitted: acquire reservation and advance to SEND_COMMITTED
        next_seq = failed_reservation.sequence_index + 1
        new_reservation = self.store.acquire_reservation(
            budget_id=budget_id,
            logical_attempt_id=logical_attempt_id,
            sequence_index=next_seq,
            target_model=selected_candidate,
        )
        self.store.commit_send(
            reservation_id=new_reservation.reservation_id,
            trust_store=self.default_trust_store,
            expected_commit=self.expected_commit,
        )
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
