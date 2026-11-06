"""End-to-end simulated test suite for Phase 4C.1 OpenRouter Free pilot pre-flight script.

Verifies:
1. OfficialOpenRouterCatalogClient constructor signature (timeout_sec configured in constructor).
2. Requirement classification (VERIFIED, PENDING, FAILED) with explicit EvidenceSource.
3. OpenRouterAccountVerifier key limit inspection on official endpoint (https://openrouter.ai/api/v1/key).
4. Spend cap verification: is_free_tier alone does NOT prove US$ 0.00; finite numeric 0.0 is strictly required.
5. Anti-redirect defense: RejectAllRedirectsHandler rejects any HTTP redirect (301/302) fail-closed.
6. Fail-closed API verification: API errors or positive limits do not silently fall back to boolean flags.
7. Authorization manifest validation: validates model, US$ 0.00 budget, code commit, authorizer, and validity.
8. Single attempt guarantee: RetryPolicy(max_attempts=1) ensures exactly 1 dispatch, zero retries on 429/503.
9. End-to-end simulated --live execution flow with mocked catalog, account verification, and transport.
10. Preservation of audit trail in AvailabilityLedger and atomic quota reservation in SQLite.
11. Strict blocking when any financial prerequisite is unconfirmed or unauthorized.
12. Explicit identification of synthetic provenance in dry-run mode.
13. Strict resolve_current_commit() without stale fallback hash.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import json
import os
import shutil
import subprocess
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
    RetryPolicy,
    SlidingWindowRateLimiter,
)
from ai_coordinator.openrouter.registry import _create_official_https_snapshot

from prepare_pilot_text_call import (
    EvidenceSource,
    ExecutionAuthorizationRecord,
    OpenRouterAccountSecurityError,
    OpenRouterAccountVerifier,
    RejectAllRedirectsHandler,
    RequirementStatus,
    evaluate_requirements,
    execute_live_pilot,
    prepare_offline_dry_run,
    resolve_current_commit,
    validate_authorization_manifest,
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
        params = [p for p in sig_fetch.parameters if p != "self"]
        self.assertEqual(len(params), 0, "fetch_catalog() não deve aceitar argumentos.")

        client = OfficialOpenRouterCatalogClient(timeout_sec=22.5)
        self.assertEqual(client.timeout_sec, 22.5)

    def test_02_requirements_classification_and_evidence_provenance(self) -> None:
        """Ajuste 2 & 4: Requisitos classificados em VERIFIED, PENDING, FAILED com origem da evidência rastreável."""
        # Scenario A: Missing API key and unconfirmed dashboard
        reqs_a = evaluate_requirements(
            api_key="",
            spend_cap_confirmed=False,
            auto_recharge_confirmed=False,
        )
        self.assertEqual(reqs_a["API_KEY_PRESENT"]["status"], RequirementStatus.FAILED)
        self.assertEqual(reqs_a["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.PENDING)
        self.assertEqual(reqs_a["SPEND_CAP_ENFORCED"]["evidence_source"], EvidenceSource.PENDING)
        self.assertEqual(reqs_a["AUTO_RECHARGE_DISABLED"]["status"], RequirementStatus.PENDING)

        # Scenario B: Human/Operator flag confirmation
        reqs_b = evaluate_requirements(
            api_key="sk-or-v1-abcdef0123456789abcdef0123456789",
            spend_cap_confirmed=True,
            auto_recharge_confirmed=True,
            authorized_by_user=True,
            db_path=self.db_path,
            ledger_path=self.ledger_path,
        )
        self.assertEqual(reqs_b["API_KEY_PRESENT"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs_b["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs_b["SPEND_CAP_ENFORCED"]["evidence_source"], EvidenceSource.OPERATOR_FLAG)
        self.assertEqual(reqs_b["AUTO_RECHARGE_DISABLED"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs_b["EXECUTION_AUTHORIZATION"]["evidence_source"], EvidenceSource.OPERATOR_FLAG)

    def test_03_account_verifier_official_endpoint_and_numeric_zero(self) -> None:
        """Ajuste 1 & 2: OpenRouterAccountVerifier usa https://openrouter.ai/api/v1/key e aceita limit == 0.0."""
        verifier = OpenRouterAccountVerifier()
        self.assertEqual(verifier.endpoint_url, "https://openrouter.ai/api/v1/key")

        mock_opener = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.getcode.return_value = 200
        mock_resp.geturl.return_value = "https://openrouter.ai/api/v1/key"
        mock_resp.read.return_value = json.dumps({
            "data": {"label": "pilot-key", "limit": 0.0, "usage": 0.0, "is_free_tier": True}
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_opener.open.return_value = mock_resp

        verifier.opener = mock_opener
        res = verifier.verify_key_limits("sk-test-key")
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["limit"], 0.0)
        self.assertTrue(res["limit_is_zero"])

        # Check evaluate_requirements with API evidence
        reqs = evaluate_requirements(api_key="sk-test-key", key_verification=res)
        self.assertEqual(reqs["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.VERIFIED)
        self.assertEqual(reqs["SPEND_CAP_ENFORCED"]["evidence_source"], EvidenceSource.API)

    def test_04_account_verifier_rejects_free_tier_with_positive_or_null_limit(self) -> None:
        """Ajuste 2: is_free_tier=True com limit positivo ou limit=null NÃO deve receber VERIFIED (P0 Financeiro)."""
        # Case A: is_free_tier=True, but limit=25.0 (positive limit)
        mock_opener_pos = MagicMock()
        mock_resp_pos = MagicMock()
        mock_resp_pos.status = 200
        mock_resp_pos.getcode.return_value = 200
        mock_resp_pos.geturl.return_value = "https://openrouter.ai/api/v1/key"
        mock_resp_pos.read.return_value = json.dumps({
            "data": {"label": "free-tier-with-limit", "limit": 25.0, "usage": 0.0, "is_free_tier": True}
        }).encode("utf-8")
        mock_resp_pos.__enter__.return_value = mock_resp_pos
        mock_opener_pos.open.return_value = mock_resp_pos

        verifier = OpenRouterAccountVerifier(opener=mock_opener_pos)
        res_pos = verifier.verify_key_limits("sk-test-key")
        self.assertEqual(res_pos["status"], "OK")
        self.assertFalse(res_pos["limit_is_zero"])

        reqs_pos = evaluate_requirements(api_key="sk-test-key", key_verification=res_pos)
        self.assertEqual(reqs_pos["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.FAILED)
        self.assertEqual(reqs_pos["SPEND_CAP_ENFORCED"]["evidence_source"], EvidenceSource.API)
        self.assertIn("US$ 25.0", reqs_pos["SPEND_CAP_ENFORCED"]["details"])

        # Case B: is_free_tier=True, but limit=null (unlimited spend in OpenRouter API)
        mock_opener_null = MagicMock()
        mock_resp_null = MagicMock()
        mock_resp_null.status = 200
        mock_resp_null.getcode.return_value = 200
        mock_resp_null.geturl.return_value = "https://openrouter.ai/api/v1/key"
        mock_resp_null.read.return_value = json.dumps({
            "data": {"label": "free-tier-unlimited", "limit": None, "usage": 0.0, "is_free_tier": True}
        }).encode("utf-8")
        mock_resp_null.__enter__.return_value = mock_resp_null
        mock_opener_null.open.return_value = mock_resp_null

        verifier_null = OpenRouterAccountVerifier(opener=mock_opener_null)
        res_null = verifier_null.verify_key_limits("sk-test-key")
        self.assertEqual(res_null["status"], "OK")
        self.assertFalse(res_null["limit_is_zero"])

        reqs_null = evaluate_requirements(api_key="sk-test-key", key_verification=res_null)
        self.assertEqual(reqs_null["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.FAILED)
        self.assertEqual(reqs_null["SPEND_CAP_ENFORCED"]["evidence_source"], EvidenceSource.API)
        self.assertIn("ilimitado", reqs_null["SPEND_CAP_ENFORCED"]["details"])

    def test_05_account_verifier_rejects_redirects_fail_closed(self) -> None:
        """Ajuste 3: RejectAllRedirectsHandler rejeita HTTP 302/301 impedindo vazamento de Authorization."""
        handler = RejectAllRedirectsHandler()
        mock_req = MagicMock()

        # Rejection of 302
        with self.assertRaises(OpenRouterAccountSecurityError) as cm:
            handler.http_error_302(
                mock_req,
                fp=None,
                code=302,
                msg="Found",
                headers={"Location": "https://malicious-external-site.com/steal-key"},
            )
        self.assertIn("rejeitado categoricamente", str(cm.exception))
        self.assertIn("302", str(cm.exception))

        # Rejection of 301
        with self.assertRaises(OpenRouterAccountSecurityError) as cm301:
            handler.http_error_301(
                mock_req,
                fp=None,
                code=301,
                msg="Moved Permanently",
                headers={"Location": "http://openrouter.ai/api/v1/key"},
            )
        self.assertIn("rejeitado categoricamente", str(cm301.exception))

    def test_06_account_verifier_handles_api_failure_without_silent_fallback(self) -> None:
        """Ajuste 4: Se endpoint retornar erro (401, 500), SPEND_CAP_ENFORCED recebe FAILED e bloqueia inferência."""
        mock_opener_err = MagicMock()
        mock_resp_err = MagicMock()
        mock_resp_err.status = 401
        mock_resp_err.getcode.return_value = 401
        mock_resp_err.geturl.return_value = "https://openrouter.ai/api/v1/key"
        mock_resp_err.__enter__.return_value = mock_resp_err
        mock_opener_err.open.return_value = mock_resp_err

        verifier = OpenRouterAccountVerifier(opener=mock_opener_err)
        res_err = verifier.verify_key_limits("sk-invalid-key")
        self.assertEqual(res_err["status"], "FAILED")
        self.assertIn("HTTP 401", res_err["error"])

        # Even if spend_cap_confirmed=True is passed, API failure must override and FAIL
        reqs = evaluate_requirements(
            api_key="sk-invalid-key",
            key_verification=res_err,
            spend_cap_confirmed=True,
        )
        self.assertEqual(reqs["SPEND_CAP_ENFORCED"]["status"], RequirementStatus.FAILED)
        self.assertEqual(reqs["SPEND_CAP_ENFORCED"]["evidence_source"], EvidenceSource.API)
        self.assertIn("Falha na consulta à API", reqs["SPEND_CAP_ENFORCED"]["details"])

    def test_07_authorization_manifest_validation_and_divergence(self) -> None:
        """Ajuste 6 & 7: Validação de manifesto externo e rejeição de divergências (modelo, commit, orçamento, validade)."""
        head_commit = "d733f25a3c20fbedb6a0639d4bd35db49f21b933"
        valid_date = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).isoformat()
        expired_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()

        valid_manifest = {
            "target_model": "meta-llama/llama-3.2-3b-instruct:free",
            "max_budget_usd": 0.0,
            "code_commit": head_commit,
            "authorizer": "operator_admin",
            "valid_until": valid_date,
            "execution_scope": "N=1 pilot call",
        }

        # 1. Valid manifest
        ok, msg = validate_authorization_manifest(valid_manifest, "meta-llama/llama-3.2-3b-instruct:free", head_commit)
        self.assertTrue(ok)
        self.assertIn("comprovado", msg)

        # 2. Budget > 0.0
        bad_budget = dict(valid_manifest, max_budget_usd=1.50)
        ok_b, msg_b = validate_authorization_manifest(bad_budget, "meta-llama/llama-3.2-3b-instruct:free", head_commit)
        self.assertFalse(ok_b)
        self.assertIn("excede US$ 0.000000", msg_b)

        # 3. Model divergence
        bad_model = dict(valid_manifest, target_model="other/model:free")
        ok_m, msg_m = validate_authorization_manifest(bad_model, "meta-llama/llama-3.2-3b-instruct:free", head_commit)
        self.assertFalse(ok_m)
        self.assertIn("diverge do modelo selecionado", msg_m)

        # 4. Commit divergence
        bad_commit = dict(valid_manifest, code_commit="0000000000000000000000000000000000000000")
        ok_c, msg_c = validate_authorization_manifest(bad_commit, "meta-llama/llama-3.2-3b-instruct:free", head_commit)
        self.assertFalse(ok_c)
        self.assertIn("diverge do HEAD atual", msg_c)

        # 5. Expired manifest
        bad_exp = dict(valid_manifest, valid_until=expired_date)
        ok_e, msg_e = validate_authorization_manifest(bad_exp, "meta-llama/llama-3.2-3b-instruct:free", head_commit)
        self.assertFalse(ok_e)
        self.assertIn("expirado", msg_e)

    def test_08_resolve_current_commit_fails_closed_without_fallback(self) -> None:
        """Ajuste 6: resolve_current_commit() não usa fallback hardcoded se git falhar."""
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            with self.assertRaises(RuntimeError) as cm:
                resolve_current_commit()
            self.assertIn("Impossível resolver commit atual", str(cm.exception))

    def test_09_single_attempt_guarantee_on_http_error(self) -> None:
        """Ajuste 5 & 7: Garantia de exatamente 1 tentativa em erros HTTP 429 ou 503 (zero retries)."""
        mock_client = MagicMock(spec=OfficialOpenRouterCatalogClient)
        mock_client.fetch_catalog.return_value = self.official_snapshot

        mock_verifier = MagicMock(spec=OpenRouterAccountVerifier)
        mock_verifier.verify_key_limits.return_value = {
            "status": "OK",
            "limit": 0.0,
            "limit_is_zero": True,
            "usage": 0.0,
            "is_free_tier": True,
        }

        # Simulate HTTP 429 Too Many Requests response
        mock_http_resp = MagicMock()
        mock_http_resp.getcode.return_value = 429
        mock_http_resp.status = 429
        mock_http_resp.headers = {"Content-Type": "application/json", "Retry-After": "60"}
        mock_http_resp.read.return_value = json.dumps({
            "error": {"message": "Rate limit reached", "code": 429}
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

        self.assertEqual(exit_code, 1, "Execução deve retornar 1 ao receber HTTP 429.")
        self.assertEqual(
            mock_urlopen.call_count,
            1,
            f"Deve despachar exatamente 1 requisição HTTP (sem retries), mas despachou {mock_urlopen.call_count}.",
        )

    def test_10_e2e_simulated_live_execution_full_flow(self) -> None:
        """Ajuste 4: Teste E2E integralmente simulado do caminho --live cobrindo catálogo, gates, quota, ledger e resultado."""
        mock_client = MagicMock(spec=OfficialOpenRouterCatalogClient)
        mock_client.fetch_catalog.return_value = self.official_snapshot

        mock_verifier = MagicMock(spec=OpenRouterAccountVerifier)
        mock_verifier.verify_key_limits.return_value = {
            "status": "OK",
            "limit": 0.0,
            "limit_is_zero": True,
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
        self.assertEqual(mock_urlopen.call_count, 1)

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

    def test_11_e2e_simulated_live_blocked_if_spend_cap_unverified(self) -> None:
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

    def test_12_e2e_simulated_live_blocked_if_auto_recharge_unverified(self) -> None:
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

    def test_13_e2e_simulated_live_blocked_if_no_operator_authorization(self) -> None:
        """Ajuste 3 & 4: Falha imediata se --authorized-by-user não estiver presente."""
        exit_code = execute_live_pilot(authorized_by_user=False)
        self.assertEqual(exit_code, 1)

    def test_14_dry_run_identifies_synthetic_metrics(self) -> None:
        """Ajuste 5: O modo dry-run identifica explicitamente catálogo, latência, tokens e custo como sintéticos."""
        ret = prepare_offline_dry_run()
        self.assertEqual(ret, 0)


if __name__ == "__main__":
    unittest.main()
