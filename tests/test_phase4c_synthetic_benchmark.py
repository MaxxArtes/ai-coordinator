"""Comprehensive test suite for Phase 4C.0.2: Synthetic Datasets and Offline Evaluation Oracles."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest

from ai_coordinator import (
    Store,
    MetricProvenance,
    TokenMetrics,
    LatencyMetrics,
    CostMetrics,
    CognitiveTelemetryRecord,
    DATASET_VERSION,
    SyntheticTaskDefinition,
    EvaluationOracleReport,
    SyntheticDatasetGenerator,
    FunctionalOracle,
    EfficiencyOracle,
    SecurityOracle,
    SyntheticBenchmarkRunner,
    canonical_json_hash,
)


class TestPhase4CSyntheticBenchmark(unittest.TestCase):
    """Test suite validating determinism, oracles, and offline execution of Phase 4C.0.2."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="test_phase4c_bench_")
        self.store = Store(self.test_dir)

    def tearDown(self) -> None:
        if hasattr(self, "store") and self.store and self.store.db:
            try:
                self.store.db.close()
            except Exception:
                pass

    def test_01_suite_a_determinism_and_bit_for_bit_reproducibility(self) -> None:
        """Validate that Suite A generation is strictly deterministic and identical across repeated calls."""
        suite_run1 = SyntheticDatasetGenerator.generate_suite_a(seed=4242)
        suite_run2 = SyntheticDatasetGenerator.generate_suite_a(seed=4242)

        self.assertEqual(len(suite_run1), len(suite_run2))
        self.assertEqual(len(suite_run1), 3)

        for t1, t2 in zip(suite_run1, suite_run2):
            self.assertEqual(t1.task_id, t2.task_id)
            self.assertEqual(t1.expected_hash, t2.expected_hash)
            self.assertEqual(t1.input_payload, t2.input_payload)
            self.assertEqual(t1.expected_output, t2.expected_output)

        # Confirm Task A1 structure
        t_a1 = suite_run1[0]
        self.assertEqual(t_a1.task_id, "suite_a_01_etl_aggregation")
        self.assertEqual(t_a1.version, DATASET_VERSION)
        self.assertIn("summary_by_category", t_a1.expected_output)
        self.assertEqual(t_a1.expected_output["total_records_processed"], 20)

        # Confirm Task A2 structure
        t_a2 = suite_run1[1]
        self.assertEqual(t_a2.task_id, "suite_a_02_reconciliation")
        self.assertTrue(t_a2.expected_output["balanced"])
        self.assertEqual(t_a2.expected_output["net_flow"], 0)

        # Confirm Task A3 structure
        t_a3 = suite_run1[2]
        self.assertEqual(t_a3.task_id, "suite_a_03_schema_invariants")
        self.assertGreater(t_a3.expected_output["valid_records_count"], 0)

    def test_02_suite_b_multi_hop_dag_and_precedence(self) -> None:
        """Validate Suite B DAG structures, fork-join partitions, and dependency resolution."""
        suite_b = SyntheticDatasetGenerator.generate_suite_b(seed=8484)
        self.assertEqual(len(suite_b), 3)

        # Task B1: 5-step pipeline trace
        t_b1 = suite_b[0]
        self.assertEqual(t_b1.task_id, "suite_b_01_sequential_dag_5_step")
        self.assertEqual(t_b1.expected_output["steps_completed"], 5)
        self.assertEqual(len(t_b1.expected_output["pipeline_trace"]), 5)

        # Task B2: Fork-Join parallel partitions
        t_b2 = suite_b[1]
        self.assertEqual(t_b2.task_id, "suite_b_02_fork_join_parallel")
        self.assertEqual(t_b2.expected_output["partition_count"], 4)
        calc_global_sum = sum(t_b2.expected_output["partition_results"].values())
        self.assertEqual(t_b2.expected_output["global_sum"], calc_global_sum)

        # Task B3: Acyclic topological dependency resolution
        t_b3 = suite_b[2]
        self.assertEqual(t_b3.task_id, "suite_b_03_dependency_fencing")
        self.assertTrue(t_b3.expected_output["is_acyclic"])
        self.assertEqual(t_b3.expected_output["topological_order"], ["node_a", "node_b", "node_c"])

    def test_03_suite_c_adversarial_payloads_and_fault_injection(self) -> None:
        """Validate Suite C adversarial secret injections in keys/values and fault handling."""
        suite_c = SyntheticDatasetGenerator.generate_suite_c(seed=1212)
        self.assertEqual(len(suite_c), 3)

        # Task C1: Secret Injection in Keys and Values
        t_c1 = suite_c[0]
        self.assertEqual(t_c1.task_id, "suite_c_01_adversarial_secret_injection")
        raw_input_text = json.dumps(t_c1.input_payload)
        self.assertIn("sk-proj-1234567890abcdef12345678", raw_input_text)
        self.assertIn("ghp_111122223333444455556666777788889999", raw_input_text)
        self.assertIn("AIzaSyD-1234567890abcdef1234567890abcde", raw_input_text)

        # Expected output must have all secrets redacted
        clean_text = json.dumps(t_c1.expected_output)
        self.assertNotIn("sk-proj", clean_text)
        self.assertNotIn("ghp_", clean_text)
        self.assertNotIn("AIza", clean_text)
        self.assertIn("[REDACTED_SECRET]", clean_text)

        # Task C2: Malformed Payload Recovery
        t_c2 = suite_c[1]
        self.assertEqual(t_c2.expected_output["error_category"], "MALFORMED_JSON_PAYLOAD")

        # Task C3: Budget Overrun Fencing
        t_c3 = suite_c[2]
        self.assertTrue(t_c3.expected_output["budget_exceeded"])
        self.assertEqual(t_c3.expected_output["action_taken"], "FENCED_HALT")

    def test_04_functional_oracle_exact_and_schema_scoring(self) -> None:
        """Validate FunctionalOracle exact canonical hash matching and partial schema scoring."""
        tasks = SyntheticDatasetGenerator.generate_suite_a(seed=4242)
        task = tasks[0]

        # 1. Exact output matches target hash
        success, score, actual_hash = FunctionalOracle.evaluate(task.expected_output, task)
        self.assertTrue(success)
        self.assertEqual(score, 1.0)
        self.assertEqual(actual_hash, task.expected_hash)

        # 2. Corrupted output fails exact match
        corrupted = dict(task.expected_output)
        corrupted["total_records_processed"] = 999
        success_corrupt, score_corrupt, hash_corrupt = FunctionalOracle.evaluate(corrupted, task)
        self.assertFalse(success_corrupt)
        self.assertNotEqual(hash_corrupt, task.expected_hash)
        self.assertGreater(score_corrupt, 0.0)
        self.assertLess(score_corrupt, 1.0)

        # 3. None output returns 0.0
        success_none, score_none, _ = FunctionalOracle.evaluate(None, task)
        self.assertFalse(success_none)
        self.assertEqual(score_none, 0.0)

    def test_05_efficiency_oracle_budget_and_cache_discount_audit(self) -> None:
        """Validate EfficiencyOracle budget bounds, provenance classification, and cache accounting."""
        tasks = SyntheticDatasetGenerator.generate_suite_a(seed=4242)
        task = tasks[0]

        # 1. Compliant execution under budget
        telemetry_ok = CognitiveTelemetryRecord(
            task_id=task.task_id,
            provider="openai",
            model_name="gpt-4o",
            tokens=TokenMetrics(input_tokens=1000, output_tokens=250, cache_read_tokens=500, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=0.004375, input_cost_usd=0.00125, cache_read_cost_usd=0.000625, output_cost_usd=0.0025, provenance=MetricProvenance.ESTIMATED),
            latencies=LatencyMetrics(total_duration_ms=150.0, provenance=MetricProvenance.MEASURED),
            success=True,
        )

        eff_ok, tok_ok, cost_ok, violations = EfficiencyOracle.evaluate(telemetry_ok, task)
        self.assertTrue(eff_ok)
        self.assertTrue(tok_ok)
        self.assertTrue(cost_ok)
        self.assertEqual(len(violations), 0)

        # 2. Token budget exceeded
        telemetry_over_tok = CognitiveTelemetryRecord(
            task_id=task.task_id,
            tokens=TokenMetrics(total_tokens=10000, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=0.001, provenance=MetricProvenance.ESTIMATED),
            success=True,
        )
        eff_bad, tok_bad, cost_bad, violations = EfficiencyOracle.evaluate(telemetry_over_tok, task)
        self.assertFalse(eff_bad)
        self.assertFalse(tok_bad)
        self.assertTrue(cost_bad)
        self.assertTrue(any("Limite de tokens excedido" in v for v in violations))

    def test_06_security_oracle_zero_secret_and_sqlite_immutability(self) -> None:
        """Validate SecurityOracle credential scanning and SQLite trigger enforcement."""
        # 1. Compliant sanitized record
        rec_clean = CognitiveTelemetryRecord(
            task_id="task_sec_01",
            metadata={"clean_key": "clean_value"},
            success=True,
        )
        saved = self.store.record_cognitive_telemetry(rec_clean)

        sec_ok, zero_leak, sqlite_ok, violations = SecurityOracle.evaluate(saved, self.store)
        self.assertTrue(sec_ok)
        self.assertTrue(zero_leak)
        self.assertTrue(sqlite_ok)
        self.assertEqual(len(violations), 0)

        # 2. Tampered record with leaked error message
        rec_leaked = CognitiveTelemetryRecord(
            task_id="task_sec_02",
            error_message="Failed with credential sk-proj-1234567890abcdef12345678",
            success=False,
        )
        # Bypassing __post_init__ redaction artificially to test Oracle vigilance
        object.__setattr__(rec_leaked, "error_message", "Failed with sk-proj-1234567890abcdef12345678")
        sec_bad, zero_leak_bad, _, sec_violations = SecurityOracle.evaluate(rec_leaked, self.store)
        self.assertFalse(sec_bad)
        self.assertFalse(zero_leak_bad)
        self.assertTrue(any("Credencial vazada" in v for v in sec_violations))

    def test_07_synthetic_benchmark_runner_offline_end_to_end(self) -> None:
        """Validate SyntheticBenchmarkRunner offline execution, mock solvers, and report generation."""
        runner = SyntheticBenchmarkRunner(store=self.store)
        tasks = SyntheticDatasetGenerator.generate_suite_a(seed=4242, count=1)
        task = tasks[0]

        # Solver mock returning perfect expected output
        def perfect_solver(payload: Dict[str, Any]) -> Dict[str, Any]:
            return task.expected_output

        report = runner.run_task(task, perfect_solver, provider="test", model_name="test-model")

        self.assertIsInstance(report, EvaluationOracleReport)
        self.assertEqual(report.task_id, task.task_id)
        self.assertTrue(report.functional_success)
        self.assertEqual(report.correctness_score, 1.0)
        self.assertTrue(report.efficiency_success)
        self.assertTrue(report.security_success)
        self.assertTrue(report.passed_all)
        self.assertIsNotNone(report.oracle_signature)
        self.assertGreater(len(report.oracle_signature), 0)

        # Verify serialization of report
        rep_dict = report.to_dict()
        self.assertEqual(rep_dict["task_id"], task.task_id)
        self.assertTrue(rep_dict["passed_all"])
        dumped = json.dumps(rep_dict)
        self.assertIn(task.task_id, dumped)


if __name__ == "__main__":
    unittest.main()
