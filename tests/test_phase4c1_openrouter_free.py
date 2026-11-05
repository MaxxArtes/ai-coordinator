"""Unit and adversarial test suite for OpenRouter Free Model Integration (Phase 4C.1)."""
from __future__ import annotations

import json
import time
import unittest

from ai_coordinator.openrouter import (
    ModelModality,
    ModelPricing,
    ModelCatalogEntry,
    FreeModelRegistry,
    FreeCostGuard,
    CostGuardError,
    UnauthorizedModelError,
    PaidFallbackBlockedError,
    AvailabilityEventType,
    AvailabilityLedger,
    SlidingWindowRateLimiter,
    CircuitState,
    CircuitBreaker,
    CircuitOpenError,
    RateLimitExceededError,
    RetryPolicy,
    BaseModelAdapter,
    TextModelAdapter,
    EmbeddingModelAdapter,
    RerankModelAdapter,
    DecisionModelAdapter,
    SpeechModelAdapter,
    AdapterResponse,
    ModelBenchmarkRunner,
    ModelBenchmarkRunResult,
)
from ai_coordinator.openrouter.adapters import scrub_outbound_payload


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
            # Paid models (Must be blocked strictly)
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
        self.registry = FreeModelRegistry()
        self.registry.load_catalog_data(self.raw_catalog, timestamp="2026-11-05T10:00:00Z")
        self.cost_guard = FreeCostGuard(self.registry)
        self.ledger = AvailabilityLedger()

    def test_01_registry_parsing_and_modality_classification(self) -> None:
        """Validate dynamic catalog parsing, zero-cost qualification, and modality inference."""
        self.assertEqual(self.registry.total_models, 7)
        free_models = self.registry.get_free_models()
        self.assertEqual(len(free_models), 5, "5 dos 7 modelos no catálogo devem ser qualificados como gratuitos")

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

        # Verify Speech Modality
        speech_models = self.registry.get_by_modality(ModelModality.SPEECH)
        self.assertEqual(len(speech_models), 1)
        self.assertEqual(speech_models[0].id, "openai/whisper-large-v3:free")

        # Verify Decision Modality
        decision_models = self.registry.get_by_modality(ModelModality.DECISIONS)
        dec_ids = [m.id for m in decision_models]
        self.assertIn("google/gemini-2.0-flash-exp:free", dec_ids)
        self.assertIn("meta-llama/llama-3.2-3b-instruct:free", dec_ids)

    def test_02_cost_guard_fail_closed_adversarial_rejections(self) -> None:
        """Validate strict fail-closed rejection of paid models, fallbacks, and uncataloged endpoints."""
        # 1. Free models pass authorization
        entry = self.cost_guard.authorize_model_execution("meta-llama/llama-3.2-3b-instruct:free")
        self.assertEqual(entry.pricing.prompt, 0.0)
        self.assertEqual(entry.pricing.completion, 0.0)
        self.assertTrue(entry.is_free)

        # 2. Paid models are immediately rejected (fail-closed)
        with self.assertRaises(UnauthorizedModelError) as ctx_paid:
            self.cost_guard.authorize_model_execution("openai/gpt-4o")
        self.assertIn("faturáveis são terminantemente proibidas", str(ctx_paid.exception))

        with self.assertRaises(UnauthorizedModelError):
            self.cost_guard.authorize_model_execution("anthropic/claude-3.5-sonnet")

        # 3. Uncataloged / missing models fail closed
        with self.assertRaises(UnauthorizedModelError) as ctx_missing:
            self.cost_guard.authorize_model_execution("unknown-org/stealth-model")
        self.assertIn("não foi encontrado no catálogo", str(ctx_missing.exception))

        # 4. Prohibit paid fallback chains
        with self.assertRaises(PaidFallbackBlockedError) as ctx_fallback:
            self.cost_guard.sanitize_fallback_targets([
                "meta-llama/llama-3.2-3b-instruct:free",
                "openai/gpt-4o",  # Paid!
            ])
        self.assertIn("Tentativa de fallback bloqueada", str(ctx_fallback.exception))

        # 5. Payload inspection blocks paid models in request dict
        with self.assertRaises(UnauthorizedModelError):
            self.cost_guard.inspect_request_payload({"model": "openai/gpt-4o", "prompt": "hello"})

        self.assertGreater(self.cost_guard.blocked_attempts, 0)

    def test_03_sliding_window_rate_limiter(self) -> None:
        """Validate sliding window token bucket rate limiting with free quota constraints."""
        limiter = SlidingWindowRateLimiter(max_per_minute=3, max_per_day=5)
        now = 1000.0

        # Acquire 3 requests within the minute
        self.assertTrue(limiter.acquire(now=now))
        self.assertTrue(limiter.acquire(now=now + 1.0))
        self.assertTrue(limiter.acquire(now=now + 2.0))

        # 4th request in the same minute is blocked
        self.assertFalse(limiter.can_acquire(now=now + 3.0))
        self.assertFalse(limiter.acquire(now=now + 3.0))
        self.assertGreater(limiter.time_until_available(now=now + 3.0), 0.0)

        # After 60 seconds, minute window resets
        now_next_min = now + 65.0
        self.assertTrue(limiter.can_acquire(now=now_next_min))
        self.assertTrue(limiter.acquire(now=now_next_min))
        self.assertTrue(limiter.acquire(now=now_next_min + 1.0))

        # Day limit (5 total) reached
        self.assertFalse(limiter.acquire(now=now_next_min + 2.0))

    def test_04_circuit_breaker_state_transitions(self) -> None:
        """Validate Circuit Breaker transitions: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_sec=10.0, half_open_success_threshold=2)
        t = 100.0

        # Normal operation: CLOSED
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow_request(now=t))

        # First failure
        cb.record_failure(now=t)
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertTrue(cb.allow_request(now=t))

        # Second failure trips to OPEN
        cb.record_failure(now=t)
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertFalse(cb.allow_request(now=t + 1.0), "Em estado OPEN deve rejeitar chamadas")

        # Before recovery timeout, remains OPEN
        self.assertFalse(cb.allow_request(now=t + 5.0))

        # After recovery timeout, transitions to HALF_OPEN
        self.assertTrue(cb.allow_request(now=t + 11.0))
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # First success in HALF_OPEN
        cb.record_success(now=t + 11.0)
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

        # Second success in HALF_OPEN restores CLOSED
        cb.record_success(now=t + 12.0)
        self.assertEqual(cb.state, CircuitState.CLOSED)

        # Trip again to verify single failure in HALF_OPEN re-trips
        cb.record_failure(now=t + 20.0)
        cb.record_failure(now=t + 20.0)
        self.assertEqual(cb.state, CircuitState.OPEN)
        # Advance to HALF_OPEN
        cb.allow_request(now=t + 35.0)
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)
        # Single failure re-trips immediately
        cb.record_failure(now=t + 36.0)
        self.assertEqual(cb.state, CircuitState.OPEN)

    def test_05_payload_sanitization_privacy_scrubbing(self) -> None:
        """Validate preventative scrubbing of API keys, passwords, and tokens in outbound payloads."""
        payload = {
            "prompt": "Classify this dataset",
            "api_key": "sk-or-v1-super-secret-token",
            "metadata": {
                "user_token": "ghp_secret_credential_12345",
                "auth_password": "super_secret_password",
                "normal_field": "public_data",
            },
            "parameters": [
                {"name": "temp", "val": 0.7},
                {"secret_key": "hidden_secret"},
            ]
        }

        scrubbed = scrub_outbound_payload(payload)
        self.assertEqual(scrubbed["api_key"], "[REDACTED_BY_COST_GUARD]")
        self.assertEqual(scrubbed["metadata"]["user_token"], "[REDACTED_BY_COST_GUARD]")
        self.assertEqual(scrubbed["metadata"]["auth_password"], "[REDACTED_BY_COST_GUARD]")
        self.assertEqual(scrubbed["metadata"]["normal_field"], "public_data")
        self.assertEqual(scrubbed["parameters"][1]["secret_key"], "[REDACTED_BY_COST_GUARD]")

    def test_06_modality_adapters_execution(self) -> None:
        """Validate modular adapters across Text, Embedding, Rerank, Decision, and Speech."""
        # 1. Text Adapter
        text_adapter = TextModelAdapter(self.cost_guard)
        text_res = text_adapter.execute("meta-llama/llama-3.2-3b-instruct:free", {"prompt": "Hello"})
        self.assertTrue(text_res.success)
        self.assertEqual(text_res.cost_usd, 0.0)
        self.assertGreater(text_res.latency_ms, 0.0)

        # 2. Embedding Adapter
        embed_adapter = EmbeddingModelAdapter(self.cost_guard)
        embed_res = embed_adapter.execute("baai/bge-large-en-v1.5:free", {"input": "Semantic vector test", "dimensions": 128})
        self.assertTrue(embed_res.success)
        self.assertEqual(len(embed_res.output_data["embedding"]), 128)
        self.assertEqual(embed_res.cost_usd, 0.0)

        # 3. Rerank Adapter
        rerank_adapter = RerankModelAdapter(self.cost_guard)
        rerank_res = rerank_adapter.execute("nvidia/nemotron-rerank-8b:free", {
            "query": "information retrieval",
            "documents": ["doc1 relevant", "doc2 irrelevant", "doc3 marginal"],
        })
        self.assertTrue(rerank_res.success)
        self.assertEqual(len(rerank_res.output_data["results"]), 3)
        self.assertEqual(rerank_res.cost_usd, 0.0)

        # 4. Decision Adapter
        decision_adapter = DecisionModelAdapter(self.cost_guard)
        dec_res = decision_adapter.execute("google/gemini-2.0-flash-exp:free", {
            "prompt": "Evaluate budget risk",
            "schema": {"type": "object", "properties": {"decision": {"type": "string"}}},
        })
        self.assertTrue(dec_res.success)
        self.assertIn("decision", dec_res.output_data["decision"])
        self.assertEqual(dec_res.cost_usd, 0.0)

        # 5. Speech Adapter
        speech_adapter = SpeechModelAdapter(self.cost_guard)
        speech_res = speech_adapter.execute("openai/whisper-large-v3:free", {"text_to_speak": "Synthesized voice"})
        self.assertTrue(speech_res.success)
        self.assertEqual(speech_res.cost_usd, 0.0)

    def test_07_model_benchmark_runner_full_resilience_integration(self) -> None:
        """Validate ModelBenchmarkRunner gating: CostGuard -> CircuitBreaker -> RateLimiter -> Adapter."""
        rate_limiter = SlidingWindowRateLimiter(max_per_minute=2, max_per_day=10)
        circuit_breaker = CircuitBreaker(failure_threshold=2)
        runner = ModelBenchmarkRunner(
            registry=self.registry,
            cost_guard=self.cost_guard,
            ledger=self.ledger,
            rate_limiter=rate_limiter,
            circuit_breaker=circuit_breaker,
        )

        # Successful executions under quota
        res1 = runner.execute_task("meta-llama/llama-3.2-3b-instruct:free", ModelModality.TEXT, {"prompt": "Task 1"})
        self.assertTrue(res1.success)
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.cost_usd, 0.0)

        res2 = runner.execute_task("google/gemini-2.0-flash-exp:free", ModelModality.TEXT, {"prompt": "Task 2"})
        self.assertTrue(res2.success)

        # Third task in the same minute throttled by RateLimiter (status 429)
        res3 = runner.execute_task("meta-llama/llama-3.2-3b-instruct:free", ModelModality.TEXT, {"prompt": "Task 3"})
        self.assertFalse(res3.success)
        self.assertEqual(res3.status_code, 429)
        self.assertIn("RATE_LIMIT_EXCEEDED", res3.error_message)

        # Paid model blocked by CostGuard (status 403)
        res_paid = runner.execute_task("openai/gpt-4o", ModelModality.TEXT, {"prompt": "Paid task"})
        self.assertFalse(res_paid.success)
        self.assertEqual(res_paid.status_code, 403)
        self.assertIn("COST_GUARD_BLOCKED", res_paid.error_message)

        # Audit ledger recorded the events
        audit = self.ledger.export_audit_trail()
        self.assertGreater(audit["total_events"], 0)


if __name__ == "__main__":
    unittest.main()
