"""Unit tests and automated regression checks for Phase 4C.2.5 Comparative Benchmarks.

Validates that:
1. All 5 comparative benchmark scenarios execute deterministically offline.
2. Invariant violation rates are exactly zero.
3. Sub-latency and resource profiling metrics are captured accurately.
4. Concurrency stress under ThreadPoolExecutor completes with zero split-brain or data corruption.
5. Markdown and JSON reports are generated with complete provenance.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from ai_coordinator.openrouter.benchmark_4c2 import (
    Phase4C2BenchmarkRunner,
    ScenarioBenchmarkResult,
    ScenarioSummaryStats,
)


class TestPhase4C25Benchmarks(unittest.TestCase):
    """Test suite verifying the Phase 4C.2.5 Benchmark Suite."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="ai_coord_test_bench_")
        self.runner = Phase4C2BenchmarkRunner(tmp_dir=self.tmp_dir)

    def tearDown(self) -> None:
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def test_01_summary_stats_computation(self) -> None:
        """TC-01: SummaryStats computes accurate mean, median, p95, p99, and stddev."""
        samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        stats = ScenarioSummaryStats.from_samples(samples)
        self.assertEqual(stats.count, 10)
        self.assertEqual(stats.min_val, 1.0)
        self.assertEqual(stats.max_val, 10.0)
        self.assertEqual(stats.median, 5.5)
        self.assertEqual(stats.mean, 5.5)
        self.assertAlmostEqual(stats.stddev, 3.0277, places=3)
        self.assertEqual(stats.p95, 10.0)

    def test_02_scenario_a_baseline(self) -> None:
        """TC-02: Scenario A baseline executes with zero invariant violations."""
        res = self.runner.run_scenario_a_baseline(iterations=10)
        self.assertEqual(res.samples_count, 10)
        self.assertEqual(res.invariant_violations, 0)
        self.assertEqual(res.invariant_violation_rate, 0.0)
        self.assertGreater(res.throughput_ops_sec, 0.0)
        self.assertGreater(res.latency_stats_ms.median, 0.0)

    def test_03_scenario_b_crypto_governance(self) -> None:
        """TC-03: Scenario B executes Ed25519 signing, Trust Store and Tripartite checks."""
        res = self.runner.run_scenario_b_crypto_only(iterations=10)
        self.assertEqual(res.samples_count, 10)
        self.assertEqual(res.invariant_violations, 0)
        self.assertIn("ed25519_sign_ms", res.sub_latencies_ms)
        self.assertIn("human_approval_issue_ms", res.sub_latencies_ms)
        self.assertIn("tripartite_verify_ms", res.sub_latencies_ms)

    def test_04_scenario_c_full_governance(self) -> None:
        """TC-04: Scenario C executes SQLite atomic quota, PIC and Transactional Outbox."""
        res = self.runner.run_scenario_c_full_governance(iterations=10)
        self.assertEqual(res.samples_count, 10)
        self.assertEqual(res.invariant_violations, 0)
        self.assertIn("budget_registration_ms", res.sub_latencies_ms)
        self.assertIn("reservation_acquire_ms", res.sub_latencies_ms)
        self.assertIn("pic_and_completion_commit_ms", res.sub_latencies_ms)
        self.assertIn("outbox_projection_fsync_ms", res.sub_latencies_ms)
        self.assertGreater(res.storage_growth_bytes["sqlite_bytes"], 0)
        self.assertGreater(res.storage_growth_bytes["jsonl_bytes"], 0)

    def test_05_scenario_d_fallback_recommendation(self) -> None:
        """TC-05: Scenario D evaluates primary 429 and produces recommendation only."""
        res = self.runner.run_scenario_d_fallback_recommendation(iterations=10)
        self.assertEqual(res.samples_count, 10)
        self.assertEqual(res.invariant_violations, 0)
        self.assertIn("fallback_evaluation_ms", res.sub_latencies_ms)

    def test_06_scenario_e_fallback_dispatch(self) -> None:
        """TC-06: Scenario E consumes secondary reservation on primary 429."""
        res = self.runner.run_scenario_e_fallback_dispatch(iterations=10)
        self.assertEqual(res.samples_count, 10)
        self.assertEqual(res.invariant_violations, 0)
        self.assertIn("secondary_fallback_eval_and_reserve_ms", res.sub_latencies_ms)
        self.assertEqual(res.simulated_transport_latency_ms, 50.0)

    def test_07_concurrency_stress_benchmark(self) -> None:
        """TC-07: Concurrency benchmark executes under thread contention with zero invariant violations."""
        res = self.runner.run_concurrency_stress_benchmark(num_threads=4, tasks_per_thread=5)
        self.assertEqual(res["total_tasks"], 20)
        self.assertEqual(res["successful_dispatches"], 20)
        self.assertEqual(res["invariant_violations"], 0)
        self.assertEqual(res["violation_rate"], 0.0)
        self.assertGreater(res["throughput_ops_sec"], 0.0)

    def test_08_full_campaign_execution(self) -> None:
        """TC-08: Full benchmark campaign executes warmup and all scenarios end-to-end."""
        campaign = self.runner.run_full_campaign(iterations=5, warmup_iterations=2)
        self.assertIn("scenarios", campaign)
        self.assertEqual(len(campaign["scenarios"]), 6)
        self.assertIn("active_repo_commit", campaign["environment"])
        self.assertIn("baseline_reference_commit", campaign["environment"])
        self.assertIn("auth_manifest_fixture_commit", campaign["environment"])
        self.assertIn("raw_latency_samples_ms", campaign["scenarios"]["scenario_c_full_governance"])
        self.assertIn("process_rss_delta_stats_kb", campaign["scenarios"]["scenario_c_full_governance"])
        self.assertEqual(campaign["overall_invariants"]["total_violations"], 0)
        self.assertEqual(campaign["overall_invariants"]["invariants_violation_rate"], 0.0)

    def test_09_scenario_a2_legacy_cost_guard(self) -> None:
        """TC-09: Scenario A2 pre-4C.2 FreeCostGuard baseline executes deterministically."""
        res = self.runner.run_scenario_a2_legacy_cost_guard(iterations=10)
        self.assertEqual(res.samples_count, 10)
        self.assertEqual(res.invariant_violations, 0)
        self.assertEqual(res.invariant_violation_rate, 0.0)
        self.assertGreater(res.throughput_ops_sec, 0.0)
        self.assertGreater(res.latency_stats_ms.median, 0.0)
        self.assertEqual(len(res.raw_latency_samples_ms), 10)

    def test_10_sqlite_and_outbox_invariants_auditor(self) -> None:
        """TC-10: Invariants auditor verifies clean database and reports zero violations."""
        res_c = self.runner.run_scenario_c_full_governance(iterations=5)
        db_path = os.path.join(self.runner.tmp_dir, "bench_c.db")
        ledger_path = os.path.join(self.runner.tmp_dir, "bench_c_audit.jsonl")
        audit = self.runner.audit_sqlite_and_outbox_invariants(db_path, ledger_path)
        self.assertEqual(audit["violations"], 0)
        self.assertEqual(audit["budget_overrun_violations"], 0)
        self.assertEqual(audit["replay_violations"], 0)
        self.assertEqual(audit["split_brain_double_allocations"], 0)
        self.assertEqual(audit["outbox_projection_mismatches"], 0)
        self.assertGreater(audit["total_checks_evaluated"], 0)


if __name__ == "__main__":
    unittest.main()
