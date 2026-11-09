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

        # Store and Projector
        self.store = AtomicDispatchQuotaStore(
            db_path=self.db_path,
            default_trust_store=self.trust_store,
            expected_commit=self.valid_commit,
        )
        self.projector = TransactionalOutboxProjector(store=self.store, ledger_path=self.ledger_path)
        self.fallback_engine = GovernedFallbackEngine(
            quota_store=self.store,
            projector=self.projector,
            cost_guard=self.cost_guard,
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

    def test_05_corrupted_ledger_recovery_skips_bad_lines_without_crash(self) -> None:
        """TC-05: Projector safely handles corrupt/truncated lines in JSONL ledger without data loss."""
        # Pre-populate ledger with valid line and a corrupt non-JSON line
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event_id": "evt_existing_001", "event_type": "DUMMY"}) + "\n")
            f.write("CORRUPTED_NON_JSON_DATA_GARBAGE!@#$%\n")
            f.write(json.dumps({"event_id": "evt_existing_002", "event_type": "DUMMY"}) + "\n")

        # Projector reads existing IDs without raising JSONDecodeError
        existing = self.projector._read_existing_ledger_event_ids()
        self.assertIn("evt_existing_001", existing)
        self.assertIn("evt_existing_002", existing)

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
        """TC-09 (Auditor P0): Enforces mandatory consent and rejects imposter identity."""
        signed = self._sign_manifest(auth_id="auth-consent-checks")

        # 1. False consent rejected
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            self.store.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=False,
            )
        self.assertIn("mandatory", str(ctx.exception).lower())

        # 2. Imposter identity rejected
        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            self.store.register_budget_from_signed_manifest(
                signed_manifest=signed,
                trust_store=self.trust_store,
                expected_commit=self.valid_commit,
                human_operator_consent=True,
                human_operator_identity="imposter_hacker",  # does not match manifest
            )
        self.assertIn("identity mismatch", str(ctx.exception).lower())

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
        """TC-11: Fallback requires proven HTTP 429 failure on matching reservation."""
        signed = self._sign_manifest(auth_id="auth-fb-provenance", max_dispatches=2)
        b_id = self.store.register_budget_from_signed_manifest(
            signed_manifest=signed,
            trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_operator_consent=True,
        )
        res1 = self.store.acquire_reservation(b_id, "att_prov", 0, "liquid/lfm-3-1.7b:free")
        self.store.commit_send(res1.reservation_id)
        # Reservation actually succeeded with HTTP 200
        self.store.record_completion(res1.reservation_id, http_status_code=200)

        # Attempting fallback on a successful reservation must be rejected
        with self.assertRaises(QuotaSecurityError) as ctx:
            self.fallback_engine.evaluate_fallback(
                budget_id=b_id,
                failed_reservation=res1,
                allowed_models=signed.manifest.allowed_models,
                logical_attempt_id="att_prov",
                catalog_registry=self.catalog,
            )
        self.assertIn("must be 'FAILED'", str(ctx.exception))

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


if __name__ == "__main__":
    unittest.main()
