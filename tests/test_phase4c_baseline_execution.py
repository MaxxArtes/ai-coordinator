"""Unit and adversarial test suite for Phase 4C.0.4: Scientific Baseline Execution."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
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
from ai_coordinator.synthetic_benchmark import _normalize_json_structure


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
        # Sample standard deviation with Bessel's correction (ddof=1)
        self.assertAlmostEqual(dist.std_dev, 15.8114, places=3)
        self.assertAlmostEqual(dist.sample_std_dev, 15.8114, places=3)
        # Population standard deviation (ddof=0)
        self.assertAlmostEqual(dist.population_std_dev, 14.1421, places=3)

    def test_04_baseline_environment_metadata_and_provenance(self) -> None:
        """Validate that baseline captures environment, git commit, python runtime, and dataset version."""
        env = BaselineEnvironmentMetadata.capture(
            seeds={"suite_a": 42, "suite_b": 43, "suite_c": 44},
            dataset_hashes={"suite_a": "hash_a", "suite_b": "hash_b", "suite_c": "hash_c"},
            task_execution_order=["task_1", "task_2"],
            iterations=1,
        )
        self.assertIsNotNone(env.git_commit)
        self.assertIsNotNone(env.git_branch)
        self.assertIsInstance(env.git_dirty, bool)
        self.assertEqual(env.iterations, 1)
        self.assertEqual(env.total_tasks_per_iteration, len(env.task_execution_order))
        self.assertEqual(env.seeds["suite_a"], 42)
        self.assertEqual(env.dataset_hashes["suite_a"], "hash_a")
        self.assertEqual(env.task_execution_order, ["task_1", "task_2"])
        self.assertEqual(env.pricing_model, "synthetic-reference")
        self.assertIn("tokens", env.metric_provenance_summary)
        self.assertIsNotNone(env.python_version)
        self.assertIsNotNone(env.platform_system)
        self.assertEqual(env.dataset_version, DATASET_VERSION)
        self.assertTrue(env.is_reference_baseline)
        self.assertFalse(env.adaptive_heuristics_active)

    def test_05_baseline_manifest_integrity_and_hmac_tamper_detection(self) -> None:
        """Validate baseline manifest signature and tamper detection across summary metrics."""
        runner = BaselineRunner(store=self.store, hmac_key=self.hmac_secret)
        report = runner.run_baseline(iterations=1)

        # Baseline: untampered report passes both checks and full deep integrity
        self.assertTrue(report.verify_integrity())
        self.assertTrue(report.verify_signature(self.hmac_secret))
        self.assertTrue(report.verify_full_report_integrity(self.hmac_secret))
        self.assertFalse(report.verify_signature("wrong_key_12345"))
        self.assertFalse(report.verify_full_report_integrity("wrong_key_12345"))

        # Tamper 0: Modify individual task result (e.g. alter canonical_output_hash) without modifying summary aggregates
        tampered_tasks = list(report.task_execution_results)
        bad_oracle_rep = dataclasses.replace(
            tampered_tasks[0].oracle_report,
            canonical_output_hash="tampered_task_hash_9999",
        )
        tampered_tasks[0] = dataclasses.replace(tampered_tasks[0], oracle_report=bad_oracle_rep)
        tampered_individual_rep = dataclasses.replace(
            report,
            task_execution_results=tampered_tasks,
        )
        self.assertFalse(
            tampered_individual_rep.verify_integrity(),
            "Adulteração individual de tarefa deve invalidar verify_integrity() via task_results_digest",
        )
        self.assertFalse(
            tampered_individual_rep.verify_signature(self.hmac_secret),
            "Adulteração individual de tarefa deve invalidar verify_signature() via task_results_digest",
        )
        self.assertFalse(
            tampered_individual_rep.verify_full_report_integrity(self.hmac_secret),
            "Adulteração individual de tarefa deve invalidar verify_full_report_integrity()",
        )

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

        # Tamper 4: Modify seeds in environment
        tampered_seeds = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, seeds={"suite_a": 99999}),
        )
        self.assertFalse(tampered_seeds.verify_integrity(), "Adulteração de seeds deve invalidar verify_integrity()")
        self.assertFalse(tampered_seeds.verify_signature(self.hmac_secret), "Adulteração de seeds deve invalidar verify_signature()")
        self.assertFalse(tampered_seeds.verify_full_report_integrity(self.hmac_secret))

        # Tamper 5: Modify dataset_hashes in environment
        tampered_hashes = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, dataset_hashes={"suite_a": "forged_dataset_hash"}),
        )
        self.assertFalse(tampered_hashes.verify_integrity(), "Adulteração de dataset_hashes deve invalidar verify_integrity()")
        self.assertFalse(tampered_hashes.verify_signature(self.hmac_secret), "Adulteração de dataset_hashes deve invalidar verify_signature()")
        self.assertFalse(tampered_hashes.verify_full_report_integrity(self.hmac_secret))

        # Tamper 6: Modify git_branch in environment
        tampered_branch = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, git_branch="forged_branch_name"),
        )
        self.assertFalse(tampered_branch.verify_integrity(), "Adulteração de git_branch deve invalidar verify_integrity()")
        self.assertFalse(tampered_branch.verify_signature(self.hmac_secret), "Adulteração de git_branch deve invalidar verify_signature()")

        # Tamper 7: Modify git_dirty in environment
        tampered_dirty = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, git_dirty=not report.environment.git_dirty),
        )
        self.assertFalse(tampered_dirty.verify_integrity(), "Adulteração de git_dirty deve invalidar verify_integrity()")
        self.assertFalse(tampered_dirty.verify_signature(self.hmac_secret), "Adulteração de git_dirty deve invalidar verify_signature()")

        # Tamper 8: Modify pricing_registry_version in environment
        tampered_pricing = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, pricing_registry_version="9999.99.99"),
        )
        self.assertFalse(tampered_pricing.verify_integrity(), "Adulteração de pricing_registry_version deve invalidar verify_integrity()")
        self.assertFalse(tampered_pricing.verify_signature(self.hmac_secret), "Adulteração de pricing_registry_version deve invalidar verify_signature()")

        # Tamper 9: Modify task_execution_order in environment
        tampered_order = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, task_execution_order=["forged_task_order"]),
        )
        self.assertFalse(tampered_order.verify_integrity(), "Adulteração de task_execution_order deve invalidar verify_integrity()")
        self.assertFalse(tampered_order.verify_signature(self.hmac_secret), "Adulteração de task_execution_order deve invalidar verify_signature()")

        # Tamper 10: Modify pricing_model in environment
        tampered_pmodel = dataclasses.replace(
            report,
            environment=dataclasses.replace(report.environment, pricing_model="forged_pricing_model"),
        )
        self.assertFalse(tampered_pmodel.verify_integrity(), "Adulteração de pricing_model deve invalidar verify_integrity()")
        self.assertFalse(tampered_pmodel.verify_signature(self.hmac_secret), "Adulteração de pricing_model deve invalidar verify_signature()")

        # Tamper 11: Missing/stripped root oracle_signature under authenticated verification
        tampered_root_sig = dataclasses.replace(report, oracle_signature=None)
        self.assertFalse(
            tampered_root_sig.verify_full_report_integrity(self.hmac_secret),
            "Ausência de assinatura raiz deve reprovar verify_full_report_integrity no modo autenticado",
        )
        # However, in unauthenticated hash integrity mode (hmac_key=None), unsigned report passes hash verification
        self.assertTrue(
            tampered_root_sig.verify_full_report_integrity(None),
            "Modo de integridade por hash (sem chave) deve aceitar relatório sem assinatura desde que íntegro",
        )

        # Tamper 12: Missing/stripped oracle_signature on an individual task under authenticated verification
        tampered_tasks_no_sig = list(report.task_execution_results)
        tampered_tasks_no_sig[0] = dataclasses.replace(
            tampered_tasks_no_sig[0],
            oracle_report=dataclasses.replace(tampered_tasks_no_sig[0].oracle_report, oracle_signature=None),
        )
        raw_rep_no_task_sig = dataclasses.replace(report, task_execution_results=tampered_tasks_no_sig)
        new_digest = raw_rep_no_task_sig.compute_task_results_digest()
        rep_no_task_sig = dataclasses.replace(raw_rep_no_task_sig, task_results_digest=new_digest)
        # Re-sign root with updated digest to strictly isolate individual task signature failure
        manifest = rep_no_task_sig.compute_canonical_manifest()
        m_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        h_val = hashlib.sha256(m_bytes).hexdigest()
        s_val = hmac.new(self.hmac_secret.encode("utf-8"), m_bytes, hashlib.sha256).hexdigest()
        rep_no_task_sig = dataclasses.replace(rep_no_task_sig, integrity_hash=h_val, oracle_signature=s_val)

        self.assertFalse(
            rep_no_task_sig.verify_full_report_integrity(self.hmac_secret),
            "Ausência de assinatura em tarefa individual deve reprovar verify_full_report_integrity no modo autenticado",
        )

    def test_06_baseline_reproducibility_across_identical_seeds(self) -> None:
        """Validate that repeated runs with identical seeds produce identical task sets and aggregates."""
        runner1 = BaselineRunner(store=self.store)
        runner2 = BaselineRunner(store=self.store)

        rep1 = runner1.run_baseline(iterations=1, seed_suite_a=4242, seed_suite_b=8484, seed_suite_c=1212)
        rep2 = runner2.run_baseline(iterations=1, seed_suite_a=4242, seed_suite_b=8484, seed_suite_c=1212)

        self.assertEqual(rep1.total_tasks_executed, 9)
        self.assertEqual(rep2.total_tasks_executed, 9)
        self.assertEqual(rep1.total_tasks_executed, rep2.total_tasks_executed)
        self.assertEqual(rep1.token_aggregates["total_tokens"], rep2.token_aggregates["total_tokens"])
        self.assertEqual(rep1.cost_aggregates["total_cost_usd"], rep2.cost_aggregates["total_cost_usd"])
        self.assertEqual(rep1.success_rates, rep2.success_rates)
        self.assertEqual(rep1.success_rates["functional_success_rate"], 1.0)
        self.assertEqual(rep1.success_rates["efficiency_success_rate"], 1.0)
        self.assertEqual(rep1.success_rates["security_pass_rate"], 1.0)
        self.assertEqual(rep1.success_rates["security_unverified_rate"], 0.0)
        self.assertEqual(rep1.success_rates["security_fail_rate"], 0.0)
        self.assertEqual(rep1.success_rates["passed_all_rate"], 1.0)

    def test_07_baseline_serialization_deserialization_and_cli_verifier(self) -> None:
        """Validate lossless round-trip serialization and standalone CLI verifier execution."""
        runner = BaselineRunner(store=self.store, hmac_key=self.hmac_secret)
        original_report = runner.run_baseline(iterations=1)

        # 1. Round-trip dictionary serialization
        report_dict = original_report.to_dict()
        reconstructed_report = BaselineExecutionReport.from_dict(report_dict)

        self.assertEqual(reconstructed_report.run_id, original_report.run_id)
        self.assertEqual(reconstructed_report.integrity_hash, original_report.integrity_hash)
        self.assertEqual(reconstructed_report.oracle_signature, original_report.oracle_signature)
        self.assertEqual(reconstructed_report.task_results_digest, original_report.task_results_digest)
        self.assertTrue(reconstructed_report.verify_integrity())
        self.assertTrue(reconstructed_report.verify_signature(self.hmac_secret))
        self.assertTrue(reconstructed_report.verify_full_report_integrity(self.hmac_secret))

        # 2. Test standalone CLI verifier script
        report_path = os.path.join(self.test_dir, "test_baseline_verify.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report_dict, f, indent=2)

        script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "verify_baseline_integrity.py"))

        # Case A: Compliant report passes verification
        res_ok = subprocess.run(
            [sys.executable, script_path, "--report", report_path, "--hmac-key", self.hmac_secret],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_ok.returncode, 0)
        self.assertIn("100% íntegro e autêntico", res_ok.stdout)

        # Case B: Tampered report is rejected with non-zero exit code
        tampered_dict = dict(report_dict)
        tampered_dict["token_aggregates"] = {"total_tokens": 999999}
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(tampered_dict, f, indent=2)

        res_bad = subprocess.run(
            [sys.executable, script_path, "--report", report_path, "--hmac-key", self.hmac_secret],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_bad.returncode, 1)
        self.assertIn("REPROVADO", res_bad.stderr)


if __name__ == "__main__":
    unittest.main()
