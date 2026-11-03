"""Comprehensive adversarial test suite for Phase 4C.0.1: Cognitive Telemetry, Provenance, and Storage."""

from __future__ import annotations

import concurrent.futures
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest

from ai_coordinator import (
    Store,
    MetricProvenance,
    TokenMetrics,
    LatencyMetrics,
    CostMetrics,
    CognitiveTelemetryRecord,
    TelemetryCollector,
    PricingRegistry,
    TelemetryAggregate,
    compute_telemetry_aggregate,
    safe_record_telemetry,
    normalize_provider_tokens,
)
from ai_coordinator.telemetry import (
    scrub_sensitive_text,
    scrub_sensitive_dict,
    scrub_sensitive_value,
)


class TestPhase4CCognitiveTelemetry(unittest.TestCase):
    """Test suite validating cognitive telemetry, provenance tracking, and storage integration."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="test_phase4c_telemetry_")
        self.store = Store(self.test_dir)

    def tearDown(self) -> None:
        if hasattr(self.store, "db"):
            self.store.db.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_provenance_validation(self) -> None:
        """Validate strict data provenance categories and rejection of arbitrary values."""
        self.assertEqual(MetricProvenance.validate("measured"), "measured")
        self.assertEqual(MetricProvenance.validate("estimated"), "estimated")
        self.assertEqual(MetricProvenance.validate("unknown"), "unknown")

        with self.assertRaises(ValueError) as ctx:
            MetricProvenance.validate("guessed")
        self.assertIn("Proveniência inválida", str(ctx.exception))

        with self.assertRaises(ValueError):
            MetricProvenance.validate("simulated")

        with self.assertRaises(ValueError):
            MetricProvenance.validate(123)  # type: ignore

    def test_02_token_metrics_accounting_and_null_distinction(self) -> None:
        """Validate token metrics calculations, auto-summing, and distinction between 0 and None."""
        # 1. Measured zero tokens is explicitly 0
        measured_zero = TokenMetrics(input_tokens=0, output_tokens=0, provenance=MetricProvenance.MEASURED)
        self.assertEqual(measured_zero.input_tokens, 0)
        self.assertEqual(measured_zero.output_tokens, 0)
        self.assertEqual(measured_zero.total_tokens, 0)

        # 2. Unknown tokens must stay None (not converted to 0)
        unknown_tokens = TokenMetrics(provenance=MetricProvenance.UNKNOWN)
        self.assertIsNone(unknown_tokens.input_tokens)
        self.assertIsNone(unknown_tokens.output_tokens)
        self.assertIsNone(unknown_tokens.total_tokens)

        # 3. Known values auto-sum correctly
        tokens = TokenMetrics(
            input_tokens=150,
            output_tokens=75,
            cache_read_tokens=50,
            cache_creation_tokens=25,
            provenance=MetricProvenance.MEASURED,
        )
        self.assertEqual(tokens.total_tokens, 300)
        self.assertEqual(tokens.provenance, "measured")

        # 4. Dict round-trip
        data = tokens.to_dict()
        restored = TokenMetrics.from_dict(data)
        self.assertEqual(restored, tokens)

    def test_03_provider_cache_normalization_contracts(self) -> None:
        """Validate vendor-specific cache contracts preventing double-counting."""
        # Anthropic: input_tokens is disjoint from cache tokens
        anthropic_tok = normalize_provider_tokens(
            provider="anthropic",
            input_tokens=1000,
            output_tokens=250,
            cache_read_tokens=500,
            cache_creation_tokens=100,
        )
        self.assertEqual(anthropic_tok.total_tokens, 1850)

        # OpenAI: prompt_tokens already includes cached_tokens (do NOT add cache_read again)
        openai_tok = normalize_provider_tokens(
            provider="openai",
            input_tokens=1000,
            output_tokens=250,
            cache_read_tokens=500,
        )
        self.assertEqual(openai_tok.total_tokens, 1250)

        # Google Gemini: promptTokenCount includes cachedContentTokenCount
        gemini_tok = normalize_provider_tokens(
            provider="gemini",
            input_tokens=800,
            output_tokens=150,
            cache_read_tokens=300,
        )
        self.assertEqual(gemini_tok.total_tokens, 950)

    def test_04_latency_metrics_decomposition_and_provenance(self) -> None:
        """Validate latency decomposition: TTFT, generation, tool duration, supervisor overhead."""
        latencies = LatencyMetrics(
            total_duration_ms=630.0,
            ttft_ms=120.5,
            generation_duration_ms=450.0,
            tool_duration_ms=150.0,
            supervisor_overhead_ms=30.0,
            latency_provenance=MetricProvenance.MEASURED,
            generation_duration_provenance=MetricProvenance.ESTIMATED,
        )
        self.assertEqual(latencies.total_duration_ms, 630.0)
        self.assertEqual(latencies.ttft_ms, 120.5)
        self.assertEqual(latencies.generation_duration_ms, 450.0)
        self.assertEqual(latencies.tool_duration_ms, 150.0)
        self.assertEqual(latencies.supervisor_overhead_ms, 30.0)
        self.assertEqual(latencies.latency_provenance, MetricProvenance.MEASURED)
        self.assertEqual(latencies.generation_duration_provenance, MetricProvenance.ESTIMATED)

        # Dict round-trip
        data = latencies.to_dict()
        restored = LatencyMetrics.from_dict(data)
        self.assertEqual(restored, latencies)

    def test_05_pricing_registry_versioning_and_cost_estimation(self) -> None:
        """Validate pricing registry calculation, vendor cache deductions, version stamping, and unknown cost handling."""
        tokens = TokenMetrics(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_tokens=500_000,
            cache_creation_tokens=200_000,
            provenance=MetricProvenance.MEASURED,
        )

        # Known Anthropic model: claude-3-5-sonnet (input_tokens is disjoint from cache)
        cost = PricingRegistry.estimate_cost("claude-3-5-sonnet", tokens, provider="anthropic")
        self.assertEqual(cost.provenance, MetricProvenance.ESTIMATED)
        self.assertEqual(cost.pricing_version, PricingRegistry.REGISTRY_VERSION)
        self.assertAlmostEqual(cost.input_cost_usd or 0.0, 3.00, places=4)
        self.assertAlmostEqual(cost.output_cost_usd or 0.0, 1.50, places=4)
        self.assertAlmostEqual(cost.cache_read_cost_usd or 0.0, 0.15, places=4)
        self.assertAlmostEqual(cost.cache_creation_cost_usd or 0.0, 0.75, places=4)
        self.assertAlmostEqual(cost.total_cost_usd or 0.0, 5.40, places=4)

        # Exact Auditor Scenario for OpenAI gpt-4o:
        # 1,000 input prompt tokens (including 500 cached tokens) and 250 output tokens
        # Uncached input = 1000 - 500 = 500 tokens ($0.00125)
        # Cache read = 500 tokens ($0.000625)
        # Output = 250 tokens ($0.002500)
        # Expected total = US$ 0.004375 (NOT 0.005625)
        openai_tokens = TokenMetrics(
            input_tokens=1_000,
            output_tokens=250,
            cache_read_tokens=500,
            provenance=MetricProvenance.MEASURED,
        )
        openai_cost = PricingRegistry.estimate_cost("gpt-4o", openai_tokens, provider="openai")
        self.assertEqual(openai_cost.provenance, MetricProvenance.ESTIMATED)
        self.assertEqual(openai_cost.pricing_version, PricingRegistry.REGISTRY_VERSION)
        self.assertAlmostEqual(openai_cost.input_cost_usd or 0.0, 0.00125, places=6)
        self.assertAlmostEqual(openai_cost.cache_read_cost_usd or 0.0, 0.000625, places=6)
        self.assertAlmostEqual(openai_cost.output_cost_usd or 0.0, 0.0025, places=6)
        self.assertEqual(openai_cost.total_cost_usd, 0.004375)

        # Gemini Scenario with cache deduction: gemini-1.5-pro
        gemini_tokens = TokenMetrics(
            input_tokens=1_000,
            output_tokens=100,
            cache_read_tokens=400,
            provenance=MetricProvenance.MEASURED,
        )
        gemini_cost = PricingRegistry.estimate_cost("gemini-1.5-pro", gemini_tokens, provider="google")
        self.assertEqual(gemini_cost.provenance, MetricProvenance.ESTIMATED)
        # uncached = 600 * 3.50 / 1e6 = 0.0021, read = 400 * 0.875 / 1e6 = 0.00035, out = 100 * 10.50 / 1e6 = 0.00105 -> 0.0035
        self.assertAlmostEqual(gemini_cost.total_cost_usd or 0.0, 0.0035, places=6)

        # Missing essential token metrics must return UNKNOWN with total_cost_usd=None
        missing_in_cost = PricingRegistry.estimate_cost("gpt-4o", TokenMetrics(input_tokens=None, output_tokens=100))
        self.assertEqual(missing_in_cost.provenance, MetricProvenance.UNKNOWN)
        self.assertIsNone(missing_in_cost.total_cost_usd)

        missing_out_cost = PricingRegistry.estimate_cost("gpt-4o", TokenMetrics(input_tokens=100, output_tokens=None))
        self.assertEqual(missing_out_cost.provenance, MetricProvenance.UNKNOWN)
        self.assertIsNone(missing_out_cost.total_cost_usd)

        # Unrecognized model: must return UNKNOWN and total_cost_usd=None (NEVER 0.0)
        unrecognized_cost = PricingRegistry.estimate_cost("unknown-local-llama-999b", tokens)
        self.assertEqual(unrecognized_cost.provenance, MetricProvenance.UNKNOWN)
        self.assertIsNone(unrecognized_cost.total_cost_usd)
        self.assertIsNone(unrecognized_cost.pricing_version)

    def test_06_adversarial_secret_scrubbing(self) -> None:
        """Validate complete redaction of all secrets across keys, values, and deep nested containers."""
        # Specific test cases from auditor's review
        auditor_cases = [
            ("failure sk-proj-1234567890abcdef12345678", "failure [REDACTED_SECRET]"),
            ("failure ghp_111122223333444455556666777788889999", "failure [REDACTED_SECRET]"),
            ("api_key=sk-proj-1234567890abcdef12345678", "api_key=[REDACTED_SECRET]"),
        ]
        for raw, expected in auditor_cases:
            scrubbed = scrub_sensitive_text(raw)
            self.assertEqual(scrubbed, expected)
            self.assertNotIn("sk-proj-1234567890abcdef12345678", scrubbed or "")
            self.assertNotIn("ghp_111122223333444455556666777788889999", scrubbed or "")

        # Standalone cloud keys
        cloud_cases = [
            ("Google API key AIzaSyD-1234567890abcdef1234567890abcde in query", "Google API key [REDACTED_SECRET] in query"),
            ("AWS key AKIAIOSFODNN7EXAMPLE found", "AWS key [REDACTED_SECRET] found"),
            ("Slack token xoxb-1234567890-abcdef123456 logged", "Slack token [REDACTED_SECRET] logged"),
            ("Auth Bearer secret-token-1234567890", "Auth Bearer [REDACTED_SECRET]"),
        ]
        for raw, expected in cloud_cases:
            scrubbed = scrub_sensitive_text(raw)
            self.assertEqual(scrubbed, expected)

        # Complex nested dictionaries and lists
        dirty_meta = {
            "model": "gpt-4o",
            "api_key": "my-secret-key-12345",
            "bearer_token": "bearer abcdef1234567890",
            "secret_key_field": ["some_value"],
            "normal_items": [
                "sk-proj-1234567890abcdef12345678",
                "ghp_111122223333444455556666777788889999",
                {"nested_credential": "password123"},
            ],
            "normal_tuple": ("AKIAIOSFODNN7EXAMPLE", "normal_data"),
        }
        clean_meta = scrub_sensitive_dict(dirty_meta)
        self.assertEqual(clean_meta["api_key"], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["bearer_token"], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["secret_key_field"], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["normal_items"][0], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["normal_items"][1], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["normal_items"][2]["nested_credential"], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["normal_tuple"][0], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["normal_tuple"][1], "normal_data")

        # Deeply nested lists (Audit Finding P1: list inside list)
        deep_nested = {
            "context": [
                ["sk-proj-1234567890abcdef12345678"],
                {"nested_matrix": [[["ghp_111122223333444455556666777788889999"]]]},
            ],
            "tuples_in_set": {("bearer 1234567890abcdef",)},
        }
        clean_deep = scrub_sensitive_dict(deep_nested)
        self.assertEqual(clean_deep["context"][0][0], "[REDACTED_SECRET]")
        self.assertEqual(clean_deep["context"][1]["nested_matrix"][0][0][0], "[REDACTED_SECRET]")
        tuple_elem = next(iter(clean_deep["tuples_in_set"]))
        self.assertEqual(tuple_elem[0], "bearer [REDACTED_SECRET]")

        # Test scrub_sensitive_value directly on arbitrary types
        self.assertIsNone(scrub_sensitive_value(None))
        self.assertEqual(scrub_sensitive_value(42), 42)
        self.assertEqual(scrub_sensitive_value([["AIzaSyD-1234567890abcdef1234567890abcde"]]), [["[REDACTED_SECRET]"]])

    def test_07_telemetry_collector_end_to_end(self) -> None:
        """Validate TelemetryCollector context manager, fine-grained timers, and finalization."""
        collector = TelemetryCollector(
            task_id="task_coll_01",
            run_id="run_coll_01",
            worker_identity="worker-agent-1",
            worker_pid=1234,
            plan_revision=2,
            provider="anthropic",
            model_name="claude-3-5-sonnet",
            metadata={"phase": "baseline_test"},
        )

        with collector:
            time.sleep(0.01)
            collector.mark_first_token()

            for _ in range(5):
                collector.record_stream_chunk()

            with collector.measure_tool():
                time.sleep(0.015)

            with collector.measure_supervisor():
                time.sleep(0.005)

            collector.record_retry()
            collector.set_tokens(
                input_tokens=1000,
                output_tokens=250,
                cache_read_tokens=400,
                provenance=MetricProvenance.MEASURED,
            )

        record = collector.finalize(success=True)

        self.assertEqual(record.task_id, "task_coll_01")
        self.assertEqual(record.provider, "anthropic")
        self.assertEqual(record.model_name, "claude-3-5-sonnet")
        self.assertEqual(record.tokens.input_tokens, 1000)
        self.assertEqual(record.tokens.output_tokens, 250)
        self.assertEqual(record.tokens.cache_read_tokens, 400)
        self.assertEqual(record.tokens.total_tokens, 1650)

        # Decomposed latency verification
        self.assertIsNotNone(record.latencies.ttft_ms)
        self.assertGreater(record.latencies.ttft_ms or 0.0, 5.0)
        self.assertGreater(record.latencies.tool_duration_ms or 0.0, 10.0)
        self.assertGreater(record.latencies.supervisor_overhead_ms or 0.0, 3.0)
        self.assertEqual(record.latencies.generation_duration_provenance, MetricProvenance.ESTIMATED)

        # Cost auto-estimation
        self.assertEqual(record.cost.provenance, MetricProvenance.ESTIMATED)
        self.assertEqual(record.cost.pricing_version, PricingRegistry.REGISTRY_VERSION)
        self.assertIsNotNone(record.cost.total_cost_usd)

    def test_08_immutable_dataclasses_enforcement(self) -> None:
        """Validate that all metric dataclasses enforce frozen immutability."""
        tokens = TokenMetrics(input_tokens=100, output_tokens=50, provenance=MetricProvenance.MEASURED)
        with self.assertRaises(FrozenInstanceError):
            tokens.input_tokens = 200  # type: ignore

        latencies = LatencyMetrics(total_duration_ms=100.0)
        with self.assertRaises(FrozenInstanceError):
            latencies.total_duration_ms = 200.0  # type: ignore

        cost = CostMetrics(total_cost_usd=1.0)
        with self.assertRaises(FrozenInstanceError):
            cost.total_cost_usd = 2.0  # type: ignore

        record = CognitiveTelemetryRecord(task_id="task_immut")
        with self.assertRaises(FrozenInstanceError):
            record.task_id = "task_other"  # type: ignore

    def test_09_database_level_immutability_triggers(self) -> None:
        """Validate SQLite trigger enforcement of append-only immutability on cognitive_telemetry."""
        rec = CognitiveTelemetryRecord(task_id="task_audit_immut", success=True)
        saved = self.store.record_cognitive_telemetry(rec)
        self.assertIsNotNone(saved.id)

        # Attempt UPDATE directly in SQLite: must raise sqlite3.IntegrityError
        with self.assertRaises(sqlite3.IntegrityError) as ctx_update:
            self.store.db.execute("UPDATE cognitive_telemetry SET task_id='tampered' WHERE id=?", (saved.id,))
        self.assertIn("immutable and append-only", str(ctx_update.exception))

        # Attempt DELETE directly in SQLite: must raise sqlite3.IntegrityError
        with self.assertRaises(sqlite3.IntegrityError) as ctx_delete:
            self.store.db.execute("DELETE FROM cognitive_telemetry WHERE id=?", (saved.id,))
        self.assertIn("immutable and cannot be deleted", str(ctx_delete.exception))

    def test_10_sqlite_persistence_queries_and_pagination(self) -> None:
        """Validate Store persistence, auto-increment ID assignment, filtering, and pagination."""
        rec1 = CognitiveTelemetryRecord(
            task_id="task_alpha",
            run_id="run_001",
            provider="openai",
            model_name="gpt-4o",
            tokens=TokenMetrics(input_tokens=500, output_tokens=100, provenance=MetricProvenance.MEASURED),
            latencies=LatencyMetrics(total_duration_ms=250.0, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=0.00225, provenance=MetricProvenance.ESTIMATED),
            success=True,
        )
        rec2 = CognitiveTelemetryRecord(
            task_id="task_alpha",
            run_id="run_002",
            provider="anthropic",
            model_name="claude-3-5-sonnet",
            tokens=TokenMetrics(input_tokens=800, output_tokens=200, provenance=MetricProvenance.MEASURED),
            latencies=LatencyMetrics(total_duration_ms=420.0, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=0.0054, provenance=MetricProvenance.ESTIMATED),
            success=False,
            error_category="rate_limit",
            error_message="Too Many Requests",
        )

        saved1 = self.store.record_cognitive_telemetry(rec1)
        saved2 = self.store.record_cognitive_telemetry(rec2)

        self.assertIsNotNone(saved1.id)
        self.assertIsNotNone(saved2.id)
        self.assertEqual(saved2.id, (saved1.id or 0) + 1)

        # Query filter by task_id
        records = self.store.query_cognitive_telemetry(task_id="task_alpha")
        self.assertEqual(len(records), 2)

        # Query filter by success
        failed = self.store.query_cognitive_telemetry(success=False)
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error_category, "rate_limit")

    def test_11_aggregate_statistics_unknown_distinction_and_truncation(self) -> None:
        """Validate aggregate statistical summary, distinction between 0 and unknown, and truncation signaling."""
        # 1. Test run with purely unknown costs and tokens
        unk_rec = CognitiveTelemetryRecord(
            task_id="task_unk",
            provider="mock",
            model_name="unknown-model",
            tokens=TokenMetrics(provenance=MetricProvenance.UNKNOWN),
            cost=CostMetrics(provenance=MetricProvenance.UNKNOWN),
            latencies=LatencyMetrics(total_duration_ms=100.0),
        )
        self.store.record_cognitive_telemetry(unk_rec)
        agg_unk = self.store.aggregate_cognitive_telemetry(task_id="task_unk")
        self.assertIsNone(agg_unk.total_cost_usd)  # Must be None, NEVER 0.0
        self.assertIsNone(agg_unk.total_tokens)
        self.assertEqual(agg_unk.records_with_unknown_cost, 1)
        self.assertEqual(agg_unk.records_with_unknown_tokens, 1)

        # 2. Insert 10 records with known values for truncation testing
        for i in range(1, 11):
            self.store.record_cognitive_telemetry(
                CognitiveTelemetryRecord(
                    task_id="task_trunc",
                    run_id=f"run_{i}",
                    provider="mock",
                    model_name="test-model",
                    tokens=TokenMetrics(input_tokens=100 * i, output_tokens=50 * i, provenance=MetricProvenance.MEASURED),
                    latencies=LatencyMetrics(total_duration_ms=50.0 * i, provenance=MetricProvenance.MEASURED),
                    cost=CostMetrics(total_cost_usd=0.0001 * i, provenance=MetricProvenance.ESTIMATED),
                )
            )

        # Unbounded aggregation: all 10 records, is_truncated=False
        agg_all = self.store.aggregate_cognitive_telemetry(task_id="task_trunc")
        self.assertEqual(agg_all.sample_size, 10)
        self.assertEqual(agg_all.total_matched_records, 10)
        self.assertFalse(agg_all.is_truncated)
        self.assertIsNone(agg_all.truncation_note)

        # Explicitly bounded sampling: max_records=4, is_truncated=True
        agg_sampled = self.store.aggregate_cognitive_telemetry(task_id="task_trunc", max_records=4)
        self.assertEqual(agg_sampled.sample_size, 4)
        self.assertEqual(agg_sampled.total_matched_records, 10)
        self.assertTrue(agg_sampled.is_truncated)
        self.assertIn("Agregação truncada", agg_sampled.truncation_note or "")

    def test_12_non_interference_safe_record(self) -> None:
        """Validate that safe_record_telemetry never raises exceptions or disturbs supervisor runs."""
        # Non-Store object or None
        result = safe_record_telemetry(None, {"task_id": "task_nil"})
        self.assertIsNone(result)

        class BrokenStore:
            def record_cognitive_telemetry(self, rec):
                raise sqlite3.OperationalError("Database is locked or disk is full")

        broken = BrokenStore()
        result_broken = safe_record_telemetry(broken, {"task_id": "task_fail"})
        self.assertIsNone(result_broken)

        # Valid store passes through cleanly
        valid_rec = CognitiveTelemetryRecord(task_id="task_pass", success=True)
        res = safe_record_telemetry(self.store, valid_rec)
        self.assertIsNotNone(res)
        self.assertIsNotNone(res.id if res else None)

    def test_13_concurrent_multithreaded_recording(self) -> None:
        """Validate concurrent thread safety and zero database corruption under parallel writes."""
        thread_count = 8
        records_per_thread = 25
        total_expected = thread_count * records_per_thread

        def worker(thread_idx: int):
            for i in range(records_per_thread):
                rec = CognitiveTelemetryRecord(
                    task_id=f"task_concurrent_{thread_idx}",
                    run_id=f"run_{thread_idx}_{i}",
                    provider="mock",
                    model_name="test-model",
                    tokens=TokenMetrics(input_tokens=100, output_tokens=50, provenance=MetricProvenance.MEASURED),
                    latencies=LatencyMetrics(total_duration_ms=50.0, provenance=MetricProvenance.MEASURED),
                    cost=CostMetrics(total_cost_usd=0.0001, provenance=MetricProvenance.ESTIMATED),
                    success=True,
                )
                self.store.record_cognitive_telemetry(rec)

        with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(worker, t) for t in range(thread_count)]
            for fut in concurrent.futures.as_completed(futures):
                fut.result()

        all_saved = self.store.query_cognitive_telemetry(limit=1000)
        self.assertEqual(len(all_saved), total_expected)

        agg = self.store.aggregate_cognitive_telemetry()
        self.assertEqual(agg.sample_size, total_expected)
        self.assertEqual(agg.success_count, total_expected)

    def test_14_schema_idempotence_and_triggers_preservation(self) -> None:
        """Validate that repeated Store initialization preserves existing data and triggers."""
        rec = CognitiveTelemetryRecord(task_id="task_idemp", model_name="gpt-4o", success=True)
        saved = self.store.record_cognitive_telemetry(rec)
        self.assertIsNotNone(saved.id)

        # Call _init_schema again
        self.store._init_schema()

        # Reopen store from same directory
        store2 = Store(self.test_dir)
        try:
            records = store2.query_cognitive_telemetry(task_id="task_idemp")
            self.assertEqual(len(records), 1)
            # Verify triggers still active
            with self.assertRaises(sqlite3.IntegrityError):
                store2.db.execute("UPDATE cognitive_telemetry SET task_id='tampered' WHERE id=?", (saved.id,))
        finally:
            store2.db.close()


if __name__ == "__main__":
    unittest.main()
