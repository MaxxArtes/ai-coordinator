"""Unit and integration tests for Phase 4C.2 Egress Transport Governance Integration.

Validates the mandatory integration between OpenRouterHttpTransport and:
1. AtomicDispatchQuotaStore (SQLite global budgets and reservation transitions).
2. Tripartite Authorization (Ed25519 SignedAuthorizationManifest, HumanOperationalApproval, TrustStore).
3. Pre-Intent Commit (PIC) ensuring SEND_COMMITTED before physical egress.
4. TransactionalOutboxProjector (durable ledger projection with fsync before egress).
5. GovernedFallbackEngine (strict fallback ceilings: max_dispatches=1 blocks second call,
   UNKNOWN prohibits retry, max_dispatches=2 authorizes exclusively pre-authorized contingency).
6. Fail-closed guarantee: any failure prior to physical egress results in ZERO HTTP calls.
7. Verification of routing restrictions (no route parameter, no provider.allow_fallbacks, no unapproved models).
8. Strict human authorization (omitted human consent fails closed; no automatic True conversion).
9. Ambiguous network errors on contingency attempt #2 transition to UNKNOWN fail-closed.
10. Operational configuration requirement: require_governed_egress defaults to True when quota_store is provided.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from ai_coordinator.manifest_auth import (
    KEY_STATUS_ACTIVE,
    PERMISSION_AUDIT_VERIFY,
    PERMISSION_DISPATCH_OPENROUTER_FREE,
    CanonicalAuthorizationManifest,
    Ed25519Signer,
    HumanOperationalApproval,
    HumanOperationalAuthority,
    HumanOperationalAuthorizationError,
    SignedAuthorizationManifest,
    TrustStore,
    TrustedKeyEntry,
)
from ai_coordinator.openrouter.atomic_quota import (
    AtomicDispatchQuotaStore,
    BudgetExhaustedError,
    GovernedFallbackEngine,
    QuotaSecurityError,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_SEND_COMMITTED,
    STATE_UNKNOWN,
    TransactionalOutboxProjector,
)
from ai_coordinator.openrouter.cost_guard import CostGuardError, FreeCostGuard, UnauthorizedModelError
from ai_coordinator.openrouter.registry import (
    FreeModelRegistry,
    ModelCatalogEntry,
    ModelModality,
    ModelPricing,
)
from ai_coordinator.openrouter.transport import (
    GovernedEgressRequiredError,
    HttpResponse,
    IncompatibleModalityEndpointError,
    LiveNetworkDisabledError,
    OpenRouterHttpTransport,
    TransportError,
)


class TransportGovernanceIntegrationTests(unittest.TestCase):
    """Rigorous tests verifying that OpenRouterHttpTransport enforces all 4C.2 governance gates."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="ai_coord_transport_gov_test_")
        self.db_path = os.path.join(self.tmp_dir, "test_governance.db")
        self.ledger_path = os.path.join(self.tmp_dir, "test_outbox_ledger.jsonl")
        self.trust_store_path = os.path.join(self.tmp_dir, "test_trust_store.json")

        self.valid_commit = "1a0adb93ad5c4ebe755beb3d6b07dccfc43eeb92"
        self.now_utc = dt.datetime.now(dt.timezone.utc)

        # 1. Cryptographic Setup: Ed25519 Signer & Trust Store
        self.operator_key_id = "ed25519:operator_test_transport"
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
                expires_at_utc=(self.now_utc + dt.timedelta(days=30)).isoformat(),
            )
        )
        self.trust_store.save_to_file()

        # 2. Human Operational Authority
        self.supervisor_key = "test_sup_secret_key_transport_gov"
        self.human_authority = HumanOperationalAuthority(supervisor_key=self.supervisor_key)
        self.human_principal = self.human_authority.issue_session(
            owner_id="security_operator_root",
            supervisor_key=self.supervisor_key,
        )

        # 3. Free Model Catalog Setup with 2 free text models
        self.primary_model = "liquid/lfm-3-1.7b:free"
        self.contingency_model = "meta-llama/llama-3.2-3b-instruct:free"

        self.catalog = FreeModelRegistry()
        raw_catalog = {
            "data": [
                {
                    "id": self.primary_model,
                    "name": "Liquid LFM 3 1.7B (Free)",
                    "architecture": {"modality": "text->text"},
                    "context_length": 32768,
                    "pricing": {"prompt": "0", "completion": "0", "request": "0", "image": "0"},
                },
                {
                    "id": self.contingency_model,
                    "name": "Llama 3.2 3B Instruct (Free)",
                    "architecture": {"modality": "text->text"},
                    "context_length": 131072,
                    "pricing": {"prompt": "0", "completion": "0", "request": "0", "image": "0"},
                },
            ]
        }
        self.catalog.load_catalog_data(raw_catalog, epoch=self.now_utc.timestamp())
        self.cost_guard = FreeCostGuard(registry=self.catalog)

        # 4. Storage and Outbox Projector
        self.store = AtomicDispatchQuotaStore(
            db_path=self.db_path,
            default_trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_authority=self.human_authority,
            default_human_principal=self.human_principal,
        )
        self.projector = TransactionalOutboxProjector(store=self.store, ledger_path=self.ledger_path)

        # 5. Mock HTTP Opener
        self.mock_opener = MagicMock()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_signed_manifest_and_approval(
        self,
        auth_id: str,
        max_dispatches: int = 1,
        models: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[SignedAuthorizationManifest, HumanOperationalApproval]:
        manifest = CanonicalAuthorizationManifest(
            authorization_id=auth_id,
            execution_scope_id="scope-transport-pilot",
            code_commit=self.valid_commit,
            allowed_models=tuple(models or [self.primary_model, self.contingency_model]),
            allowed_modalities=("text",),
            max_dispatches=max_dispatches,
            max_budget_usd=0.0,
            authorizer_identity="security_operator_root",
            authorizer_permissions=(PERMISSION_DISPATCH_OPENROUTER_FREE,),
            issued_at_utc=(self.now_utc - dt.timedelta(minutes=5)).isoformat(),
            valid_until_utc=(self.now_utc + dt.timedelta(hours=2)).isoformat(),
        )
        signed = self.signer.sign_manifest(manifest)
        approval = self.human_authority.issue_approval(
            principal=self.human_principal,
            authorization_id=auth_id,
            purpose=PERMISSION_DISPATCH_OPENROUTER_FREE,
            at_time_utc=self.now_utc,
        )
        return signed, approval

    def _create_mock_200_response(self, content: str = "Test response content") -> MagicMock:
        mock_resp = MagicMock()
        mock_resp.getcode.return_value = 200
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.read.return_value = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_resp
        mock_cm.__exit__.return_value = None
        return mock_cm

    def _create_mock_http_error(self, code: int = 429, msg: str = "Rate Limit", retry_after: str = "10") -> urllib.error.HTTPError:
        err_fp = io.BytesIO(json.dumps({"error": {"message": msg, "code": code}}).encode("utf-8"))
        return urllib.error.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=code,
            msg=msg,
            hdrs={"Content-Type": "application/json", "Retry-After": retry_after},
            fp=err_fp,
        )

    # -------------------------------------------------------------------------
    # Test 1: Ungoverned dispatch blocked when require_governed_egress=True
    # -------------------------------------------------------------------------
    def test_01_ungoverned_dispatch_blocked_when_require_governed_egress(self) -> None:
        """Calling legacy dispatch() when require_governed_egress=True fails closed immediately."""
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            opener=self.mock_opener,
            require_governed_egress=True,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "hello"}]}

        with self.assertRaises(GovernedEgressRequiredError) as ctx:
            transport.dispatch("text", payload)

        self.assertIn("require_governed_egress=True", str(ctx.exception))
        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero physical/mock HTTP calls must be made.")

    # -------------------------------------------------------------------------
    # Test 2: Absence of authorization produces ZERO HTTP calls
    # -------------------------------------------------------------------------
    def test_02_absence_of_authorization_produces_zero_http_calls(self) -> None:
        """Unregistered or unauthorized budget causes fail-closed abort with 0 HTTP calls."""
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        with self.assertRaises(QuotaSecurityError) as ctx:
            transport.dispatch_governed(
                modality_str="text",
                payload=payload,
                budget_id="non-existent-budget-id",
                logical_attempt_id="att-test-01",
            )

        self.assertIn("Budget 'non-existent-budget-id' not found", str(ctx.exception))
        self.assertEqual(self.mock_opener.open.call_count, 0, "No network call when budget is missing.")

    # -------------------------------------------------------------------------
    # Test 3: Outbox projection failure produces ZERO HTTP calls
    # -------------------------------------------------------------------------
    def test_03_outbox_projection_failure_produces_zero_http_calls(self) -> None:
        """If Transactional Outbox projection fails (e.g. disk error), physical HTTP call MUST NOT happen."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-proj-fail", max_dispatches=1)
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        with patch.object(self.projector, "project_pending_events", side_effect=OSError("Disk full during outbox fsync")):
            with self.assertRaises(OSError) as ctx:
                transport.dispatch_governed(
                    modality_str="text",
                    payload=payload,
                    budget_id=signed.manifest.authorization_id,
                    logical_attempt_id="att-test-proj-fail",
                    signed_manifest=signed,
                    human_approval=approval,
                )
            self.assertIn("Disk full", str(ctx.exception))

        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero HTTP calls when outbox projection fails.")

    # -------------------------------------------------------------------------
    # Test 4: max_dispatches=1 blocks secondary dispatch after HTTP 429
    # -------------------------------------------------------------------------
    def test_04_max_dispatches_1_blocks_secondary_dispatch_after_http_429(self) -> None:
        """When max_dispatches=1 and model returns HTTP 429, exactly 1 call occurs and 0 secondary calls."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-max-1", max_dispatches=1)
        self.mock_opener.open.side_effect = self._create_mock_http_error(code=429, msg="Rate Limit")

        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        resp = transport.dispatch_governed(
            modality_str="text",
            payload=payload,
            budget_id=signed.manifest.authorization_id,
            logical_attempt_id="att-test-max-1",
            signed_manifest=signed,
            human_approval=approval,
        )

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(self.mock_opener.open.call_count, 1, "Must make EXACTLY 1 physical dispatch.")
        self.assertEqual(resp.governance_metadata["state"], STATE_FAILED)
        self.assertEqual(resp.governance_metadata["fallback_action"], "RECOMMENDED_ONLY")

        # Verify Outbox ledger contains FALLBACK_PROPOSED_BLOCKED
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        event_types = [evt["event_type"] for evt in lines]
        self.assertIn("FALLBACK_PROPOSED_BLOCKED", event_types)

    # -------------------------------------------------------------------------
    # Test 5: UNKNOWN state blocks automatic retransmission
    # -------------------------------------------------------------------------
    def test_05_unknown_state_blocks_automatic_retransmission(self) -> None:
        """Ambiguous socket timeout leaves reservation in UNKNOWN and strictly forbids automatic retry."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-unknown", max_dispatches=2)
        self.mock_opener.open.side_effect = TimeoutError("Connection reset after socket send")

        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        resp = transport.dispatch_governed(
            modality_str="text",
            payload=payload,
            budget_id=signed.manifest.authorization_id,
            logical_attempt_id="att-test-unknown",
            signed_manifest=signed,
            human_approval=approval,
        )

        self.assertEqual(resp.status_code, 500)
        self.assertIn("AMBIGUOUS_OUTCOME_UNKNOWN", resp.raw_error)
        self.assertEqual(self.mock_opener.open.call_count, 1, "Must NOT attempt retransmission upon UNKNOWN.")
        self.assertEqual(resp.governance_metadata["state"], STATE_UNKNOWN)
        self.assertEqual(resp.governance_metadata["fallback_action"], "BLOCKED_UNKNOWN")

    # -------------------------------------------------------------------------
    # Test 6: max_dispatches=2 permits exclusively preauthorized contingency
    # -------------------------------------------------------------------------
    def test_06_max_dispatches_2_authorizes_exclusively_preauthorized_contingency(self) -> None:
        """When max_dispatches=2 and primary model returns 429, secondary dispatch executes contingency model."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-max-2", max_dispatches=2)

        mock_err = self._create_mock_http_error(code=429, msg="Rate Limited Primary")
        mock_ok = self._create_mock_200_response("Contingency response ok")
        self.mock_opener.open.side_effect = [mock_err, mock_ok]

        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        resp = transport.dispatch_governed(
            modality_str="text",
            payload=payload,
            budget_id=signed.manifest.authorization_id,
            logical_attempt_id="att-test-max-2",
            signed_manifest=signed,
            human_approval=approval,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.mock_opener.open.call_count, 2, "Must make exactly 2 calls: primary and contingency.")
        self.assertEqual(resp.governance_metadata["fallback_action"], "DISPATCH_AUTHORIZED")
        self.assertEqual(resp.governance_metadata["primary_model"], self.primary_model)
        self.assertEqual(resp.governance_metadata["contingency_model"], self.contingency_model)
        self.assertEqual(resp.governance_metadata["sequence_index"], 2)
        self.assertEqual(resp.governance_metadata["state"], STATE_COMPLETED)

        # Verify second call used contingency model
        second_call_req = self.mock_opener.open.call_args_list[1][0][0]
        second_payload = json.loads(second_call_req.data.decode("utf-8"))
        self.assertEqual(second_payload["model"], self.contingency_model)

    # -------------------------------------------------------------------------
    # Test 7: Failures before PIC prevent HTTP dispatch entirely
    # -------------------------------------------------------------------------
    def test_07_failures_before_pic_prevent_http_dispatch(self) -> None:
        """If commit_send (PIC) fails, physical HTTP call cannot be reached."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-pic-fail", max_dispatches=1)
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        with patch.object(self.store, "commit_send", side_effect=QuotaSecurityError("PIC database lock simulated")):
            with self.assertRaises(QuotaSecurityError) as ctx:
                transport.dispatch_governed(
                    modality_str="text",
                    payload=payload,
                    budget_id=signed.manifest.authorization_id,
                    logical_attempt_id="att-test-pic-fail",
                    signed_manifest=signed,
                    human_approval=approval,
                )
            self.assertIn("PIC database lock simulated", str(ctx.exception))

        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero HTTP calls when PIC fails.")

    # -------------------------------------------------------------------------
    # Test 8: Unauthorized model not in manifest produces ZERO HTTP calls
    # -------------------------------------------------------------------------
    def test_08_unauthorized_model_not_in_manifest_produces_zero_http_calls(self) -> None:
        """Attempting to dispatch a model outside manifest allowed_models fails closed with 0 calls."""
        signed, approval = self._create_signed_manifest_and_approval(
            "auth-test-single-model",
            max_dispatches=1,
            models=[self.primary_model],
        )
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.contingency_model, "messages": [{"role": "user", "content": "test"}]}

        with self.assertRaises(UnauthorizedModelError) as ctx:
            transport.dispatch_governed(
                modality_str="text",
                payload=payload,
                budget_id=signed.manifest.authorization_id,
                logical_attempt_id="att-test-model-fail",
                signed_manifest=signed,
                human_approval=approval,
            )

        self.assertIn("não consta entre os modelos autorizados pelo orçamento", str(ctx.exception))
        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero HTTP calls for unmanifested model.")

    # -------------------------------------------------------------------------
    # Test 9: Budget exhaustion produces ZERO HTTP calls
    # -------------------------------------------------------------------------
    def test_09_budget_exhaustion_produces_zero_http_calls(self) -> None:
        """When budget has consumed max_dispatches, subsequent dispatch is rejected with 0 calls."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-exhaust", max_dispatches=1)
        self.mock_opener.open.return_value = self._create_mock_200_response("Success 1")

        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        # Call 1: succeeds and consumes the budget
        resp1 = transport.dispatch_governed(
            modality_str="text",
            payload=payload,
            budget_id=signed.manifest.authorization_id,
            logical_attempt_id="att-test-exhaust-1",
            signed_manifest=signed,
            human_approval=approval,
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(self.mock_opener.open.call_count, 1)

        # Call 2: must fail with BudgetExhaustedError and NOT make a second call
        with self.assertRaises(BudgetExhaustedError) as ctx:
            transport.dispatch_governed(
                modality_str="text",
                payload=payload,
                budget_id=signed.manifest.authorization_id,
                logical_attempt_id="att-test-exhaust-2",
            )
        self.assertIn("esgotado: 1/1 despachos consumidos", str(ctx.exception))
        self.assertEqual(self.mock_opener.open.call_count, 1, "Call count must remain 1 (zero second call).")

    # -------------------------------------------------------------------------
    # Test 10: Happy path E2E dispatch with full tripartite governance
    # -------------------------------------------------------------------------
    def test_10_end_to_end_governed_success_with_tripartite_signatures(self) -> None:
        """End-to-end happy path: tripartite signatures verified, PIC committed, outbox projected, HTTP 200 returned."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-happy-e2e", max_dispatches=1)
        self.mock_opener.open.return_value = self._create_mock_200_response("Hello governed world")

        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "Hello"}]}

        resp = transport.dispatch_governed(
            modality_str="text",
            payload=payload,
            budget_id=signed.manifest.authorization_id,
            logical_attempt_id="att-test-happy-e2e",
            signed_manifest=signed,
            human_approval=approval,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.mock_opener.open.call_count, 1)
        self.assertEqual(resp.governance_metadata["state"], STATE_COMPLETED)
        self.assertEqual(resp.governance_metadata["sequence_index"], 1)

        # Verify physical outbox projection exists on disk
        self.assertTrue(os.path.exists(self.ledger_path))
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertGreaterEqual(len(lines), 2)  # SEND_COMMITTED and DISPATCH_COMPLETED_200
        types = [e["event_type"] for e in lines]
        self.assertIn("SEND_COMMITTED", types)
        self.assertIn("DISPATCH_COMPLETED_200", types)

    # -------------------------------------------------------------------------
    # Test 11: Route parameter in payload rejected fail-closed
    # -------------------------------------------------------------------------
    def test_11_route_parameter_in_payload_rejected_fail_closed(self) -> None:
        """Passing 'route' parameter in payload violates local deterministic governance and fails closed."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-route-param", max_dispatches=1)
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {
            "model": self.primary_model,
            "route": "fallback",
            "messages": [{"role": "user", "content": "hi"}],
        }

        with self.assertRaises(CostGuardError) as ctx:
            transport.dispatch_governed(
                modality_str="text",
                payload=payload,
                budget_id=signed.manifest.authorization_id,
                logical_attempt_id="att-test-route-param",
                signed_manifest=signed,
                human_approval=approval,
            )
        self.assertIn("Parâmetro 'route' proibido", str(ctx.exception))
        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero HTTP calls when route parameter is supplied.")

    # -------------------------------------------------------------------------
    # Test 12: provider.allow_fallbacks=True in payload rejected
    # -------------------------------------------------------------------------
    def test_12_provider_allow_fallbacks_in_payload_rejected(self) -> None:
        """Provider-side fallback parameter 'allow_fallbacks: True' is strictly prohibited fail-closed."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-prov-fb", max_dispatches=1)
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {
            "model": self.primary_model,
            "provider": {"allow_fallbacks": True},
            "messages": [{"role": "user", "content": "hi"}],
        }

        with self.assertRaises(CostGuardError) as ctx:
            transport.dispatch_governed(
                modality_str="text",
                payload=payload,
                budget_id=signed.manifest.authorization_id,
                logical_attempt_id="att-test-prov-fb",
                signed_manifest=signed,
                human_approval=approval,
            )
        self.assertIn("provider.allow_fallbacks=True é proibido", str(ctx.exception))
        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero HTTP calls when provider fallback is requested.")

    # -------------------------------------------------------------------------
    # Test 13: Implicit models list with unmanifested model rejected
    # -------------------------------------------------------------------------
    def test_13_implicit_models_list_with_unauthorized_model_rejected(self) -> None:
        """If 'models' array contains an unmanifested model, dispatch_governed fails closed."""
        signed, approval = self._create_signed_manifest_and_approval(
            "auth-test-models-list",
            max_dispatches=1,
            models=[self.primary_model],  # only primary model manifested
        )
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {
            "model": self.primary_model,
            "models": [self.primary_model, self.contingency_model],  # contingency model not manifested!
            "messages": [{"role": "user", "content": "hi"}],
        }

        with self.assertRaises(UnauthorizedModelError) as ctx:
            transport.dispatch_governed(
                modality_str="text",
                payload=payload,
                budget_id=signed.manifest.authorization_id,
                logical_attempt_id="att-test-models-list",
                signed_manifest=signed,
                human_approval=approval,
            )
        self.assertIn("não está autorizado pelo orçamento", str(ctx.exception))
        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero HTTP calls when unmanifested model in models array.")

    # -------------------------------------------------------------------------
    # Test 14: Omitted human consent fails closed (no automatic True conversion)
    # -------------------------------------------------------------------------
    def test_14_omitted_human_consent_rejected_fail_closed(self) -> None:
        """Omitted human consent without authenticated approval must raise HumanOperationalAuthorizationError."""
        # Create store WITHOUT default_human_principal so it cannot auto-issue
        store_no_human = AtomicDispatchQuotaStore(
            db_path=os.path.join(self.tmp_dir, "test_no_human.db"),
            default_trust_store=self.trust_store,
            expected_commit=self.valid_commit,
            human_authority=self.human_authority,
            default_human_principal=None,  # No default human principal!
        )
        projector_no_human = TransactionalOutboxProjector(
            store=store_no_human,
            ledger_path=os.path.join(self.tmp_dir, "test_no_human.jsonl"),
        )
        signed, _ = self._create_signed_manifest_and_approval("auth-test-no-human", max_dispatches=1)

        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=store_no_human,
            projector=projector_no_human,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "hi"}]}

        with self.assertRaises(HumanOperationalAuthorizationError) as ctx:
            transport.dispatch_governed(
                modality_str="text",
                payload=payload,
                budget_id=signed.manifest.authorization_id,
                logical_attempt_id="att-test-no-human",
                signed_manifest=signed,
                human_approval=None,  # omitted!
                human_operator_consent=None,  # omitted!
            )
        self.assertIn("human", str(ctx.exception).lower())
        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero HTTP calls when human authorization is omitted.")

    # -------------------------------------------------------------------------
    # Test 15: Ambiguous contingency timeout transitions to UNKNOWN fail-closed
    # -------------------------------------------------------------------------
    def test_15_ambiguous_contingency_timeout_transitions_to_unknown(self) -> None:
        """When attempt 1 returns 429 and attempt 2 (contingency) encounters a socket timeout, state MUST be UNKNOWN."""
        signed, approval = self._create_signed_manifest_and_approval("auth-test-contingency-unknown", max_dispatches=2)

        mock_err = self._create_mock_http_error(code=429, msg="Primary Rate Limited")
        # Second call raises ambiguous TimeoutError
        self.mock_opener.open.side_effect = [mock_err, TimeoutError("Contingency socket send timeout")]

        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            trust_store=self.trust_store,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "test"}]}

        resp = transport.dispatch_governed(
            modality_str="text",
            payload=payload,
            budget_id=signed.manifest.authorization_id,
            logical_attempt_id="att-test-contingency-unknown",
            signed_manifest=signed,
            human_approval=approval,
        )

        self.assertEqual(resp.status_code, 500)
        self.assertIn("AMBIGUOUS_CONTINGENCY_UNKNOWN", resp.raw_error)
        self.assertEqual(self.mock_opener.open.call_count, 2, "Must make 2 calls (primary + contingency attempt).")
        self.assertEqual(resp.governance_metadata["state"], STATE_UNKNOWN)
        self.assertEqual(resp.governance_metadata["fallback_action"], "CONTINGENCY_BLOCKED_UNKNOWN")

        # Verify second reservation in database is in STATE_UNKNOWN
        res2_id = resp.governance_metadata["reservation_id"]
        res2 = self.store.get_reservation(res2_id)
        self.assertIsNotNone(res2)
        self.assertEqual(res2.state, STATE_UNKNOWN, "Contingency reservation must be STATE_UNKNOWN.")

    # -------------------------------------------------------------------------
    # Test 16: require_governed_egress defaults to True when quota_store supplied
    # -------------------------------------------------------------------------
    def test_16_operational_configuration_defaults_require_governed_egress_to_true(self) -> None:
        """When quota_store is provided without specifying require_governed_egress, it defaults to True."""
        transport = OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            quota_store=self.store,
            projector=self.projector,
            opener=self.mock_opener,
            api_key="sk-test",
        )
        self.assertTrue(transport.require_governed_egress, "require_governed_egress must be True by default with quota_store.")

        # Attempting un-governed dispatch() MUST fail closed
        payload = {"model": self.primary_model, "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(GovernedEgressRequiredError):
            transport.dispatch("text", payload)

        self.assertEqual(self.mock_opener.open.call_count, 0, "Zero calls when legacy dispatch blocked by default.")


if __name__ == "__main__":
    unittest.main()
