"""Unit and adversarial test suite for Phase 4C.0.4: Scientific Baseline Execution."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import tempfile
import unittest

from ai_coordinator import (
    Store,
    DATASET_VERSION,
    MetricProvenance,
    SyntheticDatasetGenerator,
    BaselineRunner,
    CanonicalSolverRegistry,
    BaselineExecutionReport,
    MetricDistribution,
    TaskTimingBreakdown,
    BaselineEnvironmentMetadata,
    canonical_json_hash,
)


class TestPhase4CBaselineExecution(unittest.TestCase):
    """Test suite validating offline baseline execution, independent solvers, and statistical reporting."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="test_phase4c_baseline_")
        self.store = Store(self.test_dir)
        self.hmac_secret = "test_baseline_hmac_secret_key_2026"

    def tearDown(self) -> None:
        if hasattr(self, "store") and self.store and self.store.db:
            try:
                self.store.db.close()
            except Exception:
                pass

    def test_01_canonical_solvers_independent_accuracy(self) -> None:
        """Validate that independent solvers reproduce expected outputs without accessing expected_output."""
        # Suite A
        tasks_a = SyntheticDatasetGenerator.generate_suite_a(seed=4242)
        for t in tasks_a:
            solver = CanonicalSolverRegistry.get_solver(t.task_type)
            # Invoke solver solely with input_payload
            actual = solver(t.input_payload)
            self.assertEqual(
                canonical_json_hash(actual),
                t.expected_hash,
                f"Solver independente para '{t.task_id}' divergiu do gabarito!",
            )

        # Suite B
        tasks_b = SyntheticDatasetGenerator.generate_suite_b(seed=8484)
        for t in tasks_b:
            solver = CanonicalSolverRegistry.get_solver(t.task_type)
            actual = solver(t.input_payload)
            self.assertEqual(
                canonical_json_hash(actual),
                t.expected_hash,
                f"Solver independente para '{t.task_id}' divergiu do gabarito!",
            )

        # Suite C
        tasks_c = SyntheticDatasetGenerator.generate_suite_c(seed=1212)
        for t in tasks_c:
            solver = CanonicalSolverRegistry.get_solver(t.task_type)
            actual = solver(t.input_payload)
            self.assertEqual(
                canonical_json_hash(actual),
                t.expected_hash,
                f"Solver independente para '{t.task_id}' divergiu do gabarito!",
            )

    def test_02_timing_breakdown_and_overhead_segregation(self) -> None:
        """Validate strict separation between task execution, instrumentation overhead, and validation."""
        runner = BaselineRunner(store=self.store, hmac_key=self.hmac_secret)
        report = runner.run_baseline(iterations=1)

        self.assertGreater(report.total_tasks_executed, 0)
        for task_res in report.task_execution_results:
            timing = task_res.timing_breakdown
            self.assertGreaterEqual(timing.task_execution_ms, 0.0)
            self.assertGreaterEqual(timing.telemetry_overhead_ms, 0.0)
            self.assertGreaterEqual(timing.validation_ms, 0.0)
            self.assertGreater(timing.total_elapsed_ms, 0.0)

            # Sum of components must roughly match total elapsed
            component_sum = timing.task_execution_ms + timing.telemetry_overhead_ms + timing.validation_ms
            self.assertAlmostEqual(timing.total_elapsed_ms, component_sum, delta=5.0)

    def test_03_statistical_metric_distribution_calculations(self) -> None:
        """Validate mathematical correctness of mean, std_dev, and percentiles in MetricDistribution."""
        # Simple deterministic sequence: 10, 20, 30, 40, 50
        dist = MetricDistribution.compute([10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertEqual(dist.count, 5)
        self.assertEqual(dist.mean, 30.0)
        self.assertEqual(dist.p50, 30.0)
        self.assertEqual(dist.min_val, 10.0)
        self.assertEqual(dist.max_val, 50.0)
        self.assertAlmostEqual(dist.std_dev, 14.1421, places=3)

    def test_04_baseline_environment_metadata_and_provenance(self) -> None:
        """Validate that baseline captures environment, git commit, python runtime, and dataset version."""
        env = BaselineEnvironmentMetadata.capture()
        self.assertIsNotNone(env.git_commit)
        self.assertIsNotNone(env.python_version)
        self.assertIsNotNone(env.platform_system)
        self.assertEqual(env.dataset_version, DATASET_VERSION)
        self.assertTrue(env.is_reference_baseline)
        self.assertFalse(env.adaptive_heuristics_active)

    def test_05_baseline_manifest_integrity_and_hmac_tamper_detection(self) -> None:
        """Validate baseline manifest signature and tamper detection across summary metrics."""
        runner = BaselineRunner(store=self.store, hmac_key=self.hmac_secret)
        report = runner.run_baseline(iterations=1)

        # Baseline: untampered report passes both checks
        self.assertTrue(report.verify_integrity())
        self.assertTrue(report.verify_signature(self.hmac_secret))
        self.assertFalse(report.verify_signature("wrong_key_12345"))

        # Tamper 1: Modify success rate
        tampered_success = dataclasses.replace(
            report,
            success_rates={"passed_all_rate": 0.50},
        )
        self.assertFalse(tampered_success.verify_integrity())
        self.assertFalse(tampered_success.verify_signature(self.hmac_secret))

        # Tamper 2: Modify token count
        tampered_tokens = dataclasses.replace(
            report,
            token_aggregates={"total_tokens": 9999999},
        )
        self.assertFalse(tampered_tokens.verify_integrity())
        self.assertFalse(tampered_tokens.verify_signature(self.hmac_secret))

        # Tamper 3: Modify git commit in environment
        tampered_env = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, git_commit="forged_commit_sha"),
        )
        self.assertFalse(tampered_env.verify_integrity())
        self.assertFalse(tampered_env.verify_signature(self.hmac_secret))

    def test_06_baseline_reproducibility_across_identical_seeds(self) -> None:
        """Validate that repeated runs with identical seeds produce identical task sets and aggregates."""
        runner1 = BaselineRunner(store=self.store)
        runner2 = BaselineRunner(store=self.store)

        rep1 = runner1.run_baseline(iterations=1, seed_suite_a=4242, seed_suite_b=8484, seed_suite_c=1212)
        rep2 = runner2.run_baseline(iterations=1, seed_suite_a=4242, seed_suite_b=8484, seed_suite_c=1212)

        self.assertEqual(rep1.total_tasks_executed, rep2.total_tasks_executed)
        self.assertEqual(rep1.token_aggregates["total_tokens"], rep2.token_aggregates["total_tokens"])
        self.assertEqual(rep1.cost_aggregates["total_cost_usd"], rep2.cost_aggregates["total_cost_usd"])
        self.assertEqual(rep1.success_rates, rep2.success_rates)
        self.assertEqual(rep1.success_rates["functional_success_rate"], 1.0)
        self.assertEqual(rep1.success_rates["passed_all_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
