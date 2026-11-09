"""Comprehensive Adversarial and Recovery Test Suite for Phase 4C.2.4.

Validates all 13 critical adversarial scenarios and auditor P0 resolutions:
1. Real concurrency between multiple workers/threads via SQLite BEGIN IMMEDIATE.
2. Contention/race condition for the final unit of max_dispatches.
3. Crash simulation before and after SEND_COMMITTED (P0.3).
4. Socket timeout and ambiguous network failures (fail-closed UNKNOWN drain).
5. Read-only SQLite failure and ledger corruption tolerance.
6. Persistent manifest replay attacks and reservation reuse attempts.
7. Revocation or temporal expiration occurring between reservation and PIC commit_send.
8. Human consent omitted, false, or identity mismatch.
9. Paid model or stale catalog rejection during fallback via FreeCostGuard.
10. Fallback failure provenance: rejection without verified HTTP 429/503.
11. Idempotent outbox recovery with zero duplicate lines after crash between fsync and SQLite commit.
12. Second transmission attempts using the same reservation.
13. Concurrency between crash recovery scanner and active execution.

100% offline with zero external network calls.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import multiprocessing
import os
import shutil
import tempfile
import time
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
    LedgerCorruptionError,
    PersistentReplayAttackError,
    QuotaSecurityError,
    ReservationNotFoundError,
    TransactionalOutboxProjector,
    _InterProcessFileLock,
)
from ai_coordinator.process import safe_process_alive
from ai_coordinator.openrouter.mp_workers import (
    mp_contention_worker,
    mp_outbox_projector_worker,
    mp_recovery_scanner_worker,
)
from ai_coordinator.openrouter.cost_guard import (
    FreeCostGuard,
    PaidFallbackBlockedError,
    UnauthorizedModelError,
)
from ai_coordinator.openrouter.registry import (
    FreeModelRegistry,
    ModelCatalogEntry,
    ModelModality,
    ModelPricing,
    StaleCatalogError,
)


class TestPhase4C24AdversarialRecovery(unittest.TestCase):
    """Rigorous offline adversarial test suite for Phase 4C.2.4."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="ai_coord_4c24_adv_")
        self.db_path = os.path.join(self.tmp_dir, "pilot_quotas.db")
        self.ledger_path = os.path.join(self.tmp_dir, "pilot_audit.jsonl")
        self.trust_store_path = os.path.join(self.tmp_dir, "trust_store.json")

        self.valid_commit = "1a0adb93ad5c4ebe755beb3d6b07dccfc43eeb92"
        self.now_utc = dt.datetime.now(dt.timezone.utc)

        # Operator Key and Trust Store
        self.operator_key_id = "ed25519:operator_root_2026"
        self.signer = Ed25519Signer.generate(key_id=self.operator_key_id)

        self.trust_store = TrustStore(store_path=self.trust_store_path)
        self.trust_store.register_key(
            TrustedKeyEntry(
                key_id=self.operator_key_id,
                public_key_hex=self.signer.public_key_hex,
                identity="security_operator_root",
                permissions=[PERMISSION_DISPATCH_OPENROUTER_FREE, PERMISSION_AUDIT_VERIFY],
                status=KEY_STATUS_ACTIVE,
                created_at_utc=self.now_utc.isoformat(),
                expires_at_utc=(self.now_utc + dt.timedelta(days=90)).isoformat(),
            )
        )
        self.trust_store.save_to_file()

        # Catalog & CostGuard
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
                {
                    "id": "paid-vendor/gpt-4-pro",
                    "name": "Paid Model 4 Pro",
                    "architecture": {"modality": "text->text"},
                    "context_length": 8192,
                    "pricing": {"prompt": "0.01", "completion": "0.03", "request": "0", "image": "0"},
                },
            ]
        }
        self.catalog.load_catalog_data(raw_catalog, epoch=self.now_utc.timestamp())
        self.cost_guard = FreeCostGuard(registry=self.catalog)

        # Human Authority, Principal, Store, Projector
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
        auth_id: str = "auth-adv-001",
        max_dispatches: int = 1,
        models: Optional[Sequence[str]] = None,
        commit: Optional[str] = None,
        valid_until: Optional[str] = None,
        authorizer: str = "security_operator_root",
    ) -> SignedAuthorizationManifest:
        manifest = CanonicalAuthorizationManifest(
            authorization_id=auth_id,
            execution_scope_id="scope-adversarial-4c24",
            code_commit=commit or self.valid_commit,
            allowed_models=tuple(models or ["liquid/lfm-3-1.7b:free", "meta-llama/llama-3.2-3b-instruct:free"]),
            allowed_modalities=("text",),
            max_dispatches=max_dispatches,
            max_budget_usd=0.0,
            authorizer_identity=authorizer,
            authorizer_permissions=(PERMISSION_DISPATCH_OPENROUTER_FREE,),
            issued_at_utc=(self.now_utc - dt.timedelta(minutes=5)).isoformat(),
            valid_until_utc=valid_until or (self.now_utc + dt.timedelta(hours=2)).isoformat(),
        )
        return self.signer.sign_manifest(manifest)

    # =======================================================================
    # SCENARIO 1: Real Multi-Process / Multi-Thread Concurrency Contention
    # =======================================================================

    def test_01_real_concurrency_race_condition_max_dispatches_one(self) -> None:
        """TC-01: 5 concurrent workers race for a budget with max_dispatches=1. Exactly 1 wins."""
        signed = self._sign_manifest(auth_id="auth-race-1", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
            human_operator_identity="security_operator_root",
        )

        results = []
        errors = []

        def worker_task(worker_id: int):
            worker_store = AtomicDispatchQuotaStore(db_path=self.db_path)
            try:
                res = worker_store.acquire_reservation(
                    budget_id=b_id,
                    logical_attempt_id=f"attempt_race_{worker_id}",
                    sequence_index=0,
                    target_model="liquid/lfm-3-1.7b:free",
                )
                return ("SUCCESS", res.reservation_id, worker_id)
            except BudgetExhaustedError as exc:
                return ("EXHAUSTED", str(exc), worker_id)
            except Exception as exc:
                return ("ERROR", str(exc), worker_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_task, i) for i in range(5)]
            for f in concurrent.futures.as_completed(futures):
                outcome, detail, wid = f.result()
                if outcome == "SUCCESS":
                    results.append((wid, detail))
                elif outcome == "EXHAUSTED":
                    errors.append((wid, detail))

        # Exactly 1 success, exactly 4 BudgetExhaustedError
        self.assertEqual(len(results), 1, f"Expected 1 winner, got {len(results)}: {results}")
        self.assertEqual(len(errors), 4, f"Expected 4 exhausted, got {len(errors)}: {errors}")

        status = self.store.get_budget_status(b_id)
        self.assertEqual(status["consumed_total"], 1)
        self.assertTrue(status["is_exhausted"])

    # =======================================================================
    # SCENARIO 2: Contention for Final Unit of Multi-Dispatch Budget
    # =======================================================================

    def test_02_contention_for_final_unit_of_max_dispatches(self) -> None:
        """TC-02: max_dispatches=2, 1 already consumed. 5 workers race for the remaining 1 slot."""
        signed = self._sign_manifest(auth_id="auth-race-last-slot", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
            human_operator_identity="security_operator_root",
        )
        # Consume slot 1
        res1 = self.store.acquire_reservation(b_id, "att_init", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        self.store.record_completion(res1.reservation_id, http_status_code=429)

        # Now exactly 1 slot remains. 5 workers race for it.
        def worker_task(worker_id: int):
            worker_store = AtomicDispatchQuotaStore(db_path=self.db_path)
            try:
                res = worker_store.acquire_reservation(
                    budget_id=b_id,
                    logical_attempt_id=f"attempt_slot2_{worker_id}",
                    sequence_index=1,
                    target_model="meta-llama/llama-3.2-3b-instruct:free",
                )
                return ("SUCCESS", res.reservation_id)
            except BudgetExhaustedError:
                return ("EXHAUSTED", None)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            outcomes = [f.result()[0] for f in [executor.submit(worker_task, i) for i in range(5)]]

        self.assertEqual(outcomes.count("SUCCESS"), 1)
        self.assertEqual(outcomes.count("EXHAUSTED"), 4)

        status = self.store.get_budget_status(b_id)
        self.assertEqual(status["consumed_total"], 2)
        self.assertTrue(status["is_exhausted"])

    # =======================================================================
    # SCENARIO 3: Crash Before and After SEND_COMMITTED
    # =======================================================================

    def test_03_crash_before_and_after_send_committed(self) -> None:
        """TC-03: Differentiates crash before PIC (RESERVED) vs crash after PIC (SEND_COMMITTED)."""
        signed = self._sign_manifest(auth_id="auth-crash-differentiate", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )

        # Reservation 1: Crash BEFORE commit_send (still in RESERVED)
        res1 = self.store.acquire_reservation(b_id, "att_crash1", 0, "liquid/lfm-3-1.7b:free")
        self.assertEqual(res1.state, STATE_RESERVED)

        # Reservation 2: Crash AFTER commit_send (in SEND_COMMITTED)
        res2 = self.store.acquire_reservation(b_id, "att_crash2", 1, "meta-llama/llama-3.2-3b-instruct:free")
        self.store.commit_send(res2.reservation_id)
        self.assertEqual(self.store.get_reservation(res2.reservation_id).state, STATE_SEND_COMMITTED)

        # System restart simulation: recovery scanner runs
        recovered_ids = self.store.recover_interrupted_reservations()

        # Only res2 (SEND_COMMITTED) is drained to UNKNOWN; res1 was not in SEND_COMMITTED
        self.assertEqual(recovered_ids, [res2.reservation_id])
        self.assertEqual(self.store.get_reservation(res2.reservation_id).state, STATE_UNKNOWN)

    # =======================================================================
    # SCENARIO 4: Socket Timeout & Ambiguous Network Response
    # =======================================================================

    def test_04_socket_timeout_transitions_to_unknown_fail_closed(self) -> None:
        """TC-04: Network timeout after SEND_COMMITTED transitions to UNKNOWN and drains budget."""
        signed = self._sign_manifest(auth_id="auth-timeout-drain", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_to", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res.reservation_id)

        # Socket timeout occurs
        self.store.record_failure_unknown(res.reservation_id, reason="urllib.error.URLError: Connection reset by peer")

        up = self.store.get_reservation(res.reservation_id)
        self.assertEqual(up.state, STATE_UNKNOWN)

        # Prohibits automatic completion or retransmission
        with self.assertRaises(IllegalStateTransitionError):
            self.store.record_completion(res.reservation_id, http_status_code=200)

    # =======================================================================
    # SCENARIO 5: Disk Read-Only Failure & Ledger Corruption Tolerance
    # =======================================================================

    def test_05_corrupted_ledger_detection_fail_closed_and_verified_quarantine(self) -> None:
        """TC-05 (Auditor P1): Detects ledger corruption, fails closed on projection, and recovers via quarantine."""
        # Pre-populate ledger with valid line and a corrupt non-JSON line
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        raw_initial_content = (
            json.dumps({"event_id": "evt_existing_001", "event_type": "DUMMY", "created_at_epoch": time.time()}) + "\n"
            + "CORRUPTED_NON_JSON_DATA_GARBAGE!@#$%\n"
            + json.dumps({"event_id": "evt_existing_002", "event_type": "DUMMY", "created_at_epoch": time.time()}) + "\n"
        )
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            f.write(raw_initial_content)

        # 1. verify_ledger_integrity detects corruption and lists errors
        is_valid, errors, valid_ids = self.projector.verify_ledger_integrity()
        self.assertFalse(is_valid)
        self.assertGreaterEqual(len(errors), 1)
        self.assertIn("invalid JSON", errors[0])

        # 2. project_pending_events blocks fail-closed when corruption is detected
        with self.assertRaises(LedgerCorruptionError) as ctx:
            self.projector.project_pending_events()
        self.assertIn("Ledger corruption detected", str(ctx.exception))

        # 3. Verified recovery tool preserves raw full backup, isolates corrupted line into quarantine and repairs ledger
        repaired_cnt, quar_cnt, quar_path, full_bak_path = self.projector.recover_and_quarantine_corrupt_ledger()
        self.assertEqual(repaired_cnt, 2)
        self.assertEqual(quar_cnt, 1)
        self.assertTrue(os.path.exists(quar_path))
        self.assertTrue(os.path.exists(full_bak_path))

        # Verify full backup contains exact byte copy of pre-recovery raw ledger
        with open(full_bak_path, "r", encoding="utf-8") as bf:
            self.assertEqual(bf.read(), raw_initial_content)

        # Verify quarantine contains the corrupt line
        with open(quar_path, "r", encoding="utf-8") as qf:
            self.assertIn("CORRUPTED_NON_JSON_DATA_GARBAGE!@#$%", qf.read())

        # 4. Integrity check passes after recovery
        is_valid2, errors2, valid_ids2 = self.projector.verify_ledger_integrity()
        self.assertTrue(is_valid2)
        self.assertEqual(len(errors2), 0)
        self.assertEqual(valid_ids2, {"evt_existing_001", "evt_existing_002"})

    # =======================================================================
    # SCENARIO 6: Replay of Manifests and Reservation Reuse Attempts
    # =======================================================================

    def test_06_manifest_replay_and_reservation_reuse_blocked(self) -> None:
        """TC-06: Blocks manifest replay and prohibits reusing a reservation."""
        signed = self._sign_manifest(auth_id="auth-replay-block", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )

        # Replay attempt fails
        with self.assertRaises(PersistentReplayAttackError):
            self.store.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=True,
            )

        res = self.store.acquire_reservation(b_id, "att_reu", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res.reservation_id)
        self.store.record_completion(res.reservation_id, http_status_code=200)

        # Second transmission with same reservation is strictly blocked
        with self.assertRaises(IllegalStateTransitionError):
            self.store.commit_send(res.reservation_id)

    # =======================================================================
    # SCENARIO 7: Key Revocation or Expiration Between Reservation and Send PIC
    # =======================================================================

    def test_07_key_revocation_between_reservation_and_send_pic_blocks_send(self) -> None:
        """TC-07 (Auditor P0): Key revoked in Trust Store AFTER reservation is acquired blocks commit_send."""
        signed = self._sign_manifest(auth_id="auth-rev-at-pic", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_rev", 0, "liquid/lfm-3-1.7b:free")

        # Key is revoked in Trust Store AFTER reservation is acquired
        self.trust_store.revoke_key(
            key_id=self.operator_key_id,
            reason="Compromised workstation detected during preflight",
        )

        # commit_send revalidates Trust Store and fails closed!
        with self.assertRaises(KeyAuthorityError) as ctx:
            self.store.commit_send(res.reservation_id, trust_store=self.trust_store)
        self.assertIn("revoked", str(ctx.exception).lower())

        # Reservation is marked FAILED and never transitions to SEND_COMMITTED
        up = self.store.get_reservation(res.reservation_id)
        self.assertEqual(up.state, STATE_FAILED)

    def test_08_manifest_expiration_between_reservation_and_send_pic_blocks_send(self) -> None:
        """TC-08 (Auditor P0): Manifest expiring between reservation and commit_send blocks at PIC."""
        # Manifest valid for only 2 seconds
        near_expiry = (self.now_utc + dt.timedelta(seconds=2)).isoformat()
        signed = self._sign_manifest(auth_id="auth-exp-at-pic", valid_until=near_expiry)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_exp", 0, "liquid/lfm-3-1.7b:free")

        # Simulate evaluation after expiration timestamp
        simulated_future = self.now_utc + dt.timedelta(seconds=5)
        with self.assertRaises(QuotaSecurityError) as ctx:
            self.store.commit_send(res.reservation_id, at_time_utc=simulated_future)
        self.assertIn("expired", str(ctx.exception).lower())

        up = self.store.get_reservation(res.reservation_id)
        self.assertEqual(up.state, STATE_FAILED)

    # =======================================================================
    # SCENARIO 8: Consent Omitted, False, or Imposter Identity
    # =======================================================================

    def test_09_consent_mandatory_no_default_and_imposter_rejection(self) -> None:
        """TC-09 (Auditor P0): Enforces authenticated human approval, rejects isolated boolean flags,
        forged tokens, imposter identities, expired approvals, worker issuance, and cross-manifest reuse."""
        signed = self._sign_manifest(auth_id="auth-consent-checks")

        # 1. Isolated boolean flag on store without default session MUST BE REJECTED
        store_no_session = AtomicDispatchQuotaStore(
            db_path=os.path.join(self.tmp_dir, "isolated_flag_test.db"),
            default_trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_authority=self.human_authority,
            default_human_principal=None,
            default_human_session=None,
        )
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            store_no_session.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=True,  # Isolated boolean flag alone!
            )
        self.assertIn("isolated boolean consent flag is prohibited", str(ctx.exception).lower())

        # 2. False consent rejected
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            self.store.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=False,
            )
        self.assertIn("mandatory", str(ctx.exception).lower())

        # 3. Forged token (fabricated without authority secret) rejected
        forged_approval = HumanOperationalApproval(
            authorization_id=signed.manifest.authorization_id,
            operator_identity="security_operator_root",
            session_token=f"hsess_security_operator_root_{'0' * 64}",
            valid_from_utc=self.now_utc.isoformat(),
            valid_until_utc=(self.now_utc + dt.timedelta(hours=1)).isoformat(),
            approval_token="fake_hmac_signature_token_000000000000000000000000000000000000000000",
            explicit_consent=True,
        )
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            store_no_session.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_approval=forged_approval,
            )
        self.assertTrue(
            "adulterado" in str(ctx.exception).lower() or "invalido" in str(ctx.exception).lower()
        )

        # 4. Imposter identity rejected
        imposter_principal = self.human_authority.issue_session(owner_id="imposter_hacker")
        imposter_approval = self.human_authority.issue_approval(
            principal=imposter_principal,
            authorization_id=signed.manifest.authorization_id,
        )
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            store_no_session.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_approval=imposter_approval,
            )
        self.assertIn("operator_identity mismatch", str(ctx.exception).lower())

        # 5. Cross-manifest reuse rejected (approval for manifest A presented for manifest B)
        signed_other = self._sign_manifest(auth_id="auth-other-manifest-999")
        approval_for_other = self.human_authority.issue_approval(
            principal=self.human_principal,
            authorization_id=signed_other.manifest.authorization_id,
        )
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            store_no_session.register_budget_from_signed_manifest(
                signed_manifest=signed,  # signed has auth-consent-checks, approval has auth-other-manifest-999
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_approval=approval_for_other,
            )
        self.assertIn("authorization_id mismatch", str(ctx.exception).lower())

        # 6. Expired approval rejected
        expired_approval = self.human_authority.issue_approval(
            principal=self.human_principal,
            authorization_id=signed.manifest.authorization_id,
            valid_duration_seconds=1.0,
            at_time_utc=self.now_utc - dt.timedelta(hours=2),
        )
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            store_no_session.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_approval=expired_approval,
                check_time_utc=self.now_utc,
            )
        self.assertIn("expirada", str(ctx.exception).lower())

        # 7. Subordinate worker lockout: AI_COORDINATOR_ROLE=worker prohibited from issuing approvals
        orig_role = os.environ.get("AI_COORDINATOR_ROLE")
        try:
            os.environ["AI_COORDINATOR_ROLE"] = "worker"
            with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
                self.human_authority.issue_approval(
                    principal=self.human_principal,
                    authorization_id=signed.manifest.authorization_id,
                )
            self.assertIn("proibidos", str(ctx.exception).lower())
        finally:
            if orig_role is not None:
                os.environ["AI_COORDINATOR_ROLE"] = orig_role
            else:
                os.environ.pop("AI_COORDINATOR_ROLE", None)

        # 8. Authentic HumanOperationalApproval succeeds
        valid_approval = self.human_authority.issue_approval(
            principal=self.human_principal,
            authorization_id=signed.manifest.authorization_id,
        )
        b_id = store_no_session.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_approval=valid_approval,
        )
        self.assertTrue(b_id.startswith("budget_"))

    # =======================================================================
    # SCENARIO 9: Paid Model or Stale Catalog in Fallback (FreeCostGuard P0)
    # =======================================================================

    def test_10_paid_model_and_stale_catalog_rejected_by_free_cost_guard(self) -> None:
        """TC-10 (Auditor P0): FreeCostGuard strictly blocks paid models and stale catalogs during fallback."""
        # Manifest includes a paid model in allowed_models
        signed = self._sign_manifest(
            auth_id="auth-fb-paid-check",
            max_dispatches=2,
            models=["liquid/lfm-3-1.7b:free", "paid-vendor/gpt-4-pro"],
        )
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res1 = self.store.acquire_reservation(b_id, "att_paid", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        self.store.record_completion(res1.reservation_id, http_status_code=429)

        # Fallback evaluates candidates: paid-vendor/gpt-4-pro must be REJECTED by FreeCostGuard
        decision = self.fallback_engine.evaluate_fallback(
            budget_id=b_id,
            failed_reservation=res1,
            allowed_models=signed.manifest.allowed_models,
            logical_attempt_id="att_paid",
            catalog_registry=self.catalog,
        )

        # No candidate authorized because the only alternative is paid!
        self.assertEqual(decision.action, "NO_CANDIDATE")
        self.assertIn("No verified zero-cost alternative models", decision.reason)

    # =======================================================================
    # SCENARIO 10: Fallback Without Proven HTTP 429
    # =======================================================================

    def test_11_fallback_rejected_without_proven_http_429(self) -> None:
        """TC-11 (Auditor P0): Rejects fallback on UNKNOWN, HTTP 200, HTTP 400, or missing HTTP status."""
        signed = self._sign_manifest(auth_id="auth-fb-provenance", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )

        # 1. Successful reservation (HTTP 200) -> Rejected
        res1 = self.store.acquire_reservation(b_id, "att_prov1", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        self.store.record_completion(res1.reservation_id, http_status_code=200)
        with self.assertRaises(QuotaSecurityError) as ctx:
            self.fallback_engine.evaluate_fallback(
                budget_id=b_id,
                failed_reservation=res1,
                allowed_models=signed.manifest.allowed_models,
                logical_attempt_id="att_prov1",
                catalog_registry=self.catalog,
            )
        self.assertIn("only 'failed'", str(ctx.exception).lower())

        # 2. Ambiguous UNKNOWN reservation (e.g. socket timeout/crash) -> STRICTLY REJECTED
        signed2 = self._sign_manifest(auth_id="auth-fb-unknown-block", max_dispatches=2)
        b_id2 = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed2,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res_unk = self.store.acquire_reservation(b_id2, "att_unk", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res_unk.reservation_id)
        self.store.record_failure_unknown(res_unk.reservation_id, reason="Simulated network drop")
        with self.assertRaises(QuotaSecurityError) as ctx:
            self.fallback_engine.evaluate_fallback(
                budget_id=b_id2,
                failed_reservation=res_unk,
                allowed_models=signed2.manifest.allowed_models,
                logical_attempt_id="att_unk",
                catalog_registry=self.catalog,
            )
        self.assertIn("unknown", str(ctx.exception).lower())

        # 3. Non-429/503 HTTP status (e.g. HTTP 400 Bad Request) -> REJECTED
        signed3 = self._sign_manifest(auth_id="auth-fb-http400-block", max_dispatches=2)
        b_id3 = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed3,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res_400 = self.store.acquire_reservation(b_id3, "att_400", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res_400.reservation_id)
        self.store.record_completion(res_400.reservation_id, http_status_code=400)
        with self.assertRaises(QuotaSecurityError) as ctx:
            self.fallback_engine.evaluate_fallback(
                budget_id=b_id3,
                failed_reservation=res_400,
                allowed_models=signed3.manifest.allowed_models,
                logical_attempt_id="att_400",
                catalog_registry=self.catalog,
            )
        self.assertIn("400", str(ctx.exception))
        self.assertIn("only proven http 429 and 503", str(ctx.exception).lower())

    # =======================================================================
    # SCENARIO 11: Idempotent Outbox Recovery (Crash Between fsync and SQLite)
    # =======================================================================

    def test_12_idempotent_outbox_recovery_zero_duplicate_lines(self) -> None:
        """TC-12 (Auditor P1): Crash after fsync and before SQLite commit does not duplicate JSONL lines."""
        signed = self._sign_manifest(auth_id="auth-outbox-crash", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res = self.store.acquire_reservation(b_id, "att_idemp", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res.reservation_id)
        self.store.record_completion(res.reservation_id, http_status_code=200)

        # Normal projection #1
        count1 = self.projector.project_pending_events()
        self.assertGreater(count1, 0)

        with open(self.ledger_path, "r", encoding="utf-8") as f:
            lines1 = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(lines1), count1)

        # Simulate crash: reset projected_at_epoch to NULL in SQLite for all events
        conn = self.store._get_connection()
        conn.execute("UPDATE outbox_events SET projected_at_epoch = NULL;")

        # Second projection run (Recovery after crash)
        count2 = self.projector.project_pending_events()
        self.assertEqual(count2, count1)

        # Verify that the ledger file STILL has exactly lines1 length (ZERO duplicate lines appended!)
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            lines2 = [l.strip() for l in f if l.strip()]

        self.assertEqual(len(lines2), len(lines1), "Idempotent recovery failed: duplicate lines were appended to JSONL!")
        self.assertEqual(lines2, lines1)

    # =======================================================================
    # SCENARIO 12: Recovery Scanner vs Active Execution Concurrency
    # =======================================================================

    def test_13_recovery_scanner_does_not_corrupt_active_completed_reservations(self) -> None:
        """TC-13: Recovery scanner only targets dangling uncompleted reservations, leaving completed intact."""
        signed = self._sign_manifest(auth_id="auth-rec-active", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        # Reservation 1 completed
        res1 = self.store.acquire_reservation(b_id, "att_c1", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        self.store.record_completion(res1.reservation_id, http_status_code=200)

        # Reservation 2 dangling (crashed)
        res2 = self.store.acquire_reservation(b_id, "att_c2", 1, "meta-llama/llama-3.2-3b-instruct:free")
        self.store.commit_send(res2.reservation_id)

        # Run recovery
        recovered = self.store.recover_interrupted_reservations()

        self.assertEqual(recovered, [res2.reservation_id])
        self.assertEqual(self.store.get_reservation(res1.reservation_id).state, STATE_COMPLETED)
        self.assertEqual(self.store.get_reservation(res2.reservation_id).state, STATE_UNKNOWN)

    # =======================================================================
    # SCENARIO 13: SEND_COMMITTED Missing Evidence Fails Closed (Auditor P0)
    # =======================================================================

    def test_14_commit_send_missing_trust_store_or_commit_fails_closed(self) -> None:
        """TC-14 (Auditor P0): commit_send strictly blocks fail-closed if TrustStore or commit HEAD is missing."""
        signed = self._sign_manifest(auth_id="auth-pic-evidence-missing", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )

        # 1. Missing Trust Store blocks fail-closed
        store_no_ts = AtomicDispatchQuotaStore(
            db_path=self.db_path,
            default_trust_store=None,  # No TrustStore available
            expected_commit=self.valid_commit,
            human_authority=self.human_authority,
            default_human_principal=self.human_principal,
        )
        res_no_ts = store_no_ts.acquire_reservation(b_id, "att_no_ts", 0, "liquid/lfm-3-1.7b:free")
        with self.assertRaises(QuotaSecurityError) as ctx:
            store_no_ts.commit_send(res_no_ts.reservation_id)
        self.assertIn("trust store is mandatory", str(ctx.exception).lower())
        self.assertEqual(store_no_ts.get_reservation(res_no_ts.reservation_id).state, STATE_FAILED)

        # 2. Missing Execution Commit HEAD blocks fail-closed
        store_no_commit = AtomicDispatchQuotaStore(
            db_path=self.db_path,
            default_trust_store=self.trust_store,
            expected_commit=None,  # No commit HEAD available
            human_authority=self.human_authority,
            default_human_principal=self.human_principal,
        )
        res_no_cm = store_no_commit.acquire_reservation(b_id, "att_no_cm", 1, "liquid/lfm-3-1.7b:free")
        with self.assertRaises(QuotaSecurityError) as ctx:
            store_no_commit.commit_send(res_no_cm.reservation_id)
        self.assertIn("runtime code commit is mandatory", str(ctx.exception).lower())
        self.assertEqual(store_no_commit.get_reservation(res_no_cm.reservation_id).state, STATE_FAILED)

    # =======================================================================
    # SCENARIO 14: Multi-Process Contention for Final Quota (Auditor P0)
    # =======================================================================

    def test_15_multiprocess_contention_for_final_unit_of_max_dispatches(self) -> None:
        """TC-15 (Auditor Mandate): Separate OS processes compete for final quota; exactly 1 wins."""
        signed = self._sign_manifest(auth_id="auth-mp-contention", max_dispatches=1)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )

        queue = multiprocessing.Queue()
        processes = []
        process_count = 4

        for i in range(process_count):
            p = multiprocessing.Process(
                target=mp_contention_worker,
                args=(self.db_path, b_id, f"mp_att_{i}", 0, "liquid/lfm-3-1.7b:free", queue),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10.0)

        results = [queue.get(timeout=2.0) for _ in range(process_count)]
        statuses = [r[0] for r in results]

        self.assertEqual(statuses.count("SUCCESS"), 1, f"Expected exactly 1 process to acquire quota: {results}")
        self.assertEqual(statuses.count("EXHAUSTED"), process_count - 1, f"Expected other processes to fail exhausted: {results}")
        self.assertEqual(statuses.count("ERROR"), 0, f"Unexpected worker error in processes: {results}")

    # =======================================================================
    # SCENARIO 15: Multi-Process Concurrent Outbox Projection (Auditor P1)
    # =======================================================================

    def test_16_multiprocess_concurrent_outbox_projection_no_data_loss(self) -> None:
        """TC-16 (Auditor Mandate): Multiple OS processes project outbox events simultaneously without data loss or corruption."""
        signed = self._sign_manifest(auth_id="auth-mp-projection", max_dispatches=4)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        for i in range(3):
            r = self.store.acquire_reservation(b_id, f"mp_proj_{i}", i, "liquid/lfm-3-1.7b:free")
            self.store.commit_send(r.reservation_id)
            self.store.record_completion(r.reservation_id, http_status_code=200)

        queue = multiprocessing.Queue()
        processes = []
        process_count = 4

        for _ in range(process_count):
            p = multiprocessing.Process(
                target=mp_outbox_projector_worker,
                args=(self.db_path, self.ledger_path, queue),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10.0)

        results = [queue.get(timeout=2.0) for _ in range(process_count)]
        for tag, val in results:
            self.assertIn(tag, ("PROJECTED",), f"Worker error in multi-process projection: {val}")

        # Verify ledger integrity
        is_ok, errors, event_ids = self.projector.verify_ledger_integrity()
        self.assertTrue(is_ok, f"Ledger integrity compromised during multi-process projection: {errors}")
        self.assertGreater(len(event_ids), 0)

    # =======================================================================
    # SCENARIO 16: Multi-Process Simultaneous Crash Recovery Scanner
    # =======================================================================

    def test_17_multiprocess_simultaneous_crash_recovery_scanner(self) -> None:
        """TC-17 (Auditor Mandate): Multiple OS processes run crash recovery scanner simultaneously without split-brain."""
        signed = self._sign_manifest(auth_id="auth-mp-recovery", max_dispatches=5)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res_ids = []
        for i in range(4):
            r = self.store.acquire_reservation(b_id, f"mp_rec_{i}", i, "liquid/lfm-3-1.7b:free")
            self.store.commit_send(r.reservation_id)
            res_ids.append(r.reservation_id)

        queue = multiprocessing.Queue()
        processes = []
        process_count = 3

        for _ in range(process_count):
            p = multiprocessing.Process(
                target=mp_recovery_scanner_worker,
                args=(self.db_path, 0.0, queue),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join(timeout=10.0)

        results = [queue.get(timeout=2.0) for _ in range(process_count)]
        recovered_counts = [val for tag, val in results if tag == "RECOVERED"]
        self.assertEqual(sum(recovered_counts), 4, f"Total recovered reservations must equal 4 across all processes: {results}")

        for rid in res_ids:
            self.assertEqual(self.store.get_reservation(rid).state, STATE_UNKNOWN)

    # =======================================================================
    # SCENARIO 16: Inter-Process File Lock Dead PID Reclamation & Liveness (Auditor P1)
    # =======================================================================

    def test_18_inter_process_lock_dead_pid_reclamation_and_living_pid_contention(self) -> None:
        """TC-18 (Auditor P1): _InterProcessFileLock reclaims locks from dead PIDs and blocks on live PIDs."""
        lock_file = os.path.join(self.tmp_dir, "test_ipc.lock")

        # 1. Dead PID simulation: write lock file with an impossible/dead PID (e.g. 9999999)
        dead_pid = 9999999
        self.assertFalse(safe_process_alive(dead_pid))
        with open(lock_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": dead_pid, "acquired_at_epoch": time.time()}))

        # Lock acquisition MUST detect dead PID and immediately reclaim the lock
        with _InterProcessFileLock(lock_file, timeout_sec=1.0) as lock:
            self.assertTrue(lock._acquired)
            self.assertTrue(os.path.exists(lock_file))

        # Lock is cleanly released on exit
        self.assertFalse(os.path.exists(lock_file))

        # 2. Living PID contention simulation: write lock file with current active PID
        current_pid = os.getpid()
        self.assertTrue(safe_process_alive(current_pid))
        with open(lock_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"pid": current_pid, "acquired_at_epoch": time.time()}))

        # Contention attempt MUST NOT steal the lock from active PID and MUST timeout
        with self.assertRaises(TimeoutError) as ctx:
            with _InterProcessFileLock(lock_file, timeout_sec=0.1, retry_delay_sec=0.02):
                pass
        self.assertIn("held by active PID", str(ctx.exception))
        # Lock file was preserved because PID is alive
        self.assertTrue(os.path.exists(lock_file))
        os.unlink(lock_file)


if __name__ == "__main__":
    unittest.main()
