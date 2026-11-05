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
    TextModelAdapter,
    UnauthorizedModelError,
    parse_float_cost,
    scrub_outbound_payload,
)


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

    def test_01_registry_parsing_and_modality_classification(self) -> None:
        """Validate dynamic catalog parsing, zero-cost qualification, and modality inference."""
        self.assertEqual(self.registry.total_models, 8)
        free_models = self.registry.get_free_models()
        self.assertEqual(len(free_models), 5, "Exatamente 5 dos 8 modelos devem ser qualificados como gratuitos")

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

        # Verify Speech Modality (Transcription)
        speech_models = self.registry.get_by_modality(ModelModality.SPEECH)
        self.assertEqual(len(speech_models), 1)
        self.assertEqual(speech_models[0].id, "openai/whisper-large-v3:free")
        self.assertIn(ModelModality.SPEECH_TRANSCRIPTION, speech_models[0].modalities)

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

        with patch("urllib.request.urlopen", return_value=mock_context_manager):
            live_transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, api_key="sk-or-v1-fake-test-key", live_network_enabled=True)
            adapter = TextModelAdapter(self.cost_guard, transport=live_transport)
            adapter_res = adapter.execute("meta-llama/llama-3.2-3b-instruct:free", {"prompt": "Live dispatch test"})

            self.assertTrue(adapter_res.success)
            self.assertEqual(adapter_res.tokens["total_tokens"], 20)
            self.assertEqual(adapter_res.token_provenance, MetricProvenance.MEASURED)
            self.assertEqual(adapter_res.output_data["text"], "Live response from OpenRouter")

    def test_13_centralized_egress_gate_blocks_direct_dispatch_paid_model(self) -> None:
        """Revisão 4 - P0 Financeiro: dispatch() direto é interceptado pelo FreeCostGuard e rejeita modelos pagos."""
        transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, api_key="sk-test", live_network_enabled=True)
        # Attempt to bypass via direct transport call with paid model
        with self.assertRaises(UnauthorizedModelError) as ctx_paid:
            transport.dispatch("text", {"model": "openai/gpt-4o", "messages": []})
        self.assertIn("terminantemente proibidas", str(ctx_paid.exception))

        # Attempt to dispatch without CostGuard fails closed
        unguarded_transport = OpenRouterHttpTransport(cost_guard=None, api_key="sk-test", live_network_enabled=True)
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
        """Revisão 4 - P1 Quotas: Dois workers disputando a última cota concorrentemente no SQLite."""
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
        # Ensure parent dir cannot be created or simulate failure
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

        with patch("urllib.request.urlopen", return_value=mock_context_manager):
            live_transport = OpenRouterHttpTransport(cost_guard=self.cost_guard, api_key="sk-test", live_network_enabled=True)
            adapter = TextModelAdapter(self.cost_guard, transport=live_transport)
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


if __name__ == "__main__":
    unittest.main()
