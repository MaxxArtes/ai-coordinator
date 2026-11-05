"""Unit and adversarial test suite for OpenRouter Free Model Integration (Phase 4C.1).

Validates:
1. Dynamic catalog parsing, zero-cost qualification, and modality inference.
2. Strict fail-closed rejection of paid models, fallbacks, and uncataloged endpoints.
3. parse_float_cost strict parsing (rejects None, negative, NaN, Inf, non-numeric).
4. Rejection of models with :free suffix but non-zero or invalid pricing.
5. Routing and fallback parameter inspection (rejects ambiguous routes and paid fallbacks).
6. Fail-closed modality and tool support compatibility enforcement.
7. Embedded credential detection in prompt texts (rejects Bearer, sk-*, PEM keys).
8. Sliding window rate limiter with quota consumption on every retry attempt.
9. Persistent rate limiter and persistent availability audit ledger.
10. Retry-After header awareness and real backoff delay execution.
11. Metric provenance classification (SYNTHETIC vs MEASURED vs UNKNOWN).
12. HTTP transport default fail-closed state and mocked network dispatch.
13. Centralized Egress Point: transport.dispatch() cannot bypass CostGuard.
14. Catalog freshness TTL enforcement: stale catalog raises StaleCatalogError.
15. Atomic cross-process SQLite quota reservation race condition protection.
16. Ledger persistence failure fail-closed behavior (AuditPersistenceError).
17. Responses missing 'usage' field are classified strictly as UNKNOWN.
18. Unknown modalities reject fail-closed without fallback to /chat/completions.
"""
from __future__ import annotations

import concurrent.futures
import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from ai_coordinator.openrouter import (
    AmbiguousRoutingError,
    AuditPersistenceError,
    AuthenticatedCatalogSnapshot,
    AvailabilityEvent,
    AvailabilityEventType,
    AvailabilityLedger,
    BaseModelAdapter,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    ConfidentialDataDetectedError,
    CostGuardError,
    DecisionModelAdapter,
    EmbeddingModelAdapter,
    FreeCostGuard,
    FreeModelRegistry,
    HttpResponse,
    IncompatibleModalityEndpointError,
    IncompatibleModalityError,
    InvalidPricingError,
    LiveNetworkDisabledError,
    MetricProvenance,
    ModelBenchmarkRunResult,
    ModelBenchmarkRunner,
    ModelCatalogEntry,
    ModelModality,
    ModelPricing,
    OfficialOpenRouterCatalogClient,
    OpenRouterHttpTransport,
    PaidFallbackBlockedError,
    PricingStatus,
    QuotaPersistenceError,
    RateLimitExceededError,
    RerankModelAdapter,
    RetryPolicy,
    SQLiteQuotaStore,
    SlidingWindowRateLimiter,
    SpeechModelAdapter,
    StaleCatalogError,
    StrictOpenRouterRedirectHandler,
    TextModelAdapter,
    TransportError,
    UnauthorizedModelError,
    UntrustedCatalogSourceError,
    parse_float_cost,
    scrub_outbound_payload,
)
from ai_coordinator.openrouter.registry import _PROCESS_CATALOG_SECRET


def _mp_quota_worker(db_path: str, account_id: str, max_per_minute: int, max_per_day: int, out_queue: Any) -> None:
    """Worker function executed in an isolated operating system process for multiprocessing test."""
    try:
        from ai_coordinator.openrouter.resilience import SlidingWindowRateLimiter
        limiter = SlidingWindowRateLimiter(
            account_id=account_id,
            max_per_minute=max_per_minute,
            max_per_day=max_per_day,
            sqlite_path=db_path,
        )
        granted = limiter.acquire()
        out_queue.put(granted)
    except Exception as exc:
        out_queue.put(f"EXCEPTION: {exc}")


def build_synthetic_openrouter_catalog() -> dict:
    """Generate representative OpenRouter catalog containing free and paid models across modalities."""
    return {
        "data": [
            {
                "id": "meta-llama/llama-3.2-3b-instruct:free",
                "name": "Meta: Llama 3.2 3B Instruct (free)",
                "description": "Lightweight text chat model with tool calling",
                "context_length": 131072,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "text->text",
                    "supported_parameters": ["tools", "temperature", "max_tokens"],
                },
                "created": 1727280000,
            },
            {
                "id": "google/gemini-2.0-flash-exp:free",
                "name": "Google: Gemini 2.0 Flash Experimental (free)",
                "description": "High performance multimodal text and decision model",
                "context_length": 1048576,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "text->text",
                    "supported_parameters": ["tools", "tool_choice", "temperature"],
                },
                "created": 1734000000,
            },
            {
                "id": "nvidia/nemotron-rerank-8b:free",
                "name": "NVIDIA: Nemotron Rerank 8B (free)",
                "description": "Specialized neural document reranker",
                "context_length": 8192,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "text->rerank",
                    "supported_parameters": ["temperature"],
                },
                "created": 1731000000,
            },
            {
                "id": "baai/bge-large-en-v1.5:free",
                "name": "BAAI: BGE Large English (free)",
                "description": "Semantic embeddings model",
                "context_length": 512,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "text->embedding",
                    "supported_parameters": [],
                },
                "created": 1700000000,
            },
            {
                "id": "openai/whisper-large-v3:free",
                "name": "OpenAI: Whisper Large V3 (free)",
                "description": "Speech recognition and audio transcription",
                "context_length": 448,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "audio->text",
                    "supported_parameters": [],
                },
                "created": 1715000000,
            },
            {
                "id": "fishaudio/fish-speech-1.5:free",
                "name": "Fish Audio: Fish Speech 1.5 (free)",
                "description": "Text to speech generation model",
                "context_length": 4096,
                "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "text->audio",
                    "supported_parameters": [],
                },
                "created": 1720000000,
            },
            {
                "id": "adversarial/sneaky-model:free",
                "name": "Adversarial Sneaky Model",
                "description": "Claims to be free in ID but charges money",
                "context_length": 4096,
                "pricing": {"prompt": "0.0001", "completion": "0.0002", "image": "0", "request": "0"},
                "architecture": {"modality": "text->text"},
                "created": 1735000000,
            },
            {
                "id": "openai/gpt-4o",
                "name": "OpenAI: GPT-4o",
                "description": "Flagship paid model",
                "context_length": 128000,
                "pricing": {"prompt": "0.000005", "completion": "0.000015", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "text->text",
                    "supported_parameters": ["tools"],
                },
                "created": 1715600000,
            },
            {
                "id": "anthropic/claude-3.5-sonnet",
                "name": "Anthropic: Claude 3.5 Sonnet",
                "description": "Advanced paid model",
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015", "image": "0", "request": "0"},
                "architecture": {
                    "modality": "text->text",
                    "supported_parameters": ["tools"],
                },
                "created": 1718800000,
            },
        ]
    }


