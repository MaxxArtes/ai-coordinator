"""Comprehensive test suite for Phase 4C.0.1: Cognitive Telemetry, Provenance, and Storage."""

from __future__ import annotations

import concurrent.futures
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
)
from ai_coordinator.telemetry import (
    scrub_sensitive_text,
    scrub_sensitive_dict,
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

    def test_02_token_metrics_accounting(self) -> None:
        """Validate token metrics calculations, auto-summing, and non-negative clamping."""
        tokens = TokenMetrics(
            input_tokens=150,
            output_tokens=75,
            cache_read_tokens=50,
            cache_creation_tokens=25,
            provenance=MetricProvenance.MEASURED,
        )
        self.assertEqual(tokens.total_tokens, 300)
        self.assertEqual(tokens.provenance, "measured")

        # Negative clamping
        neg_tokens = TokenMetrics(
            input_tokens=-10,
            output_tokens=-5,
            provenance=MetricProvenance.ESTIMATED,
        )
        self.assertEqual(neg_tokens.input_tokens, 0)
        self.assertEqual(neg_tokens.output_tokens, 0)
        self.assertEqual(neg_tokens.total_tokens, 0)

        # Dict round-trip
        data = tokens.to_dict()
        restored = TokenMetrics.from_dict(data)
        self.assertEqual(restored, tokens)

    def test_03_latency_metrics_decomposition(self) -> None:
        """Validate latency decomposition: TTFT, generation, tool duration, supervisor overhead."""
        latencies = LatencyMetrics(
            ttft_ms=120.5,
            generation_duration_ms=450.0,
            tool_duration_ms=150.0,
            supervisor_overhead_ms=30.0,
            total_duration_ms=630.0,
            provenance=MetricProvenance.MEASURED,
        )
        self.assertEqual(latencies.ttft_ms, 120.5)
        self.assertEqual(latencies.generation_duration_ms, 450.0)
        self.assertEqual(latencies.tool_duration_ms, 150.0)
        self.assertEqual(latencies.supervisor_overhead_ms, 30.0)
        self.assertEqual(latencies.total_duration_ms, 630.0)

        # Dict round-trip
        data = latencies.to_dict()
        restored = LatencyMetrics.from_dict(data)
        self.assertEqual(restored, latencies)

    def test_04_pricing_registry_and_cost_estimation(self) -> None:
        """Validate pricing registry calculation and strict provenance classification."""
        tokens = TokenMetrics(
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_read_tokens=500_000,
            cache_creation_tokens=200_000,
            provenance=MetricProvenance.MEASURED,
        )

        # Known Anthropic model: claude-3-5-sonnet
        # input: $3.00, output: $15.00/M, cache_read: $0.30/M, cache_creation: $3.75/M
        cost = PricingRegistry.estimate_cost("claude-3-5-sonnet", tokens)
        self.assertEqual(cost.provenance, MetricProvenance.ESTIMATED)
        self.assertAlmostEqual(cost.input_cost_usd or 0.0, 3.00, places=4)
        self.assertAlmostEqual(cost.output_cost_usd or 0.0, 1.50, places=4)
        self.assertAlmostEqual(cost.cache_read_cost_usd or 0.0, 0.15, places=4)
        self.assertAlmostEqual(cost.cache_creation_cost_usd or 0.0, 0.75, places=4)
        self.assertAlmostEqual(cost.total_cost_usd or 0.0, 5.40, places=4)

        # Unrecognized model: must return UNKNOWN and total_cost_usd=None (NEVER fabricate costs)
        unrecognized_cost = PricingRegistry.estimate_cost("unknown-local-llama-999b", tokens)
        self.assertEqual(unrecognized_cost.provenance, MetricProvenance.UNKNOWN)
        self.assertIsNone(unrecognized_cost.total_cost_usd)

        # Custom model registration
        PricingRegistry.register_pricing(
            "custom-research-model",
            input_per_m=2.0,
            output_per_m=8.0,
            cache_read_per_m=0.5,
            cache_creation_per_m=1.0,
        )
        custom_cost = PricingRegistry.estimate_cost("custom-research-model", tokens)
        self.assertEqual(custom_cost.provenance, MetricProvenance.ESTIMATED)
        self.assertAlmostEqual(custom_cost.input_cost_usd or 0.0, 2.0, places=4)
        self.assertAlmostEqual(custom_cost.output_cost_usd or 0.0, 0.8, places=4)

    def test_05_secret_scrubbing_in_telemetry(self) -> None:
        """Validate strict scrubbing of secrets in error messages and metadata."""
        dirty_error = "Connection failed to https://api.openai.com/v1 with Bearer sk-proj-1234567890abcdef12345678"
        clean_error = scrub_sensitive_text(dirty_error)
        self.assertNotIn("sk-proj-1234567890abcdef12345678", clean_error or "")
        self.assertIn("[REDACTED_SECRET]", clean_error or "")

        dirty_meta = {
            "model": "gpt-4o",
            "api_key": "secret_key_12345",
            "auth_token": "bearer abcdef1234567890",
            "nested": {
                "credential": "super-secret-password",
                "normal_param": "temperature: 0.7",
            },
        }
        clean_meta = scrub_sensitive_dict(dirty_meta)
        self.assertEqual(clean_meta["api_key"], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["auth_token"], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["nested"]["credential"], "[REDACTED_SECRET]")
        self.assertEqual(clean_meta["nested"]["normal_param"], "temperature: 0.7")

        # Telemetry record creation automatically applies scrubbing
        rec = CognitiveTelemetryRecord(
            task_id="task_scrub_01",
            error_message="Fatal error with token: ghp_111122223333444455556666777788889999",
            metadata={"secret_field": "confidential_password"},
        )
        self.assertNotIn("ghp_111122223333444455556666777788889999", rec.error_message or "")
        self.assertIn("[REDACTED_SECRET]", rec.error_message or "")
        self.assertEqual(rec.metadata["secret_field"], "[REDACTED_SECRET]")

    def test_06_telemetry_collector_workflow(self) -> None:
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
            # Simulate initial inference delay before first token
            time.sleep(0.01)
            collector.mark_first_token()

            # Record streaming tokens
            for _ in range(5):
                collector.record_stream_chunk()

            # Measure tool invocation
            with collector.measure_tool():
                time.sleep(0.015)

            # Measure coordinator overhead
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
        self.assertEqual(record.run_id, "run_coll_01")
        self.assertEqual(record.provider, "anthropic")
        self.assertEqual(record.model_name, "claude-3-5-sonnet")
        self.assertEqual(record.tokens.input_tokens, 1000)
        self.assertEqual(record.tokens.output_tokens, 250)
        self.assertEqual(record.tokens.cache_read_tokens, 400)
        self.assertEqual(record.tokens.total_tokens, 1650)
        self.assertEqual(record.tokens.provenance, MetricProvenance.MEASURED)

        # Latencies check
        self.assertIsNotNone(record.latencies.ttft_ms)
        self.assertGreater(record.latencies.ttft_ms or 0.0, 5.0)
        self.assertGreater(record.latencies.tool_duration_ms or 0.0, 10.0)
        self.assertGreater(record.latencies.supervisor_overhead_ms or 0.0, 3.0)
        self.assertGreater(record.latencies.total_duration_ms, 25.0)

        # Cost automatically estimated from reference pricing
        self.assertEqual(record.cost.provenance, MetricProvenance.ESTIMATED)
        self.assertIsNotNone(record.cost.total_cost_usd)
        self.assertGreater(record.cost.total_cost_usd or 0.0, 0.0)

        # Counters
        self.assertEqual(record.tool_calls_count, 1)
        self.assertEqual(record.retries_count, 1)
        self.assertEqual(record.stream_chunks_count, 5)
        self.assertTrue(record.success)

    def test_07_sqlite_persistence_and_queries(self) -> None:
        """Validate Store persistence, auto-increment ID, query filters, and pagination."""
        rec1 = CognitiveTelemetryRecord(
            task_id="task_alpha",
            run_id="run_001",
            worker_identity="worker_A",
            worker_pid=1001,
            plan_revision=1,
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
            worker_identity="worker_B",
            worker_pid=1002,
            plan_revision=1,
            provider="anthropic",
            model_name="claude-3-5-sonnet",
            tokens=TokenMetrics(input_tokens=800, output_tokens=200, provenance=MetricProvenance.MEASURED),
            latencies=LatencyMetrics(total_duration_ms=420.0, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=0.0054, provenance=MetricProvenance.ESTIMATED),
            success=False,
            error_category="rate_limit",
            error_message="Too Many Requests",
        )

        rec3 = CognitiveTelemetryRecord(
            task_id="task_beta",
            run_id="run_003",
            provider="openai",
            model_name="gpt-4o-mini",
            tokens=TokenMetrics(input_tokens=300, output_tokens=50, provenance=MetricProvenance.MEASURED),
            latencies=LatencyMetrics(total_duration_ms=110.0, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=0.000075, provenance=MetricProvenance.ESTIMATED),
            success=True,
        )

        saved1 = self.store.record_cognitive_telemetry(rec1)
        saved2 = self.store.record_cognitive_telemetry(rec2)
        saved3 = self.store.record_cognitive_telemetry(rec3)

        self.assertIsNotNone(saved1.id)
        self.assertIsNotNone(saved2.id)
        self.assertIsNotNone(saved3.id)
        self.assertEqual(saved2.id, (saved1.id or 0) + 1)

        # Query all
        all_records = self.store.query_cognitive_telemetry()
        self.assertEqual(len(all_records), 3)

        # Filter by task_id
        alpha_records = self.store.query_cognitive_telemetry(task_id="task_alpha")
        self.assertEqual(len(alpha_records), 2)
        self.assertTrue(all(r.task_id == "task_alpha" for r in alpha_records))

        # Filter by provider
        openai_records = self.store.query_cognitive_telemetry(provider="openai")
        self.assertEqual(len(openai_records), 2)
        self.assertTrue(all(r.provider == "openai" for r in openai_records))

        # Filter by success
        failed_records = self.store.query_cognitive_telemetry(success=False)
        self.assertEqual(len(failed_records), 1)
        self.assertEqual(failed_records[0].error_category, "rate_limit")
        self.assertEqual(failed_records[0].error_message, "Too Many Requests")

        # Pagination
        paged = self.store.query_cognitive_telemetry(limit=2, offset=0)
        self.assertEqual(len(paged), 2)
        paged_next = self.store.query_cognitive_telemetry(limit=2, offset=2)
        self.assertEqual(len(paged_next), 1)

    def test_08_aggregate_statistics_and_percentiles(self) -> None:
        """Validate aggregate statistical summary: percentiles, provenance totals, and error rate."""
        # Insert 10 records with predictable latencies: 100, 200, ..., 1000 ms
        for i in range(1, 11):
            is_err = (i == 10)
            self.store.record_cognitive_telemetry(
                CognitiveTelemetryRecord(
                    task_id="task_agg",
                    run_id=f"run_{i:02d}",
                    provider="mock",
                    model_name="test-model",
                    tokens=TokenMetrics(
                        input_tokens=1000 * i,
                        output_tokens=100 * i,
                        provenance=MetricProvenance.MEASURED,
                    ),
                    latencies=LatencyMetrics(
                        total_duration_ms=100.0 * i,
                        ttft_ms=10.0 * i,
                        generation_duration_ms=80.0 * i,
                        tool_duration_ms=15.0 * i,
                        supervisor_overhead_ms=5.0 * i,
                        provenance=MetricProvenance.MEASURED,
                    ),
                    cost=CostMetrics(
                        total_cost_usd=0.001 * i,
                        provenance=MetricProvenance.ESTIMATED,
                    ),
                    tool_calls_count=2,
                    retries_count=1 if is_err else 0,
                    success=not is_err,
                    error_category="timeout" if is_err else None,
                )
            )

        agg = self.store.aggregate_cognitive_telemetry(task_id="task_agg")

        self.assertEqual(agg.record_count, 10)
        self.assertEqual(agg.total_input_tokens, sum(1000 * i for i in range(1, 11)))
        self.assertEqual(agg.total_output_tokens, sum(100 * i for i in range(1, 11)))
        self.assertEqual(agg.tokens_by_provenance[MetricProvenance.MEASURED], agg.total_tokens)
        self.assertAlmostEqual(agg.total_cost_usd, sum(0.001 * i for i in range(1, 11)), places=6)

        # Durations: min 100, max 1000, avg 550
        self.assertEqual(agg.avg_duration_ms, 550.0)
        self.assertEqual(agg.p50_duration_ms, 550.0)
        self.assertGreaterEqual(agg.p90_duration_ms, 900.0)
        self.assertGreaterEqual(agg.p99_duration_ms, 990.0)

        # Decomposed averages
        self.assertEqual(agg.avg_ttft_ms, 55.0)
        self.assertEqual(agg.avg_generation_duration_ms, 440.0)
        self.assertEqual(agg.avg_tool_duration_ms, 82.5)
        self.assertEqual(agg.avg_supervisor_overhead_ms, 27.5)

        # Activity counters
        self.assertEqual(agg.tool_calls_count, 20)
        self.assertEqual(agg.retries_count, 1)

        # Success / failure
        self.assertEqual(agg.success_count, 9)
        self.assertEqual(agg.failure_count, 1)
        self.assertEqual(agg.success_rate, 0.9)
        self.assertEqual(agg.error_breakdown.get("timeout"), 1)

    def test_09_non_interference_invariant(self) -> None:
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

    def test_10_concurrent_multithreaded_recording(self) -> None:
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
        self.assertEqual(agg.record_count, total_expected)
        self.assertEqual(agg.success_count, total_expected)
        self.assertEqual(agg.total_input_tokens, total_expected * 100)
        self.assertEqual(agg.total_output_tokens, total_expected * 50)

    def test_11_schema_migration_idempotence(self) -> None:
        """Validate that repeated Store initialization preserves existing telemetry data."""
        rec = CognitiveTelemetryRecord(
            task_id="task_idempotence",
            provider="openai",
            model_name="gpt-4o",
            success=True,
        )
        saved = self.store.record_cognitive_telemetry(rec)
        self.assertIsNotNone(saved.id)

        # Call _init_schema again
        self.store._init_schema()

        # Reopen store from same directory
        store2 = Store(self.test_dir)
        try:
            records = store2.query_cognitive_telemetry(task_id="task_idempotence")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].task_id, "task_idempotence")
            self.assertEqual(records[0].model_name, "gpt-4o")
        finally:
            store2.db.close()


if __name__ == "__main__":
    unittest.main()
