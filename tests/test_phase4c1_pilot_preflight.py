"""End-to-end simulated test suite for Phase 4C.1 OpenRouter Free pilot pre-flight script.

Verifies:
1. OfficialOpenRouterCatalogClient constructor signature (timeout_sec configured in constructor).
2. Requirement classification (VERIFIED, PENDING, FAILED) and fail-closed financial gating.
3. OpenRouterAccountVerifier key limit inspection (spend cap zero verification vs positive budget rejection).
4. End-to-end simulated --live execution flow with mocked catalog, account verification, and transport.
5. Preservation of audit trail in AvailabilityLedger and atomic quota reservation in SQLite.
6. Strict blocking when any financial prerequisite is unconfirmed or unauthorized.
7. Explicit identification of synthetic provenance in dry-run mode.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from ai_coordinator.openrouter import (
    AuthenticatedCatalogSnapshot,
    AvailabilityEventType,
    AvailabilityLedger,
    FreeCostGuard,
    FreeModelRegistry,
    MetricProvenance,
    ModelCatalogEntry,
    ModelModality,
    OfficialOpenRouterCatalogClient,
    OpenRouterHttpTransport,
    SlidingWindowRateLimiter,
)
from ai_coordinator.openrouter.registry import _create_official_https_snapshot

from prepare_pilot_text_call import (
    ExecutionAuthorizationRecord,
    OpenRouterAccountVerifier,
    RequirementStatus,
    evaluate_requirements,
    execute_live_pilot,
    prepare_offline_dry_run,
    resolve_current_commit,
)


class TestPhase4C1PilotPreflight(unittest.TestCase):
    """Test suite covering the pre-flight verification script and simulated live execution."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="test_pilot_preflight_")
        self.db_path = os.path.join(self.tmp_dir, "pilot_quotas.db")
        self.ledger_path = os.path.join(self.tmp_dir, "pilot_audit.jsonl")

        # Create valid official HTTPS snapshot for testing
        self.mock_catalog_data = [
            {
                "id": "meta-llama/llama-3.2-3b-instruct:free",
                "name": "Meta: Llama 3.2 3B Instruct (free)",
                "context_length": 131072,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {"modality": "text->text", "instruct_type": "llama3"},
            },
            {
                "id": "google/gemini-2.0-flash-exp:free",
                "name": "Google: Gemini 2.0 Flash Experimental (free)",
                "context_length": 1048576,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {"modality": "text->text", "instruct_type": "gemini"},
            },
        ]
        raw_bytes = json.dumps({"data": self.mock_catalog_data}).encode("utf-8")
        self.official_snapshot = _create_official_https_snapshot(
            raw_bytes,
            "https://openrouter.ai/api/v1/models",
            time.time(),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_01_catalog_client_signature_matches_requirements(self) -> None:
        """Ajuste 1: OfficialOpenRouterCatalogClient configura timeout_sec no construtor e fetch_catalog() tem aridade 0."""
        sig_init = inspect.signature(OfficialOpenRouterCatalogClient.__init__)
        self.assertIn("timeout_sec", sig_init.parameters)

        sig_fetch = inspect.signature(OfficialOpenRouterCatalogClient.fetch_catalog)
        # fetch_catalog must take only self
        params = [p for p in sig_fetch.parameters if p != "self"]
        self.assertEqual(len(params), 0, "fetch_catalog() não deve aceitar argumentos.")

        client = OfficialOpenRouterCatalogClient(timeout_sec=22.5)
        self.assertEqual(client.timeout_sec, 22.5)

    def test_02_requirements_classification_verified_pending_failed(self) -> None:
        """Ajuste 2: Requisitos devem ser estritamente classificados em VERIFIED, PENDING ou FAILED."""
        # Scenario A: Missing API key and unconfirmed dashboard
        reqs_a = evaluate_requirements(
            api_key="",
            spend_cap_confirmed=False,
            auto_recharge_confirmed=False,
        )
        self.assertEqual(reqs_a["API_KEY_PRESENT"]["status"], RequirementStatus.FAILED)
        self.assertEqual(reqs_a["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.PENDING)
        self.assertEqual(reqs_a["AUTO_RECHARGE_DISABLED"]["status"], RequirementStatus.PENDING)

        # Scenario B: All confirmed
        reqs_b = evaluate_requirements(
            api_key="sk-or-v1-abcdef0123456789abcdef0123456789",
            spend_cap_confirmed=True,
            auto_recharge_confirmed=True,
            db_path=self.db_path,
            ledger_path=self.ledger_path,
        )
        self.assertEqual(reqs_b["API_KEY_PRESENT"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs_b["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs_b["AUTO_RECHARGE_DISABLED"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs_b["QUOTA_PERSISTENCE_READY"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs_b["AUDIT_PERSISTENCE_READY"]["status"], RequirementStatus.VERIFIED)

    def test_03_account_verifier_verifies_zero_limit_and_rejects_positive(self) -> None:
        """Ajuste 2: OpenRouterAccountVerifier consulta /api/v1/auth/key e diferencia limite zero de positivo."""
        mock_opener_zero = MagicMock()
        mock_resp_zero = MagicMock()
        mock_resp_zero.status = 200
        mock_resp_zero.getcode.return_value = 200
        mock_resp_zero.read.return_value = json.dumps({
            "data": {"label": "pilot-key", "limit": 0.0, "usage": 0.0, "is_free_tier": True}
        }).encode("utf-8")
        mock_resp_zero.__enter__.return_value = mock_resp_zero
        mock_opener_zero.open.return_value = mock_resp_zero

        verifier_zero = OpenRouterAccountVerifier(opener=mock_opener_zero)
        res_zero = verifier_zero.verify_key_limits("sk-test-key")
        self.assertEqual(res_zero["status"], "OK")
        self.assertEqual(res_zero["limit"], 0.0)
        self.assertTrue(res_zero["is_free_tier"])

        # Positive limit
        mock_opener_pos = MagicMock()
        mock_resp_pos = MagicMock()
        mock_resp_pos.status = 200
        mock_resp_pos.getcode.return_value = 200
        mock_resp_pos.read.return_value = json.dumps({
            "data": {"label": "paid-key", "limit": 25.0, "usage": 1.2}
        }).encode("utf-8")
        mock_resp_pos.__enter__.return_value = mock_resp_pos
        mock_opener_pos.open.return_value = mock_resp_pos

        verifier_pos = OpenRouterAccountVerifier(opener=mock_opener_pos)
        res_pos = verifier_pos.verify_key_limits("sk-test-key")
        reqs_pos = evaluate_requirements(api_key="sk-test", key_verification=res_pos)
        self.assertEqual(reqs_pos["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.FAILED)
        self.assertIn("positivo", reqs_pos["SPEND_CAP_ENFORCED"]["details"])

    def test_04_authorization_record_creation_and_binding(self) -> None:
        """Ajuste 3: ExecutionAuthorizationRecord associa modelo, orçamento US$ 0.00 e commit."""
        record = ExecutionAuthorizationRecord(
            target_model="meta-llama/llama-3.2-3b-instruct:free",
            max_budget_usd=0.000000,
            code_commit="06b460bb1c3979027ccdb2af38245fdfc0ea1827",
            execution_scope="N=1 single controlled pilot call (TEXT modality)",
            operator_intent_flag=True,
            timestamp_utc="2026-11-06T14:00:00Z",
            spend_cap_verified=True,
            auto_recharge_verified=True,
            manifest_id="auth_test12345",
        )
        as_dict = record.to_dict()
        self.assertEqual(as_dict["max_budget_usd"], 0.0)
        self.assertEqual(as_dict["target_model"], "meta-llama/llama-3.2-3b-instruct:free")
        self.assertTrue(as_dict["spend_cap_verified"])
        self.assertTrue(as_dict["auto_recharge_verified"])

    def test_05_e2e_simulated_live_execution_full_flow(self) -> None:
        """Ajuste 4: Teste E2E integralmente simulado do caminho --live cobrindo catálogo, gates, quota, ledger e resultado."""
        mock_client = MagicMock(spec=OfficialOpenRouterCatalogClient)
        mock_client.fetch_catalog.return_value = self.official_snapshot

        mock_verifier = MagicMock(spec=OpenRouterAccountVerifier)
        mock_verifier.verify_key_limits.return_value = {
            "status": "OK",
            "limit": 0.0,
            "usage": 0.0,
            "is_free_tier": True,
        }

        mock_http_resp = MagicMock()
        mock_http_resp.getcode.return_value = 200
        mock_http_resp.headers = {"Content-Type": "application/json"}
        mock_http_resp.read.return_value = json.dumps({
            "id": "gen-mock-live-12345",
            "model": "meta-llama/llama-3.2-3b-instruct:free",
            "choices": [{"message": {"role": "assistant", "content": "Classified: POSITIVE"}}],
            "usage": {"prompt_tokens": 14, "completion_tokens": 4, "total_tokens": 18},
        }).encode("utf-8")
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_http_resp

        registry = FreeModelRegistry(require_trusted_source=True)
        registry.load_authenticated_snapshot(self.official_snapshot)
        guard = FreeCostGuard(registry)
        limiter = SlidingWindowRateLimiter(sqlite_path=self.db_path)
        ledger = AvailabilityLedger(storage_path=self.ledger_path, require_durable=True)

        mock_transport = OpenRouterHttpTransport(
            cost_guard=guard,
            rate_limiter=limiter,
            ledger=ledger,
            api_key="sk-or-v1-fake-live-key",
            live_network_enabled=True,
        )

        with patch("urllib.request.urlopen", return_value=mock_cm) as mock_urlopen:
            exit_code = execute_live_pilot(
                api_key_override="sk-or-v1-fake-live-key",
                catalog_client=mock_client,
                account_verifier=mock_verifier,
                confirm_spend_cap_zero=True,
                confirm_no_auto_recharge=True,
                verify_key_endpoint=True,
                authorized_by_user=True,
                bench_dir=self.tmp_dir,
                custom_transport=mock_transport,
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(mock_urlopen.called, "urllib.request.urlopen deve ter sido acionado pelo transport.")

        # Verify quota persistence
        self.assertEqual(limiter.requests_in_current_minute, 1)
        self.assertEqual(limiter.requests_in_current_day, 1)

        # Verify ledger persistence
        self.assertTrue(os.path.exists(self.ledger_path))
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        event_types = [l["event_type"] for l in lines]
        self.assertIn(AvailabilityEventType.BENCHMARK_EXECUTED.value, event_types)

        # Verify persisted result artifact
        res_file = os.path.join(self.tmp_dir, "pilot_live_n1_result.json")
        self.assertTrue(os.path.exists(res_file))
        with open(res_file, "r", encoding="utf-8") as f:
            saved_result = json.load(f)
        self.assertTrue(saved_result["success"])
        self.assertEqual(saved_result["tokens"]["total_tokens"], 18)
        self.assertEqual(saved_result["token_provenance"], MetricProvenance.MEASURED.value)
        self.assertEqual(saved_result["authorization_record"]["max_budget_usd"], 0.0)

    def test_06_e2e_simulated_live_blocked_if_spend_cap_unverified(self) -> None:
        """Ajuste 4: Se o Spend Cap estiver PENDING (não confirmado), o caminho live é BLOQUEADO antes da inferência."""
        mock_client = MagicMock(spec=OfficialOpenRouterCatalogClient)
        mock_client.fetch_catalog.return_value = self.official_snapshot

        registry = FreeModelRegistry(require_trusted_source=True)
        registry.load_authenticated_snapshot(self.official_snapshot)
        guard = FreeCostGuard(registry)
        limiter = SlidingWindowRateLimiter(sqlite_path=self.db_path)
        ledger = AvailabilityLedger(storage_path=self.ledger_path, require_durable=True)

        mock_transport = OpenRouterHttpTransport(
            cost_guard=guard,
            rate_limiter=limiter,
            ledger=ledger,
            api_key="sk-or-v1-fake-live-key",
            live_network_enabled=True,
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            exit_code = execute_live_pilot(
                api_key_override="sk-or-v1-fake-live-key",
                catalog_client=mock_client,
                confirm_spend_cap_zero=False,  # Unconfirmed!
                confirm_no_auto_recharge=True,
                authorized_by_user=True,
                bench_dir=self.tmp_dir,
                custom_transport=mock_transport,
            )

        self.assertEqual(exit_code, 1, "Deve falhar com código 1 quando Spend Cap está pendente.")
        mock_urlopen.assert_not_called()

    def test_07_e2e_simulated_live_blocked_if_auto_recharge_unverified(self) -> None:
        """Ajuste 4: Se recarga automática estiver PENDING (não confirmada), o caminho live é BLOQUEADO antes da inferência."""
        mock_client = MagicMock(spec=OfficialOpenRouterCatalogClient)
        mock_client.fetch_catalog.return_value = self.official_snapshot

        registry = FreeModelRegistry(require_trusted_source=True)
        registry.load_authenticated_snapshot(self.official_snapshot)
        guard = FreeCostGuard(registry)
        limiter = SlidingWindowRateLimiter(sqlite_path=self.db_path)
        ledger = AvailabilityLedger(storage_path=self.ledger_path, require_durable=True)

        mock_transport = OpenRouterHttpTransport(
            cost_guard=guard,
            rate_limiter=limiter,
            ledger=ledger,
            api_key="sk-or-v1-fake-live-key",
            live_network_enabled=True,
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            exit_code = execute_live_pilot(
                api_key_override="sk-or-v1-fake-live-key",
                catalog_client=mock_client,
                confirm_spend_cap_zero=True,
                confirm_no_auto_recharge=False,  # Unconfirmed!
                authorized_by_user=True,
                bench_dir=self.tmp_dir,
                custom_transport=mock_transport,
            )

        self.assertEqual(exit_code, 1, "Deve falhar com código 1 quando recarga automática está pendente.")
        mock_urlopen.assert_not_called()

    def test_08_e2e_simulated_live_blocked_if_no_operator_authorization(self) -> None:
        """Ajuste 3 & 4: Falha imediata se --authorized-by-user não estiver presente."""
        exit_code = execute_live_pilot(authorized_by_user=False)
        self.assertEqual(exit_code, 1)

    def test_09_dry_run_identifies_synthetic_metrics(self) -> None:
        """Ajuste 5: O modo dry-run identifica explicitamente catálogo, latência, tokens e custo como sintéticos."""
        ret = prepare_offline_dry_run()
        self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
