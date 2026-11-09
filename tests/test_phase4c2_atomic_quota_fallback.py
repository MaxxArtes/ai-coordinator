"""Comprehensive Offline Adversarial Test Suite for Phase 4C.2.3:
Atomic Quota Store, Transactional Outbox, and Governed Multi-Model Fallback.

Validates:
1. P0.1 — Local At-Most-Once Guarantee & Point of Irreversible Commitment (SEND_COMMITTED).
2. P0.2 — Audit Atomicity via SQLite Transactional Outbox projected with fsync to JSONL.
3. P0.3 — Finite State Machine and Conservative Crash Recovery (UNKNOWN fail-closed state).
4. Persistent Anti-Replay Ledger bound to Ed25519 Canonical Manifests.
5. Governed Multi-Model Fallback Engine (max_dispatches=1 vs max_dispatches>1).

All tests execute strictly offline with zero external network sockets.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

from ai_coordinator.manifest_auth import (
    KEY_STATUS_ACTIVE,
    PERMISSION_AUDIT_VERIFY,
    PERMISSION_DISPATCH_OPENROUTER_FREE,
    CanonicalAuthorizationManifest,
    Ed25519Signer,
    HumanOperationalApproval,
    HumanOperationalAuthority,
    HumanOperationalAuthorizationError,
    KeyAuthorityError,
    SignedAuthorizationManifest,
    TrustStore,
    TrustedKeyEntry,
)
from ai_coordinator.openrouter.atomic_quota import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_RESERVED,
    STATE_SEND_COMMITTED,
    STATE_UNKNOWN,
    AtomicDispatchQuotaStore,
    BudgetExhaustedError,
    GovernedFallbackEngine,
    IllegalStateTransitionError,
    PersistentReplayAttackError,
    QuotaSecurityError,
    ReservationNotFoundError,
    TransactionalOutboxProjector,
)
from ai_coordinator.openrouter.cost_guard import FreeCostGuard
from ai_coordinator.openrouter.registry import (
    FreeModelRegistry,
    ModelCatalogEntry,
    ModelModality,
    ModelPricing,
)


class TestPhase4C2AtomicQuotaAndFallback(unittest.TestCase):
    """Test suite for Phase 4C.2.3 atomic quota, outbox, and governed fallback."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="ai_coord_4c23_test_")
        self.db_path = os.path.join(self.tmp_dir, "test_pilot_quotas.db")
        self.ledger_path = os.path.join(self.tmp_dir, "test_pilot_audit.jsonl")
        self.trust_store_path = os.path.join(self.tmp_dir, "test_trust_store.json")

        self.valid_commit = "1a0adb93ad5c4ebe755beb3d6b07dccfc43eeb92"

        # Signer and Trust Store
        self.operator_key_id = "ed25519:operator_test_4c23"
        self.signer = Ed25519Signer.generate(key_id=self.operator_key_id)

        self.trust_store = TrustStore(store_path=self.trust_store_path)
        self.now_utc = dt.datetime.now(dt.timezone.utc)
        self.trust_store.register_key(
            TrustedKeyEntry(
                key_id=self.operator_key_id,
                public_key_hex=self.signer.public_key_hex,
                identity="security_operator_root",
                permissions=[PERMISSION_DISPATCH_OPENROUTER_FREE, PERMISSION_AUDIT_VERIFY],
                status=KEY_STATUS_ACTIVE,
                created_at_utc=self.now_utc.isoformat(),
                expires_at_utc=(self.now_utc + dt.timedelta(days=30)).isoformat(),
            )
        )
        self.trust_store.save_to_file()

        # Build mock FreeModelRegistry and FreeCostGuard
        self.catalog = FreeModelRegistry()
        raw_catalog = {
            "data": [
                {
                    "id": "liquid/lfm-3-1.7b:free",
                    "name": "Liquid LFM 3 1.7B (Free)",
                    "architecture": {"modality": "text->text"},
                    "context_length": 32768,
                    "pricing": {"prompt": "0", "completion": "0", "request": "0", "image": "0"},
                },
                {
                    "id": "meta-llama/llama-3.2-3b-instruct:free",
                    "name": "Llama 3.2 3B Instruct (Free)",
                    "architecture": {"modality": "text->text"},
                    "context_length": 131072,
                    "pricing": {"prompt": "0", "completion": "0", "request": "0", "image": "0"},
                },
            ]
        }
        self.catalog.load_catalog_data(raw_catalog, epoch=self.now_utc.timestamp())
        self.cost_guard = FreeCostGuard(registry=self.catalog)

        # Human Authority, Principal, Store, and Projector
        self.human_authority = HumanOperationalAuthority()
        self.human_principal = self.human_authority.issue_session(owner_id="security_operator_root")
        self.store = AtomicDispatchQuotaStore(
            db_path=self.db_path,
            default_trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_authority=self.human_authority,
            default_human_principal=self.human_principal,
        )
        self.projector = TransactionalOutboxProjector(store=self.store, ledger_path=self.ledger_path)
        self.fallback_engine = GovernedFallbackEngine(
            quota_store=self.store,
            projector=self.projector,
            cost_guard=self.cost_guard,
            default_trust_store=self.trust_store,
            expected_commit=self.valid_commit,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _sign_manifest(
        self,
        auth_id: str = "auth-4c23-001",
        max_dispatches: int = 1,
        models: Optional[Sequence[str]] = None,
        commit: Optional[str] = None,
    ) -> SignedAuthorizationManifest:
        manifest = CanonicalAuthorizationManifest(
            authorization_id=auth_id,
            execution_scope_id="scope-4c23-pilot",
            code_commit=commit or self.valid_commit,
            allowed_models=tuple(models or ["liquid/lfm-3-1.7b:free", "meta-llama/llama-3.2-3b-instruct:free"]),
            allowed_modalities=("text",),
            max_dispatches=max_dispatches,
            max_budget_usd=0.0,
            authorizer_identity="security_operator_root",
            authorizer_permissions=(PERMISSION_DISPATCH_OPENROUTER_FREE,),
            issued_at_utc=(self.now_utc - dt.timedelta(minutes=5)).isoformat(),
            valid_until_utc=(self.now_utc + dt.timedelta(hours=2)).isoformat(),
        )
        return self.signer.sign_manifest(manifest)

    # =======================================================================
    # SECTION 1: Persistent Anti-Replay & Tripartite Integration
    # =======================================================================

    def test_01_persistent_anti_replay_prevents_duplicate_manifest_registration(self) -> None:
        """Enforces persistent anti-replay: registering the same authorization_id twice fails."""
        signed = self._sign_manifest(auth_id="auth-unique-100")
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        self.assertTrue(b_id.startswith("budget_"))

        # Second registration in same instance must fail
        with self.assertRaises(PersistentReplayAttackError) as ctx:
            self.store.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=True,
            )
        self.assertIn("replay attack prevented", str(ctx.exception).lower())

        # Simulate restart: fresh store instance on same DB file must still reject!
        new_store_instance = AtomicDispatchQuotaStore(
            db_path=self.db_path,
            default_trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_authority=self.human_authority,
            default_human_principal=self.human_principal,
        )
        with self.assertRaises(PersistentReplayAttackError) as ctx:
            new_store_instance.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=True,
            )
        self.assertIn("replay attack prevented", str(ctx.exception).lower())

    def test_02_budget_registration_rejects_missing_human_consent(self) -> None:
        """Enforce Tier 3: Budget registration fails if human_operator_consent is False."""
        signed = self._sign_manifest(auth_id="auth-no-consent")
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            self.store.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=False,  # missing consent
            )
        self.assertIn("Human operational consent is missing", str(ctx.exception))

    def test_03_budget_registration_rejects_commit_divergence(self) -> None:
        """Enforce Tier 3: Budget registration fails if runtime commit differs from manifest."""
        signed = self._sign_manifest(auth_id="auth-commit-divergence")
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            self.store.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit="0000000000000000000000000000000000000000",
                human_operator_consent=True,
            )
        self.assertIn("commit mismatch", str(ctx.exception).lower())

    # =======================================================================
    # SECTION 2: Local At-Most-Once Guarantee & Irreversible Commitment
    # =======================================================================

    def test_04_local_at_most_once_max_dispatches_one(self) -> None:
        """P0.1 Local At-Most-Once: max_dispatches=1 allows exactly 1 reservation."""
        signed = self._sign_manifest(auth_id="auth-n1-strict", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )

        # 1. Acquire reservation #1
        res1 = self.store.acquire_reservation(
            budget_id=b_id,
            logical_attempt_id="attempt_001",
            sequence_index=0,
            target_model="liquid/lfm-3-1.7b:free",
        )
        self.assertEqual(res1.state, STATE_RESERVED)
        self.assertEqual(res1.target_model, "liquid/lfm-3-1.7b:free")

        # 2. Acquire reservation #2 fails with BudgetExhaustedError
        with self.assertRaises(BudgetExhaustedError) as ctx:
            self.store.acquire_reservation(
                budget_id=b_id,
                logical_attempt_id="attempt_001",
                sequence_index=1,
                target_model="meta-llama/llama-3.2-3b-instruct:free",
            )
        self.assertIn("exhausted", str(ctx.exception).lower())

    def test_05_point_of_irreversible_commitment_send_committed(self) -> None:
        """P0.1 PIC: commit_send transitions state to SEND_COMMITTED and emits outbox event."""
        signed = self._sign_manifest(auth_id="auth-pic-commit", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_pic", 0, "liquid/lfm-3-1.7b:free")

        # Commit send before opening socket
        self.store.commit_send(res.reservation_id)

        # Verify state in DB
        updated = self.store.get_reservation(res.reservation_id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.state, STATE_SEND_COMMITTED)
        self.assertIsNotNone(updated.send_committed_at_epoch)

        # Trying to commit send again raises IllegalStateTransitionError
        with self.assertRaises(IllegalStateTransitionError):
            self.store.commit_send(res.reservation_id)

    def test_06_state_machine_transitions_completed_and_failed(self) -> None:
        """State transitions from SEND_COMMITTED to COMPLETED or FAILED."""
        signed = self._sign_manifest(auth_id="auth-state-mach", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )

        # 1. Success path: HTTP 200 -> COMPLETED
        res1 = self.store.acquire_reservation(b_id, "att_sm1", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        self.store.record_completion(res1.reservation_id, http_status_code=200)

        up1 = self.store.get_reservation(res1.reservation_id)
        self.assertEqual(up1.state, STATE_COMPLETED)
        self.assertEqual(up1.http_status_code, 200)

        # 2. Failure path: HTTP 429 -> FAILED
        res2 = self.store.acquire_reservation(b_id, "att_sm2", 1, "meta-llama/llama-3.2-3b-instruct:free")
        self.store.commit_send(res2.reservation_id)
        self.store.record_completion(res2.reservation_id, http_status_code=429)

        up2 = self.store.get_reservation(res2.reservation_id)
        self.assertEqual(up2.state, STATE_FAILED)
        self.assertEqual(up2.http_status_code, 429)

    # =======================================================================
    # SECTION 3: Transactional Outbox & Persistent JSONL Projection
    # =======================================================================

    def test_07_transactional_outbox_projects_durably_with_fsync(self) -> None:
        """P0.2 Outbox Pattern: Events in SQLite projected to pilot_audit.jsonl with fsync."""
        signed = self._sign_manifest(auth_id="auth-outbox-fsync", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_outbox", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res.reservation_id)
        self.store.record_completion(res.reservation_id, http_status_code=200)

        # Before projection, file does not exist or has 0 projected
        projected_count = self.projector.project_pending_events()
        self.assertGreaterEqual(projected_count, 3)  # BUDGET_REGISTERED, DISPATCH_RESERVED, SEND_COMMITTED, COMPLETED

        # Verify ledger file exists and contains valid JSON lines
        self.assertTrue(os.path.exists(self.ledger_path))
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), projected_count)
        types = [l["event_type"] for l in lines]
        self.assertIn("BUDGET_REGISTERED", types)
        self.assertIn("DISPATCH_RESERVED", types)
        self.assertIn("SEND_COMMITTED", types)
        self.assertIn("DISPATCH_COMPLETED_200", types)

        # Subsequent call is idempotent (0 new lines)
        self.assertEqual(self.projector.project_pending_events(), 0)

    # =======================================================================
    # SECTION 4: Conservative Crash Recovery & UNKNOWN State (P0.3)
    # =======================================================================

    def test_08_conservative_crash_recovery_drains_unclosed_reservations_to_unknown(self) -> None:
        """P0.3 Crash Recovery: Dangling SEND_COMMITTED reservations drained to UNKNOWN."""
        signed = self._sign_manifest(auth_id="auth-crash-test", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_crash", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res.reservation_id)

        # Simulate process death: process killed immediately after commit_send (no response)
        # Restart simulation: fresh store instance calls recover_interrupted_reservations()
        recovery_store = AtomicDispatchQuotaStore(db_path=self.db_path)
        recovered = recovery_store.recover_interrupted_reservations()

        self.assertEqual(recovered, [res.reservation_id])

        # Reservation is now permanently in UNKNOWN state
        up = recovery_store.get_reservation(res.reservation_id)
        self.assertEqual(up.state, STATE_UNKNOWN)

        # Budget is permanently consumed (fail-closed)
        status = recovery_store.get_budget_status(b_id)
        self.assertEqual(status["consumed_total"], 1)
        self.assertTrue(status["is_exhausted"])

        # Any attempt to retransmit or complete the UNKNOWN reservation raises error
        with self.assertRaises(IllegalStateTransitionError):
            recovery_store.record_completion(res.reservation_id, http_status_code=200)

    def test_09_direct_failure_to_unknown_on_socket_timeout(self) -> None:
        """Unhandled socket error transitions reservation to UNKNOWN and drains budget."""
        signed = self._sign_manifest(auth_id="auth-timeout-test", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_to", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res.reservation_id)

        self.store.record_failure_unknown(res.reservation_id, reason="urllib.error.URLError: Connection timed out")

        up = self.store.get_reservation(res.reservation_id)
        self.assertEqual(up.state, STATE_UNKNOWN)
        status = self.store.get_budget_status(b_id)
        self.assertTrue(status["is_exhausted"])

    # =======================================================================
    # SECTION 5: Governed Multi-Model Fallback Engine
    # =======================================================================

    def test_10_fallback_level_1_max_dispatches_one_recommends_only(self) -> None:
        """Fallback Level 1: When max_dispatches=1 and model 1 fails 429, emits RECOMMEND_ONLY and zero dispatches."""
        signed = self._sign_manifest(auth_id="auth-fb-lvl1", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_fb1", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res.reservation_id)
        self.store.record_completion(res.reservation_id, http_status_code=429)

        # Trigger fallback evaluation
        decision = self.fallback_engine.evaluate_fallback(
            budget_id=b_id,
            failed_reservation=res,
            allowed_models=signed.manifest.allowed_models,
            logical_attempt_id="att_fb1",
            catalog_registry=self.catalog,
        )

        self.assertEqual(decision.action, "RECOMMENDED_ONLY")
        self.assertEqual(decision.target_model, "meta-llama/llama-3.2-3b-instruct:free")
        self.assertIsNone(decision.reservation_id)
        self.assertIn("Single-dispatch budget limit", decision.reason)

        # Verify Outbox projected event FALLBACK_PROPOSED_BLOCKED
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        prop_events = [l for l in lines if l["event_type"] == "FALLBACK_PROPOSED_BLOCKED"]
        self.assertEqual(len(prop_events), 1)
        self.assertEqual(prop_events[0]["payload"]["recommended_contingency_model"], "meta-llama/llama-3.2-3b-instruct:free")

    def test_11_fallback_level_2_max_dispatches_two_authorizes_secondary(self) -> None:
        """Fallback Level 2: When max_dispatches=2 and model 1 fails 429, authorizes secondary dispatch."""
        signed = self._sign_manifest(auth_id="auth-fb-lvl2", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res1 = self.store.acquire_reservation(b_id, "att_fb2", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        self.store.record_completion(res1.reservation_id, http_status_code=429)

        # Trigger fallback evaluation
        decision = self.fallback_engine.evaluate_fallback(
            budget_id=b_id,
            failed_reservation=res1,
            allowed_models=signed.manifest.allowed_models,
            logical_attempt_id="att_fb2",
            catalog_registry=self.catalog,
        )

        self.assertEqual(decision.action, "DISPATCH_AUTHORIZED")
        self.assertEqual(decision.target_model, "meta-llama/llama-3.2-3b-instruct:free")
        self.assertEqual(decision.sequence_index, 1)
        self.assertIsNotNone(decision.reservation_id)

        # Check reservation state in DB is already SEND_COMMITTED
        res2 = self.store.get_reservation(decision.reservation_id)
        self.assertEqual(res2.state, STATE_SEND_COMMITTED)
        self.assertEqual(res2.target_model, "meta-llama/llama-3.2-3b-instruct:free")

        # Budget is now at 2/2 consumed
        status = self.store.get_budget_status(b_id)
        self.assertEqual(status["consumed_total"], 2)
        self.assertTrue(status["is_exhausted"])

    def test_12_fallback_level_2_blocked_when_budget_fully_exhausted(self) -> None:
        """Fallback Level 2: When max_dispatches=2 and both 1 and 2 fail, third dispatch is BLOCKED_BUDGET."""
        signed = self._sign_manifest(auth_id="auth-fb-exh", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        # Reservation 1
        res1 = self.store.acquire_reservation(b_id, "att_exh", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        self.store.record_completion(res1.reservation_id, http_status_code=429)

        # Reservation 2
        res2 = self.store.acquire_reservation(b_id, "att_exh", 1, "meta-llama/llama-3.2-3b-instruct:free")
        self.store.commit_send(res2.reservation_id)
        self.store.record_completion(res2.reservation_id, http_status_code=429)

        # Attempting fallback after both have failed
        decision = self.fallback_engine.evaluate_fallback(
            budget_id=b_id,
            failed_reservation=res2,
            allowed_models=signed.manifest.allowed_models,
            logical_attempt_id="att_exh",
            catalog_registry=self.catalog,
        )

        self.assertEqual(decision.action, "BLOCKED_BUDGET")
        self.assertIn("exhausted", decision.reason.lower())


if __name__ == "__main__":
    unittest.main()
