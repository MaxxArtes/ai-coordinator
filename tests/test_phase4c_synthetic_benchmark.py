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
    SecurityVerdict,
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
        runner = SyntheticBenchmarkRunner(store=self.store, hmac_key="test-oracle-hmac-key")
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
        self.assertEqual(report.observed_tokens.provenance, MetricProvenance.ESTIMATED)
        self.assertIsNotNone(report.integrity_hash)
        self.assertEqual(len(report.integrity_hash), 64)
        self.assertIsNotNone(report.oracle_signature)
        self.assertEqual(len(report.oracle_signature), 64)

        # Verify serialization of report
        rep_dict = report.to_dict()
        self.assertEqual(rep_dict["task_id"], task.task_id)
        self.assertTrue(rep_dict["passed_all"])
        self.assertEqual(rep_dict["integrity_hash"], report.integrity_hash)
        self.assertEqual(rep_dict["oracle_signature"], report.oracle_signature)
        dumped = json.dumps(rep_dict)
        self.assertIn(task.task_id, dumped)

    def test_08_adversarial_candidate_leaking_secrets_rejected_by_both_oracles(self) -> None:
        """Validate that a candidate returning raw credentials fails FunctionalOracle AND SecurityOracle."""
        tasks = SyntheticDatasetGenerator.generate_suite_c(seed=1212)
        task_c1 = tasks[0]  # suite_c_01_adversarial_secret_injection

        # Malicious / naive candidate returning raw unredacted input payload with secrets
        leaky_output = dict(task_c1.input_payload)

        # 1. FunctionalOracle must reject because actual output has literal secret instead of [REDACTED_SECRET]
        func_ok, correctness, actual_hash = FunctionalOracle.evaluate(leaky_output, task_c1)
        self.assertFalse(func_ok)
        self.assertNotEqual(actual_hash, task_c1.expected_hash)
        self.assertLess(correctness, 1.0)

        # 2. SecurityOracle must scan actual_output directly and flag the leak
        telemetry = CognitiveTelemetryRecord(
            task_id=task_c1.task_id,
            metadata={"phase": "test"},
            success=True,
        )
        sec_ok, zero_leak, sqlite_ok, violations = SecurityOracle.evaluate(
            actual_output=leaky_output,
            telemetry=telemetry,
            store=self.store,
        )
        self.assertFalse(sec_ok)
        self.assertFalse(zero_leak)
        self.assertTrue(any("saída (actual_output) do candidato" in v for v in violations))

    def test_09_efficiency_oracle_rejects_unknown_and_missing_metrics(self) -> None:
        """Validate that missing or UNKNOWN metrics produce budget rejection instead of false approval."""
        tasks = SyntheticDatasetGenerator.generate_suite_a(seed=4242)
        task = tasks[0]

        # Case A: Unknown token count
        telemetry_unknown_tok = CognitiveTelemetryRecord(
            task_id=task.task_id,
            tokens=TokenMetrics(input_tokens=None, output_tokens=None, provenance=MetricProvenance.UNKNOWN),
            cost=CostMetrics(total_cost_usd=0.001, provenance=MetricProvenance.ESTIMATED),
            success=True,
        )
        eff_a, tok_a, cost_a, violations_a = EfficiencyOracle.evaluate(telemetry_unknown_tok, task)
        self.assertFalse(eff_a)
        self.assertFalse(tok_a)
        self.assertTrue(cost_a)
        self.assertTrue(any("tokens ausente ou desconhecida" in v for v in violations_a))

        # Case B: Unknown cost
        telemetry_unknown_cost = CognitiveTelemetryRecord(
            task_id=task.task_id,
            tokens=TokenMetrics(input_tokens=100, output_tokens=50, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=None, provenance=MetricProvenance.UNKNOWN),
            success=True,
        )
        eff_b, tok_b, cost_b, violations_b = EfficiencyOracle.evaluate(telemetry_unknown_cost, task)
        self.assertFalse(eff_b)
        self.assertTrue(tok_b)
        self.assertFalse(cost_b)
        self.assertTrue(any("custo ausente ou desconhecida" in v for v in violations_b))

    def test_10_security_oracle_catches_missing_or_bypassed_database_triggers(self) -> None:
        """Validate that SecurityOracle detects when SQLite immutability triggers are missing."""
        # Create a temporary standalone SQLite database WITHOUT triggers
        raw_db_dir = tempfile.mkdtemp(prefix="test_no_triggers_")
        raw_db_path = f"{raw_db_dir}/unprotected.db"
        conn = sqlite3.connect(raw_db_path)
        try:
            conn.execute(
                """CREATE TABLE cognitive_telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    metadata TEXT,
                    created_at TEXT
                )"""
            )
            cur = conn.execute("INSERT INTO cognitive_telemetry (task_id, metadata, created_at) VALUES ('orig_task', '{}', 'now')")
            row_id = cur.lastrowid
            conn.commit()

            class FakeStore:
                def __init__(self, db):
                    self.db = db

            fake_store = FakeStore(conn)
            telemetry = CognitiveTelemetryRecord(
                id=row_id,
                task_id="orig_task",
                success=True,
            )

            # SecurityOracle must detect that UPDATE succeeded without trigger rejection
            sec_ok, zero_leak, sqlite_ok, violations = SecurityOracle.evaluate(
                actual_output={"clean": True},
                telemetry=telemetry,
                store=fake_store,
            )
            self.assertFalse(sec_ok)
            self.assertTrue(zero_leak)
            self.assertFalse(sqlite_ok)
            self.assertTrue(any("não foi rejeitado pelo trigger" in v for v in violations))

            # Verify that savepoint rollback protected the row from permanent tampering
            check_row = conn.execute("SELECT task_id FROM cognitive_telemetry WHERE id = ?", (row_id,)).fetchone()
            self.assertEqual(check_row[0], "orig_task")
        finally:
            conn.close()

    def test_11_runner_heuristic_provenance_and_unauthenticated_distinction(self) -> None:
        """Validate that unkeyed runners produce integrity_hash but oracle_signature is None."""
        unkeyed_runner = SyntheticBenchmarkRunner(store=self.store, hmac_key=None)
        tasks = SyntheticDatasetGenerator.generate_suite_a(seed=4242, count=1)
        task = tasks[0]

        report = unkeyed_runner.run_task(task, lambda payload: task.expected_output)
        self.assertEqual(report.observed_tokens.provenance, MetricProvenance.ESTIMATED)
        self.assertIsNotNone(report.integrity_hash)
        self.assertEqual(len(report.integrity_hash), 64)
        self.assertIsNone(report.oracle_signature)

    def test_12_security_oracle_returns_none_when_store_not_provided(self) -> None:
        """Validate that SecurityOracle returns None and PARTIAL_UNVERIFIED when store is None."""
        telemetry = CognitiveTelemetryRecord(
            task_id="no_store_task",
            metadata={"phase": "test"},
            success=True,
        )
        sec_ok, zero_leak, sqlite_ok, violations = SecurityOracle.evaluate(
            actual_output={"clean": True},
            telemetry=telemetry,
            store=None,
        )
        self.assertIsNone(sec_ok, "security_success deve ser None (não completamente verificado) quando store for None")
        self.assertTrue(zero_leak)
        self.assertIsNone(sqlite_ok, "sqlite_ok deve ser None (não avaliado) quando store for None")
        self.assertEqual(len(violations), 0)

        # Ensure EvaluationOracleReport assigns PARTIAL_UNVERIFIED and passed_all is False
        report = EvaluationOracleReport(
            task_id="no_store_task",
            suite="suite_a_data_engineering",
            functional_success=True,
            correctness_score=1.0,
            canonical_output_hash="abc",
            expected_output_hash="abc",
            efficiency_success=True,
            budget_tokens_compliant=True,
            budget_cost_compliant=True,
            observed_tokens=telemetry.tokens,
            observed_cost=telemetry.cost,
            observed_latencies=telemetry.latencies,
            security_success=sec_ok,
            sqlite_immutable=sqlite_ok,
        )
        self.assertEqual(report.security_verdict, SecurityVerdict.PARTIAL_UNVERIFIED)
        self.assertFalse(report.passed_all, "passed_all deve ser False quando security_success for None")


if __name__ == "__main__":
    unittest.main()