class TestPhase4C1OpenRouterFree(unittest.TestCase):
    """Test suite for OpenRouter free models registry, cost guard, and resilience."""

    def setUp(self) -> None:
        self.raw_catalog = build_synthetic_openrouter_catalog()
        self.registry = FreeModelRegistry(max_age_seconds=3600.0)
        self.registry.load_catalog_data(self.raw_catalog, timestamp="2026-11-05T10:00:00Z")
        self.cost_guard = FreeCostGuard(self.registry)
        self.ledger = AvailabilityLedger()

    def _create_authenticated_guard(self) -> FreeCostGuard:
        mock_http_response = MagicMock()
        mock_http_response.getcode.return_value = 200
        mock_http_response.geturl.return_value = "https://openrouter.ai/api/v1/models"
        mock_http_response.headers = {"Content-Type": "application/json"}
        mock_http_response.read.return_value = json.dumps(self.raw_catalog).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_http_response

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            client = OfficialOpenRouterCatalogClient()
            snapshot = client.fetch_catalog()

        reg = FreeModelRegistry(max_age_seconds=3600.0, require_trusted_source=True)
        reg.load_authenticated_snapshot(snapshot)
        return FreeCostGuard(reg)

    def test_01_registry_parsing_and_modality_classification(self) -> None:
        """Validate dynamic catalog parsing, zero-cost qualification, and modality inference."""
        self.assertEqual(self.registry.total_models, 9)
        free_models = self.registry.get_free_models()
        self.assertEqual(len(free_models), 6, "Exatamente 6 dos 9 modelos devem ser qualificados como gratuitos")

        # Verify Text Modality
        text_models = self.registry.get_by_modality(ModelModality.TEXT)
        text_ids = [m.id for m in text_models]
        self.assertIn("meta-llama/llama-3.2-3b-instruct:free", text_ids)
        self.assertIn("google/gemini-2.0-flash-exp:free", text_ids)

        # Verify Rerank Modality
        rerank_models = self.registry.get_by_modality(ModelModality.RERANK)
        self.assertEqual(len(rerank_models), 1)
        self.assertEqual(rerank_models[0].id, "nvidia/nemotron-rerank-8b:free")

        # Verify Embedding Modality
        embed_models = self.registry.get_by_modality(ModelModality.EMBEDDING)
        self.assertEqual(len(embed_models), 1)
        self.assertEqual(embed_models[0].id, "baai/bge-large-en-v1.5:free")

        # Verify Speech Modality (Transcription and Generation)
        speech_models = self.registry.get_by_modality(ModelModality.SPEECH)
        self.assertEqual(len(speech_models), 2)
        speech_ids = [m.id for m in speech_models]
        self.assertIn("openai/whisper-large-v3:free", speech_ids)
        self.assertIn("fishaudio/fish-speech-1.5:free", speech_ids)

        trans_models = self.registry.get_by_modality(ModelModality.SPEECH_TRANSCRIPTION)
        self.assertEqual(len(trans_models), 1)
        self.assertEqual(trans_models[0].id, "openai/whisper-large-v3:free")

        gen_models = self.registry.get_by_modality(ModelModality.SPEECH_GENERATION)
        self.assertEqual(len(gen_models), 1)
        self.assertEqual(gen_models[0].id, "fishaudio/fish-speech-1.5:free")

        # Verify Decision Modality
        decision_models = self.registry.get_by_modality(ModelModality.DECISIONS)
        dec_ids = [m.id for m in decision_models]
        self.assertIn("google/gemini-2.0-flash-exp:free", dec_ids)
        self.assertIn("meta-llama/llama-3.2-3b-instruct:free", dec_ids)

    def test_02_cost_guard_fail_closed_adversarial_rejections(self) -> None:
        """Validate strict fail-closed rejection of paid models, fallbacks, and uncataloged endpoints."""
        entry = self.cost_guard.authorize_model_execution("meta-llama/llama-3.2-3b-instruct:free")
        self.assertEqual(entry.pricing.prompt, 0.0)
        self.assertEqual(entry.pricing.completion, 0.0)
        self.assertTrue(entry.is_free)

        with self.assertRaises(UnauthorizedModelError) as ctx_paid:
            self.cost_guard.authorize_model_execution("openai/gpt-4o")
        self.assertIn("terminantemente proibidas", str(ctx_paid.exception))

        with self.assertRaises(UnauthorizedModelError):
            self.cost_guard.authorize_model_execution("anthropic/claude-3.5-sonnet")

        with self.assertRaises(UnauthorizedModelError):
            self.cost_guard.authorize_model_execution("adversarial/sneaky-model:free")

        with self.assertRaises(UnauthorizedModelError) as ctx_missing:
            self.cost_guard.authorize_model_execution("unknown-org/stealth-model")
        self.assertIn("não foi encontrado no catálogo", str(ctx_missing.exception))

        with self.assertRaises(PaidFallbackBlockedError) as ctx_fallback:
            self.cost_guard.sanitize_fallback_targets([
                "meta-llama/llama-3.2-3b-instruct:free",
                "openai/gpt-4o",
            ])
        self.assertIn("Tentativa de fallback bloqueada", str(ctx_fallback.exception))

        with self.assertRaises(UnauthorizedModelError):
            self.cost_guard.inspect_request_payload({"model": "openai/gpt-4o", "prompt": "hello"})

        self.assertGreater(self.cost_guard.blocked_attempts, 0)

    def test_03_parse_float_cost_adversarial_validation(self) -> None:
        """Determinação 1: parse_float_cost deve lançar InvalidPricingError para ausentes, negativos, NaN ou Inf."""
        self.assertEqual(parse_float_cost("0"), 0.0)
        self.assertEqual(parse_float_cost(0.0), 0.0)
        self.assertEqual(parse_float_cost("0.000005"), 0.000005)

        with self.assertRaises(InvalidPricingError):
            parse_float_cost(None)
        with self.assertRaises(InvalidPricingError):
            parse_float_cost("-0.01")
        with self.assertRaises(InvalidPricingError):
            parse_float_cost(-5.0)
        with self.assertRaises(InvalidPricingError):
            parse_float_cost("NaN")
        with self.assertRaises(InvalidPricingError):
            parse_float_cost("Infinity")
        with self.assertRaises(InvalidPricingError):
            parse_float_cost(float("inf"))
        with self.assertRaises(InvalidPricingError):
            parse_float_cost("invalid_cost_string")

    def test_04_free_qualification_requires_conjunction_and_rejects_free_suffix_alone(self) -> None:
        """Determinação 2: Exigir conjunção obrigatória de preço zero em todas as dimensões."""
        entry = self.registry.get_model("adversarial/sneaky-model:free")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.is_free)
        self.assertEqual(entry.pricing.status, PricingStatus.PAID)

        corrupt_catalog = {
            "data": [
                {
                    "id": "corrupt/nan-price:free",
                    "pricing": {"prompt": "NaN", "completion": "0"},
                }
            ]
        }
        test_registry = FreeModelRegistry()
        test_registry.load_catalog_data(corrupt_catalog)
        corrupt_entry = test_registry.get_model("corrupt/nan-price:free")
        self.assertIsNotNone(corrupt_entry)
        self.assertFalse(corrupt_entry.is_free)
        self.assertEqual(corrupt_entry.pricing.status, PricingStatus.INVALID)

    def test_05_routing_and_fallback_adversarial_inspection(self) -> None:
        """Determinação 3: Eliminar rotas ambíguas, fallbacks implícitos e parâmetros de roteamento desconhecidos."""
        self.cost_guard.inspect_request_payload({"model": "meta-llama/llama-3.2-3b-instruct:free"})

        with self.assertRaises(AmbiguousRoutingError):
            self.cost_guard.inspect_request_payload({
                "model": "meta-llama/llama-3.2-3b-instruct:free",
                "route": "cheapest",
            })

        with self.assertRaises(AmbiguousRoutingError):
            self.cost_guard.inspect_request_payload({
                "model": "meta-llama/llama-3.2-3b-instruct:free",
                "route": "paid",
            })

        with self.assertRaises(AmbiguousRoutingError):
            self.cost_guard.inspect_request_payload({
                "model": "meta-llama/llama-3.2-3b-instruct:free",
                "route": "fallback",
            })

        with self.assertRaises(PaidFallbackBlockedError):
            self.cost_guard.inspect_request_payload({
                "model": "meta-llama/llama-3.2-3b-instruct:free",
                "route": "fallback",
                "models": ["google/gemini-2.0-flash-exp:free", "openai/gpt-4o"],
            })

        self.cost_guard.inspect_request_payload({
            "model": "meta-llama/llama-3.2-3b-instruct:free",
            "route": "fallback",
            "models": ["google/gemini-2.0-flash-exp:free"],
        })

    def test_06_modality_and_tools_compatibility_enforcement(self) -> None:
        """Determinação 4: Bloquear modelos cuja modalidade ou parâmetros obrigatórios não tenham suporte comprovado."""
        text_adapter = TextModelAdapter(self.cost_guard)
        with self.assertRaises(IncompatibleModalityError) as ctx_mod:
            text_adapter.execute("baai/bge-large-en-v1.5:free", {"prompt": "Hello"})
        self.assertIn("não declara suporte à modalidade 'text'", str(ctx_mod.exception))

        rerank_adapter = RerankModelAdapter(self.cost_guard)
        with self.assertRaises(IncompatibleModalityError):
            rerank_adapter.execute("openai/whisper-large-v3:free", {"query": "q", "documents": ["d"]})

        decision_adapter = DecisionModelAdapter(self.cost_guard)
        with self.assertRaises(IncompatibleModalityError) as ctx_tools:
            decision_adapter.execute("baai/bge-large-en-v1.5:free", {
                "prompt": "Evaluate",
                "tools": [{"type": "function", "function": {"name": "test"}}],
            })
        self.assertIn("IncompatibleModalityError", ctx_tools.exception.__class__.__name__)

    def test_07_embedded_credential_detection_in_text(self) -> None:
        """Determinação 7: Detectar credenciais e segredos embutidos em textos e rejeitar preventivamente."""
        with self.assertRaises(ConfidentialDataDetectedError):
            scrub_outbound_payload({"prompt": "User query with Authorization: Bearer 1234567890abcdef1234567890"})

        with self.assertRaises(ConfidentialDataDetectedError):
            scrub_outbound_payload({"prompt": "Debug output sk-or-v1-abcdef0123456789abcdef0123456789"})

        with self.assertRaises(ConfidentialDataDetectedError):
            scrub_outbound_payload({"prompt": "Key sk-1234567890123456789012345"})

        with self.assertRaises(ConfidentialDataDetectedError):
            scrub_outbound_payload({"prompt": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA..."})

        clean_res = scrub_outbound_payload({"prompt": "Normal safe prompt without secrets", "max_tokens": 100})
        self.assertEqual(clean_res["prompt"], "Normal safe prompt without secrets")

    def test_08_retry_quota_consumption_and_persistent_limits(self) -> None:
        """Determinação 5: Cada tentativa de retry deve consumir uma reserva própria da quota."""
        limiter = SlidingWindowRateLimiter(max_per_minute=3, max_per_day=5)
        mock_sleeps = []

        runner = ModelBenchmarkRunner(
            registry=self.registry,
            cost_guard=self.cost_guard,
            ledger=self.ledger,
            rate_limiter=limiter,
            retry_policy=RetryPolicy(max_attempts=3, base_delay_sec=0.1),
            sleeper=lambda s: mock_sleeps.append(s),
        )

        call_count = 0
        def failing_handler(inputs: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return {"success": False, "status_code": 503}
            return {"success": True, "status_code": 200, "text": "Success on attempt 3"}

        custom_adapter = TextModelAdapter(self.cost_guard, mock_handler=failing_handler)
        runner.register_custom_adapter(ModelModality.TEXT, custom_adapter)

        res = runner.execute_task("meta-llama/llama-3.2-3b-instruct:free", ModelModality.TEXT, {"prompt": "Retry test"})
        self.assertTrue(res.success)
        self.assertEqual(call_count, 3)
        self.assertEqual(len(mock_sleeps), 2)

        self.assertEqual(limiter.requests_in_current_minute, 3)
        self.assertEqual(limiter.requests_in_current_day, 3)

        res_blocked = runner.execute_task("meta-llama/llama-3.2-3b-instruct:free", ModelModality.TEXT, {"prompt": "Throttled"})
        self.assertFalse(res_blocked.success)
        self.assertEqual(res_blocked.status_code, 429)

    def test_09_retry_after_header_handling_and_real_backoff(self) -> None:
        """Determinação 5 e 6: Respeitar cabeçalho Retry-After e executar atraso real de backoff."""
        limiter = SlidingWindowRateLimiter(max_per_minute=10, max_per_day=20)
        delays_executed = []

        runner = ModelBenchmarkRunner(
            registry=self.registry,
            cost_guard=self.cost_guard,
            ledger=self.ledger,
            rate_limiter=limiter,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_sec=1.0),
            sleeper=lambda d: delays_executed.append(d),
        )

        def retry_after_handler(inputs: dict) -> dict:
            return {"success": False, "status_code": 429, "retry_after_sec": 4.5}

        custom_adapter = TextModelAdapter(self.cost_guard, mock_handler=retry_after_handler)
        runner.register_custom_adapter(ModelModality.TEXT, custom_adapter)

        res = runner.execute_task("meta-llama/llama-3.2-3b-instruct:free", ModelModality.TEXT, {"prompt": "Throttled prompt"})
        self.assertFalse(res.success)
        self.assertEqual(len(delays_executed), 1)
        self.assertGreaterEqual(delays_executed[0], 4.5)

    def test_10_availability_ledger_synchronous_persistence(self) -> None:
        """Determinação 8: Persistir eventos e snapshots em armazenamento de auditoria preservando histórico."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            audit_path = os.path.join(tmp_dir, "audit_ledger.jsonl")
            ledger = AvailabilityLedger(storage_path=audit_path)

            ledger.record_event("meta-llama/llama-3.2-3b-instruct:free", AvailabilityEventType.MODEL_DISCOVERED)
            ledger.record_event("meta-llama/llama-3.2-3b-instruct:free", AvailabilityEventType.RATE_LIMITED_429, {"wait": 5.0})
            ledger.record_snapshot({"total_models": 5, "source": "test"})

            self.assertTrue(os.path.exists(audit_path))

            recovered_ledger = AvailabilityLedger(storage_path=audit_path)
            events = recovered_ledger.get_events_for_model("meta-llama/llama-3.2-3b-instruct:free")
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].event_type, AvailabilityEventType.MODEL_DISCOVERED)
            self.assertEqual(events[1].event_type, AvailabilityEventType.RATE_LIMITED_429)
            self.assertEqual(len(recovered_ledger._snapshots), 1)

    def test_11_metric_provenance_differentiation(self) -> None:
        """Determinação 9: Separar métricas MEASURED, ESTIMATED, SYNTHETIC e UNKNOWN. Nunca chamar mock de MEASURED."""
        runner = ModelBenchmarkRunner(
            registry=self.registry,
            cost_guard=self.cost_guard,
            ledger=self.ledger,
        )

        res = runner.execute_task("meta-llama/llama-3.2-3b-instruct:free", ModelModality.TEXT, {"prompt": "Metric test"})
        self.assertTrue(res.success)
        self.assertEqual(res.token_provenance, MetricProvenance.SYNTHETIC)
        self.assertEqual(res.latency_provenance, MetricProvenance.SYNTHETIC)
        self.assertIn("prompt_tokens", res.tokens)
        self.assertIn("completion_tokens", res.tokens)

    def test_12_http_transport_fail_closed_and_mocked_network_dispatch(self) -> None:
        """Determinação 10: Transporte HTTP real desativado por padrão (fail-closed) e testado com respostas mockadas."""
        transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, live_network_enabled=False)
        with self.assertRaises(LiveNetworkDisabledError) as ctx_disabled:
            transport.dispatch("text", {"model": "meta-llama/llama-3.2-3b-instruct:free", "messages": []})
        self.assertIn("Transporte de rede HTTP real desativado por padrão", str(ctx_disabled.exception))

        mock_http_response = MagicMock()
        mock_http_response.getcode.return_value = 200
        mock_http_response.headers = {"Content-Type": "application/json"}
        mock_http_response.read.return_value = json.dumps({
            "id": "gen-123",
            "choices": [{"message": {"content": "Live response from OpenRouter"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }).encode("utf-8")

        mock_context_manager = MagicMock()
        mock_context_manager.__enter__.return_value = mock_http_response

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "quotas.db")
            audit_path = os.path.join(tmp_dir, "audit.jsonl")
            limiter = SlidingWindowRateLimiter(max_per_minute=20, max_per_day=50, sqlite_path=db_path)
            ledger = AvailabilityLedger(storage_path=audit_path, require_durable=True)

            with patch("urllib.request.urlopen", return_value=mock_context_manager):
                auth_guard = self._create_authenticated_guard()
                live_transport = OpenRouterHttpTransport(
                    cost_guard=auth_guard,
                    rate_limiter=limiter,
                    ledger=ledger,
                    api_key="sk-or-v1-fake-test-key",
                    live_network_enabled=True,
                )
                adapter = TextModelAdapter(auth_guard, transport=live_transport)
                adapter_res = adapter.execute("meta-llama/llama-3.2-3b-instruct:free", {"prompt": "Live dispatch test"})

                self.assertTrue(adapter_res.success)
                self.assertEqual(adapter_res.tokens["total_tokens"], 20)
                self.assertEqual(adapter_res.token_provenance, MetricProvenance.MEASURED)
                self.assertEqual(adapter_res.output_data["text"], "Live response from OpenRouter")

    def test_13_centralized_egress_gate_blocks_direct_dispatch_paid_model(self) -> None:
        """Revisão 4 - P0 Financeiro: dispatch() direto é interceptado pelo FreeCostGuard e rejeita modelos pagos."""
        # 1. Gate 3 rejects paid model even before live network checks
        transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, api_key="sk-test", live_network_enabled=False)
        with self.assertRaises(UnauthorizedModelError) as ctx_paid:
            transport.dispatch("text", {"model": "openai/gpt-4o", "messages": []})
        self.assertIn("terminantemente proibidas", str(ctx_paid.exception))

        # 2. Attempt to dispatch without CostGuard fails closed
        unguarded_transport = OpenRouterHttpTransport(cost_guard=None, api_key="sk-test", live_network_enabled=False)
        with self.assertRaises(CostGuardError) as ctx_unguarded:
            unguarded_transport.dispatch("text", {"model": "meta-llama/llama-3.2-3b-instruct:free", "messages": []})
        self.assertIn("sem FreeCostGuard configurado", str(ctx_unguarded.exception))

    def test_14_catalog_ttl_freshness_and_stale_rejection(self) -> None:
        """Revisão 4 - P0 Catálogo: Catálogo expirado sem atualização remota deve falhar fechado com StaleCatalogError."""
        stale_registry = FreeModelRegistry(max_age_seconds=10.0)
        past_epoch = time.time() - 100.0  # 100s ago (exceeded 10s TTL)
        stale_registry.load_catalog_data(self.raw_catalog, epoch=past_epoch)
        self.assertTrue(stale_registry.is_stale())

        stale_guard = FreeCostGuard(stale_registry)
        with self.assertRaises(StaleCatalogError) as ctx_stale:
            stale_guard.authorize_model_execution("meta-llama/llama-3.2-3b-instruct:free")
        self.assertIn("Catálogo de modelos expirado", str(ctx_stale.exception))

        # With dynamic fetcher configured, ensure_fresh_catalog succeeds
        def mock_fetcher():
            return build_synthetic_openrouter_catalog()

        stale_registry.fetcher = mock_fetcher
        refreshed_entry = stale_guard.authorize_model_execution("meta-llama/llama-3.2-3b-instruct:free")
        self.assertIsNotNone(refreshed_entry)
        self.assertFalse(stale_registry.is_stale())

    def test_15_concurrent_multiprocess_atomic_sqlite_quota_competition(self) -> None:
        """Revisão 4 - P1 Quotas: Dois workers disputando a última cota concorrentemente no SQLite (threads)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "quotas.db")
            # Limit is strictly 1 per minute
            limiter = SlidingWindowRateLimiter(max_per_minute=1, max_per_day=10, sqlite_path=db_path)

            results = []
            def worker():
                thread_limiter = SlidingWindowRateLimiter(max_per_minute=1, max_per_day=10, sqlite_path=db_path)
                return thread_limiter.acquire()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futs = [executor.submit(worker) for _ in range(2)]
                results = [f.result() for f in futs]

            # Exactly one must succeed, and exactly one must fail
            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 1)

    def test_16_ledger_persistence_failure_fail_closed(self) -> None:
        """Revisão 4 - P1 Auditoria: Falha de escrita no armazenamento durável de auditoria lança AuditPersistenceError."""
        # Using an impossible path on Windows/Posix (e.g. empty string or forbidden directory)
        invalid_path = os.path.join(tempfile.gettempdir(), "non_existent_dir_12345", "sub", "audit.jsonl")
        ledger = AvailabilityLedger(storage_path="/invalid_root_never_created\0/audit.jsonl", require_durable=True)
        with self.assertRaises(AuditPersistenceError):
            ledger.record_event("test_model", AvailabilityEventType.MODEL_DISCOVERED)

    def test_17_response_without_usage_classified_unknown(self) -> None:
        """Revisão 4 - P2 Métricas: Respostas HTTP sem campo 'usage' são classificadas como UNKNOWN, nunca MEASURED."""
        mock_http_response = MagicMock()
        mock_http_response.getcode.return_value = 200
        mock_http_response.headers = {"Content-Type": "application/json"}
        # Provider returned choices, but omitted usage completely!
        mock_http_response.read.return_value = json.dumps({
            "id": "gen-no-usage",
            "choices": [{"message": {"content": "Response without usage"}}],
        }).encode("utf-8")

        mock_context_manager = MagicMock()
        mock_context_manager.__enter__.return_value = mock_http_response

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "quotas.db")
            audit_path = os.path.join(tmp_dir, "audit.jsonl")
            limiter = SlidingWindowRateLimiter(max_per_minute=20, max_per_day=50, sqlite_path=db_path)
            ledger = AvailabilityLedger(storage_path=audit_path, require_durable=True)

            with patch("urllib.request.urlopen", return_value=mock_context_manager):
                auth_guard = self._create_authenticated_guard()
                live_transport = OpenRouterHttpTransport(
                    cost_guard=auth_guard,
                    rate_limiter=limiter,
                    ledger=ledger,
                    api_key="sk-test",
                    live_network_enabled=True,
                )
                adapter = TextModelAdapter(auth_guard, transport=live_transport)
                res = adapter.execute("meta-llama/llama-3.2-3b-instruct:free", {"prompt": "Test no usage"})

                self.assertTrue(res.success)
                self.assertEqual(res.token_provenance, MetricProvenance.UNKNOWN, "Sem campo 'usage', métrica deve ser UNKNOWN")
                self.assertEqual(res.tokens["total_tokens"], 0)

    def test_18_unknown_modality_endpoint_rejected_fail_closed(self) -> None:
        """Revisão 4 - P1 Modalidades: Modalidade desconhecida deve ser rejeitada fail-closed sem fallback para /chat/completions."""
        transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, live_network_enabled=False)
        with self.assertRaises(IncompatibleModalityEndpointError) as ctx_unknown:
            transport.get_endpoint_for_modality("telepathy_v1")
        self.assertIn("Modalidade desconhecida ou sem endpoint correspondente", str(ctx_unknown.exception))

    def test_19_true_os_multiprocessing_atomic_sqlite_quota_competition(self) -> None:
        """Revisão 5 - Determinação 5: Teste adversarial com múltiplos processos reais do SO via multiprocessing."""
        import multiprocessing
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "mp_quotas.db")
            # Strict limit: 1 request per minute
            init_limiter = SlidingWindowRateLimiter(max_per_minute=1, max_per_day=10, sqlite_path=db_path)
            self.assertEqual(init_limiter.max_per_minute, 1)

            ctx = multiprocessing.get_context("spawn")
            out_queue = ctx.Queue()

            p1 = ctx.Process(target=_mp_quota_worker, args=(db_path, "default", 1, 10, out_queue))
            p2 = ctx.Process(target=_mp_quota_worker, args=(db_path, "default", 1, 10, out_queue))

            p1.start()
            p2.start()

            res1 = out_queue.get(timeout=10.0)
            res2 = out_queue.get(timeout=10.0)

            p1.join(timeout=5.0)
            p2.join(timeout=5.0)

            results = [res1, res2]
            self.assertIn(True, results)
            self.assertIn(False, results)
            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 1)

    def test_20_official_catalog_client_https_and_untrusted_source_rejection(self) -> None:
        """Revisão 5 e 6 - P0 Catálogo: Origem autenticada oficial via HTTPS e rejeição de falsificações."""
        # 1. Official client queries fixed HTTPS endpoint and returns AuthenticatedCatalogSnapshot
        mock_http_response = MagicMock()
        mock_http_response.getcode.return_value = 200
        mock_http_response.geturl.return_value = "https://openrouter.ai/api/v1/models"
        mock_http_response.headers = {"Content-Type": "application/json"}
        mock_http_response.read.return_value = json.dumps(build_synthetic_openrouter_catalog()).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_http_response

        with patch("urllib.request.urlopen", return_value=mock_ctx):
            client = OfficialOpenRouterCatalogClient()
            snapshot = client.fetch_catalog()
            self.assertIsInstance(snapshot, AuthenticatedCatalogSnapshot)
            self.assertEqual(snapshot.endpoint_url, "https://openrouter.ai/api/v1/models")
            self.assertTrue(snapshot.is_official_https_origin)
            self.assertIsNotNone(snapshot.origin_attestation)

        # 2. Tampering with endpoint URL or forging an unverified snapshot fails closed
        with self.assertRaises(UntrustedCatalogSourceError):
            AuthenticatedCatalogSnapshot.create(b"{}", endpoint_url="https://evil-unauthorized-server.com/models")

        # 3. Forged HMAC in snapshot is detected and rejected
        forged_snapshot = AuthenticatedCatalogSnapshot(
            raw_bytes=b"{}",
            endpoint_url="https://openrouter.ai/api/v1/models",
            fetched_at_epoch=time.time(),
            hmac_signature="forged_invalid_hmac_signature_hex",
        )
        with self.assertRaises(UntrustedCatalogSourceError):
            forged_snapshot.verify_authenticity()

        # 4. FreeModelRegistry with require_trusted_source=True strictly rejects raw dictionaries
        secure_registry = FreeModelRegistry(require_trusted_source=True)
        arbitrary_untrusted_data = build_synthetic_openrouter_catalog()
        with self.assertRaises(UntrustedCatalogSourceError) as ctx_untrusted:
            secure_registry.load_catalog_data(arbitrary_untrusted_data)
        self.assertIn("apenas snapshots originados diretamente do endpoint oficial", str(ctx_untrusted.exception))

        # 5. Verified AuthenticatedCatalogSnapshot is accepted and authenticates origin
        secure_registry.load_authenticated_snapshot(snapshot)
        self.assertEqual(secure_registry.total_models, 9)
        self.assertTrue(secure_registry.is_authenticated_origin)

    def test_21_live_network_configuration_strict_validation(self) -> None:
        """Revisão 5 e 6 - P0 Configuração: Exigir CostGuard, SQLiteQuotaStore, Ledger Durável e Origem Autenticada."""
        # Missing CostGuard
        with self.assertRaises(CostGuardError) as ctx_cg:
            OpenRouterHttpTransport(cost_guard=None, live_network_enabled=True)
        self.assertIn("Modo live exige FreeCostGuard", str(ctx_cg.exception))

        # Missing SQLite RateLimiter
        with self.assertRaises(CostGuardError) as ctx_limiter:
            OpenRouterHttpTransport(cost_guard=self.cost_guard, rate_limiter=None, live_network_enabled=True)
        self.assertIn("Modo live exige rate_limiter com SQLiteQuotaStore", str(ctx_limiter.exception))

        # RateLimiter without SQLite (in-memory)
        in_memory_limiter = SlidingWindowRateLimiter(sqlite_path=None)
        with self.assertRaises(CostGuardError) as ctx_mem:
            OpenRouterHttpTransport(cost_guard=self.cost_guard, rate_limiter=in_memory_limiter, live_network_enabled=True)
        self.assertIn("Modo live exige rate_limiter com SQLiteQuotaStore", str(ctx_mem.exception))

        # Missing or non-durable ledger
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "quotas.db")
            valid_limiter = SlidingWindowRateLimiter(sqlite_path=db_path)

            with self.assertRaises(AuditPersistenceError) as ctx_ledger:
                OpenRouterHttpTransport(
                    cost_guard=self.cost_guard,
                    rate_limiter=valid_limiter,
                    ledger=None,
                    live_network_enabled=True,
                )
            self.assertIn("Modo live exige AvailabilityLedger configurado com require_durable=True", str(ctx_ledger.exception))

            # Unauthenticated catalog origin in live mode
            valid_ledger = AvailabilityLedger(storage_path=os.path.join(tmp_dir, "audit.jsonl"), require_durable=True)
            self.assertFalse(self.cost_guard.registry.is_authenticated_origin)
            with self.assertRaises(UntrustedCatalogSourceError) as ctx_unauth:
                OpenRouterHttpTransport(
                    cost_guard=self.cost_guard,
                    rate_limiter=valid_limiter,
                    ledger=valid_ledger,
                    live_network_enabled=True,
                )
            self.assertIn("Modo live exige catálogo com origem oficial HTTPS comprovada", str(ctx_unauth.exception))

    def test_22_single_quota_reservation_in_runner_and_transport(self) -> None:
        """Revisão 5 - Determinação 4: Quota cobrada exatamente uma vez por tentativa no ponto de saída (sem dupla cobrança)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "quotas.db")
            audit_path = os.path.join(tmp_dir, "audit.jsonl")
            limiter = SlidingWindowRateLimiter(max_per_minute=10, max_per_day=20, sqlite_path=db_path)
            ledger = AvailabilityLedger(storage_path=audit_path, require_durable=True)
            auth_guard = self._create_authenticated_guard()

            mock_http_response = MagicMock()
            mock_http_response.getcode.return_value = 200
            mock_http_response.headers = {"Content-Type": "application/json"}
            mock_http_response.read.return_value = json.dumps({
                "id": "gen-single-quota",
                "choices": [{"message": {"content": "Hello single quota"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }).encode("utf-8")

            mock_ctx = MagicMock()
            mock_ctx.__enter__.return_value = mock_http_response

            with patch("urllib.request.urlopen", return_value=mock_ctx):
                live_transport = OpenRouterHttpTransport(
                    cost_guard=auth_guard,
                    rate_limiter=limiter,
                    ledger=ledger,
                    api_key="sk-or-v1-fake-key",
                    live_network_enabled=True,
                )
                runner = ModelBenchmarkRunner(
                    registry=auth_guard.registry,
                    cost_guard=auth_guard,
                    ledger=ledger,
                    rate_limiter=limiter,
                    transport=live_transport,
                )

                initial_reservations = limiter.requests_in_current_minute
                res = runner.execute_task("meta-llama/llama-3.2-3b-instruct:free", ModelModality.TEXT, {"prompt": "Single charge test"})

                self.assertTrue(res.success)
                # Exactly 1 quota reservation occurred, NOT 2!
                self.assertEqual(limiter.requests_in_current_minute - initial_reservations, 1)

    def test_23_binary_audio_generation_and_transcription_contracts(self) -> None:
        """Revisão 5 - Determinação 6: Contratos de áudio para geração binária e upload de transcrição."""
        raw_wav_bytes = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"

        # 1. Voice generation (binary response via mock_handler)
        def mock_tts_handler(inputs: dict) -> dict:
            return {
                "success": True,
                "status_code": 200,
                "audio_result": {
                    "binary_data": raw_wav_bytes,
                    "audio_bytes_length": len(raw_wav_bytes),
                    "format": "wav",
                },
            }

        tts_adapter = SpeechModelAdapter(self.cost_guard, mock_handler=mock_tts_handler)
        gen_res = tts_adapter.execute(
            "fishaudio/fish-speech-1.5:free",
            {"text_to_speak": "Hello world from synthetic speech", "format": "wav"},
        )
        self.assertTrue(gen_res.success)
        self.assertIn("binary_data", gen_res.output_data)
        self.assertEqual(gen_res.output_data["binary_data"], raw_wav_bytes)
        self.assertEqual(gen_res.output_data["audio_bytes_length"], len(raw_wav_bytes))

        # 2. Audio transcription (upload response via mock_handler)
        def mock_asr_handler(inputs: dict) -> dict:
            return {
                "success": True,
                "status_code": 200,
                "audio_result": {
                    "text": "Transcribed speech from audio upload",
                },
            }

        asr_adapter = SpeechModelAdapter(self.cost_guard, mock_handler=mock_asr_handler)
        trans_res = asr_adapter.execute(
            "openai/whisper-large-v3:free",
            {"audio_bytes": "UklGRi4AAABXQVZFZg==", "format": "wav"},
        )
        self.assertTrue(trans_res.success)
        self.assertEqual(trans_res.output_data.get("text"), "Transcribed speech from audio upload")

    def test_24_scientific_metrics_partial_usage_classified_unknown(self) -> None:
        """Revisão 5 - Determinação 7: Contagens parciais ou ausentes em usage são UNKNOWN, nunca MEASURED."""
        from ai_coordinator.openrouter.adapters import extract_usage_with_provenance

        # Full prompt and completion tokens -> MEASURED
        full_usage = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
        toks, prov = extract_usage_with_provenance(full_usage)
        self.assertEqual(prov, MetricProvenance.MEASURED)
        self.assertEqual(toks["total_tokens"], 30)

        # Missing completion_tokens -> UNKNOWN
        partial_usage_no_comp = {"usage": {"prompt_tokens": 10, "total_tokens": 10}}
        toks_no_comp, prov_no_comp = extract_usage_with_provenance(partial_usage_no_comp)
        self.assertEqual(prov_no_comp, MetricProvenance.UNKNOWN)

        # Missing prompt_tokens -> UNKNOWN
        partial_usage_no_prompt = {"usage": {"completion_tokens": 15, "total_tokens": 15}}
        toks_no_prompt, prov_no_prompt = extract_usage_with_provenance(partial_usage_no_prompt)
        self.assertEqual(prov_no_prompt, MetricProvenance.UNKNOWN)

        # Empty usage dict -> UNKNOWN
        toks_empty, prov_empty = extract_usage_with_provenance({"usage": {}})
        self.assertEqual(prov_empty, MetricProvenance.UNKNOWN)

    def test_25_adversarial_fabricated_catalog_with_zero_cost_rejected_in_live_mode(self) -> None:
        """Revisão 6 - P0 Catálogo: Catálogo fabricado com preço $0.00 é terminantemente rejeitado em modo live."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "quotas.db")
            audit_path = os.path.join(tmp_dir, "audit.jsonl")
            limiter = SlidingWindowRateLimiter(sqlite_path=db_path)
            ledger = AvailabilityLedger(storage_path=audit_path, require_durable=True)

            # Attacker fabricates an arbitrary catalog with 0 cost and attempts to inject into registry
            fabricated_dict = {
                "data": [
                    {
                        "id": "hacked/free-model:free",
                        "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
                        "architecture": {"modality": "text->text"},
                        "_trusted": True,  # Forged flag
                    }
                ]
            }
            fake_registry = FreeModelRegistry()
            fake_registry.load_catalog_data(fabricated_dict)
            self.assertFalse(fake_registry.is_authenticated_origin)

            fake_guard = FreeCostGuard(fake_registry)

            # Live transport must reject unauthenticated catalog origin immediately
            with self.assertRaises(UntrustedCatalogSourceError) as ctx_fake:
                OpenRouterHttpTransport(
                    cost_guard=fake_guard,
                    rate_limiter=limiter,
                    ledger=ledger,
                    api_key="sk-test",
                    live_network_enabled=True,
                )
            self.assertIn("Modo live exige catálogo com origem oficial HTTPS comprovada", str(ctx_fake.exception))

    def test_26_transport_direct_dispatch_rejects_api_key_field(self) -> None:
        """Revisão 6 - P0 Privacidade: Chamada direta ao dispatch() com campo 'api_key' é abortada no Gate 1."""
        transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, live_network_enabled=False)
        payload = {
            "model": "meta-llama/llama-3.2-3b-instruct:free",
            "messages": [{"role": "user", "content": "Hello"}],
            "api_key": "injected_secret_api_key_12345",
        }
        with self.assertRaises(ConfidentialDataDetectedError) as ctx:
            transport.dispatch("text", payload)
        self.assertIn("Detectado campo sensível proibido 'api_key'", str(ctx.exception))

    def test_27_transport_direct_dispatch_rejects_password_field(self) -> None:
        """Revisão 6 - P0 Privacidade: Chamada direta ao dispatch() com campo 'password' é abortada no Gate 1."""
        transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, live_network_enabled=False)
        payload = {
            "model": "meta-llama/llama-3.2-3b-instruct:free",
            "messages": [{"role": "user", "content": "Hello"}],
            "credentials": {"password": "my_admin_password"},
        }
        with self.assertRaises(ConfidentialDataDetectedError) as ctx:
            transport.dispatch("text", payload)
        self.assertIn("Detectado campo sensível proibido 'credentials'", str(ctx.exception))

    def test_28_transport_direct_dispatch_rejects_bearer_in_messages(self) -> None:
        """Revisão 6 - P0 Privacidade: Chamada direta com Bearer token ou chave embutida em mensagem é abortada no Gate 1."""
        transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, live_network_enabled=False)

        # 1. Bearer token in message content
        payload_bearer = {
            "model": "meta-llama/llama-3.2-3b-instruct:free",
            "messages": [{"role": "user", "content": "Please verify this token: Bearer abcdef1234567890"}],
        }
        with self.assertRaises(ConfidentialDataDetectedError):
            transport.dispatch("text", payload_bearer)

        # 2. OpenRouter API key pattern in message content
        payload_sk = {
            "model": "meta-llama/llama-3.2-3b-instruct:free",
            "messages": [{"role": "user", "content": "Key is sk-or-v1-abcdef0123456789abcdef0123"}],
        }
        with self.assertRaises(ConfidentialDataDetectedError):
            transport.dispatch("text", payload_sk)

    def test_29_transport_live_mode_rejects_non_text_modalities(self) -> None:
        """Revisão 6 - Escopo do Piloto: Chamada live em modalidade não-TEXT é bloqueada por restrição do piloto."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "quotas.db")
            audit_path = os.path.join(tmp_dir, "audit.jsonl")
            limiter = SlidingWindowRateLimiter(sqlite_path=db_path)
            ledger = AvailabilityLedger(storage_path=audit_path, require_durable=True)
            auth_guard = self._create_authenticated_guard()

            live_transport = OpenRouterHttpTransport(
                cost_guard=auth_guard,
                rate_limiter=limiter,
                ledger=ledger,
                api_key="sk-test",
                live_network_enabled=True,
            )

            # Attempting speech in live mode is blocked
            with self.assertRaises(IncompatibleModalityEndpointError) as ctx_speech:
                live_transport.dispatch("speech", {"model": "openai/whisper-large-v3:free", "audio_bytes": "abc"})
            self.assertIn("PILOT_SCOPE_RESTRICTION", str(ctx_speech.exception))

            # Attempting embedding in live mode is blocked
            with self.assertRaises(IncompatibleModalityEndpointError) as ctx_embed:
                live_transport.dispatch("embedding", {"model": "baai/bge-large-en-v1.5:free", "input": "test"})
            self.assertIn("PILOT_SCOPE_RESTRICTION", str(ctx_embed.exception))

    def test_30_ttft_metric_reported_unknown_without_streaming(self) -> None:
        """Revisão 6 - Métricas: TTFT reportado explicitamente como UNKNOWN e None quando sem streaming."""
        runner = ModelBenchmarkRunner(
            registry=self.registry,
            cost_guard=self.cost_guard,
            ledger=self.ledger,
        )

        res = runner.execute_task(
            "meta-llama/llama-3.2-3b-instruct:free",
            ModelModality.TEXT,
            {"prompt": "TTFT measurement test"},
        )
        self.assertTrue(res.success)
        self.assertIsNone(res.ttft_ms, "Em chamadas não-streaming, TTFT numérico deve ser None")
        self.assertEqual(res.ttft_provenance, MetricProvenance.UNKNOWN)

        res_dict = res.to_dict()
        self.assertIn("ttft_ms", res_dict)
        self.assertIsNone(res_dict["ttft_ms"])
        self.assertEqual(res_dict["ttft_provenance"], "UNKNOWN")

    def test_31_adversarial_locally_created_snapshot_fails_live_authorization(self) -> None:
        """Auditoria Revisão 6: Comprovar que AuthenticatedCatalogSnapshot.create() não autoriza chamadas reais."""
        synthetic_catalog = build_synthetic_openrouter_catalog()
        raw_bytes = json.dumps(synthetic_catalog).encode("utf-8")

        # 1. Snapshot created locally via create() has is_official_https_origin = False
        local_snapshot = AuthenticatedCatalogSnapshot.create(raw_bytes)
        self.assertFalse(local_snapshot.is_official_https_origin)
        self.assertIsNone(local_snapshot.origin_attestation)

        # 2. Registry with require_trusted_source=True rejects it fail-closed
        strict_registry = FreeModelRegistry(require_trusted_source=True)
        with self.assertRaises(UntrustedCatalogSourceError) as ctx:
            strict_registry.load_authenticated_snapshot(local_snapshot)
        self.assertIn("snapshot fornecido foi fabricado localmente", str(ctx.exception))

        # 3. Registry with require_trusted_source=False accepts for offline testing,
        # but keeps is_authenticated_origin = False
        offline_registry = FreeModelRegistry(require_trusted_source=False)
        offline_registry.load_authenticated_snapshot(local_snapshot)
        self.assertFalse(offline_registry.is_authenticated_origin)

        # 4. OpenRouterHttpTransport in live mode strictly rejects it fail-closed
        guard = FreeCostGuard(offline_registry)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf, \
             tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as lf:
            db_path = tf.name
            log_path = lf.name
        try:
            limiter = SlidingWindowRateLimiter(sqlite_path=db_path)
            ledger = AvailabilityLedger(storage_path=log_path, require_durable=True)
            with self.assertRaises(UntrustedCatalogSourceError) as ctx_transport:
                OpenRouterHttpTransport(
                    cost_guard=guard,
                    rate_limiter=limiter,
                    ledger=ledger,
                    live_network_enabled=True,
                    api_key="sk-or-v1-dummy-key-for-test",
                )
            self.assertIn("Modo live exige catálogo com origem oficial HTTPS comprovada", str(ctx_transport.exception))
        finally:
            for p in (db_path, log_path):
                if os.path.exists(p):
                    os.unlink(p)

    def test_32_catalog_client_https_redirect_and_destination_validation(self) -> None:
        """Auditoria Revisão 6: Validação de destino oficial, política de redirecionamentos e origem efetiva."""
        client = OfficialOpenRouterCatalogClient()

        # 1. Insecure HTTP redirect/response is rejected
        mock_resp_http = MagicMock()
        mock_resp_http.getcode.return_value = 200
        mock_resp_http.geturl.return_value = "http://openrouter.ai/api/v1/models"
        mock_resp_http.headers = {"Content-Type": "application/json"}
        mock_resp_http.read.return_value = b'{"data": []}'
        mock_ctx_http = MagicMock()
        mock_ctx_http.__enter__.return_value = mock_resp_http
        with patch("urllib.request.urlopen", return_value=mock_ctx_http):
            with self.assertRaises(UntrustedCatalogSourceError) as ctx:
                client.fetch_catalog()
            self.assertIn("esquema", str(ctx.exception).lower())

        # 2. Redirect to foreign host is rejected
        mock_resp_foreign = MagicMock()
        mock_resp_foreign.getcode.return_value = 200
        mock_resp_foreign.geturl.return_value = "https://evil-unauthorized-server.com/api/v1/models"
        mock_resp_foreign.headers = {"Content-Type": "application/json"}
        mock_resp_foreign.read.return_value = b'{"data": []}'
        mock_ctx_foreign = MagicMock()
        mock_ctx_foreign.__enter__.return_value = mock_resp_foreign
        with patch("urllib.request.urlopen", return_value=mock_ctx_foreign):
            with self.assertRaises(UntrustedCatalogSourceError) as ctx:
                client.fetch_catalog()
            self.assertIn("host", str(ctx.exception).lower())

        # 3. Wrong path is rejected
        mock_resp_path = MagicMock()
        mock_resp_path.getcode.return_value = 200
        mock_resp_path.geturl.return_value = "https://openrouter.ai/api/v1/other_endpoint"
        mock_resp_path.headers = {"Content-Type": "application/json"}
        mock_resp_path.read.return_value = b'{"data": []}'
        mock_ctx_path = MagicMock()
        mock_ctx_path.__enter__.return_value = mock_resp_path
        with patch("urllib.request.urlopen", return_value=mock_ctx_path):
            with self.assertRaises(UntrustedCatalogSourceError) as ctx:
                client.fetch_catalog()
            self.assertIn("caminho", str(ctx.exception).lower())

        # 4. Non-JSON Content-Type is rejected
        mock_resp_ct = MagicMock()
        mock_resp_ct.getcode.return_value = 200
        mock_resp_ct.geturl.return_value = "https://openrouter.ai/api/v1/models"
        mock_resp_ct.headers = {"Content-Type": "text/html"}
        mock_resp_ct.read.return_value = b'<html><body>Not JSON</body></html>'
        mock_ctx_ct = MagicMock()
        mock_ctx_ct.__enter__.return_value = mock_resp_ct
        with patch("urllib.request.urlopen", return_value=mock_ctx_ct):
            with self.assertRaises(UntrustedCatalogSourceError) as ctx:
                client.fetch_catalog()
            self.assertIn("content-type", str(ctx.exception).lower())

    def test_33_custom_headers_cannot_overwrite_reserved_headers(self) -> None:
        """Auditoria Revisão 6: Impedir que custom_headers sobrescreva Authorization ou cabeçalhos reservados."""
        auth_guard = self._create_authenticated_guard()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf, \
             tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as lf:
            db_path = tf.name
            log_path = lf.name
        try:
            limiter = SlidingWindowRateLimiter(sqlite_path=db_path)
            ledger = AvailabilityLedger(storage_path=log_path, require_durable=True)
            transport = OpenRouterHttpTransport(
                cost_guard=auth_guard,
                rate_limiter=limiter,
                ledger=ledger,
                live_network_enabled=True,
                api_key="sk-or-v1-legitimate-pilot-key",
            )
            payload = {
                "model": "meta-llama/llama-3.2-3b-instruct:free",
                "messages": [{"role": "user", "content": "hello"}],
            }
            # Attempt to overwrite Authorization
            with self.assertRaises(TransportError) as ctx_auth:
                transport.dispatch("text", payload, custom_headers={"Authorization": "Bearer evil-swapped-key"})
            self.assertIn("reservado", str(ctx_auth.exception).lower())
            self.assertIn("authorization", str(ctx_auth.exception).lower())

            # Attempt to overwrite authorization (case-insensitive)
            with self.assertRaises(TransportError) as ctx_auth_lower:
                transport.dispatch("text", payload, custom_headers={"authorization": "Bearer evil-swapped-key"})
            self.assertIn("reservado", str(ctx_auth_lower.exception).lower())

            # Attempt to overwrite Content-Type
            with self.assertRaises(TransportError) as ctx_ct:
                transport.dispatch("text", payload, custom_headers={"Content-Type": "text/plain"})
            self.assertIn("reservado", str(ctx_ct.exception).lower())

            # Attempt to overwrite Host
            with self.assertRaises(TransportError) as ctx_host:
                transport.dispatch("text", payload, custom_headers={"Host": "attacker.com"})
            self.assertIn("reservado", str(ctx_host.exception).lower())
        finally:
            for p in (db_path, log_path):
                if os.path.exists(p):
                    os.unlink(p)

    def test_34_forged_origin_attestation_seal_strictly_rejected(self) -> None:
        """Auditoria Revisão 6: Atestação forjada com is_official_https_origin=True é rejeitada fail-closed."""
        import hashlib
        import hmac

        synthetic_catalog = build_synthetic_openrouter_catalog()
        raw_bytes = json.dumps(synthetic_catalog).encode("utf-8")

        # Tampered attestation signature
        tampered_snapshot = AuthenticatedCatalogSnapshot(
            raw_bytes=raw_bytes,
            endpoint_url="https://openrouter.ai/api/v1/models",
            fetched_at_epoch=time.time(),
            hmac_signature=hmac.new(_PROCESS_CATALOG_SECRET, raw_bytes, hashlib.sha256).hexdigest(),
            is_official_https_origin=True,
            origin_attestation="forged_attestation_signature_hex",
        )
        with self.assertRaises(UntrustedCatalogSourceError) as ctx:
            tampered_snapshot.verify_authenticity()
        self.assertIn("inválida ou forjada", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
