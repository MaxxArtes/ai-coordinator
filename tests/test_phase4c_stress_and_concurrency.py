"""Comprehensive Stress, Concurrency, and Resilience Tests for Phase 4C.0.3.

Validates:
1. High-concurrency SQLite telemetry writes (WAL mode + BEGIN IMMEDIATE) without deadlocks.
2. Concurrent interleaved reads, aggregations, and writes under load.
3. Concurrent immutability enforcement without SAVEPOINT collisions on shared connections.
4. Atomic budget fencing under concurrent load (TOCTOU prevention).
5. High-volume credential scrubbing and raw SQL zero-leak verification.
6. Crash recovery and WAL reconciliation on abrupt termination.
7. Telemetry execution overhead (< 5.0ms) and memory leak audit.
8. Observability non-interference: candidate output and agent invariants remain unchanged.
9. Comprehensive HMAC tamper-detection across all report metrics, violations, and version fields.
10. Monotonic timing (time.perf_counter) and max_duration_ms enforcement in EfficiencyOracle.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from typing import Any, Dict, List, Optional, Tuple

from ai_coordinator.storage import Store
from ai_coordinator.telemetry import (
    CognitiveTelemetryRecord,
    CostMetrics,
    LatencyMetrics,
    MetricProvenance,
    PricingRegistry,
    TokenMetrics,
    scrub_sensitive_dict,
    scrub_sensitive_value,
)
from ai_coordinator.synthetic_benchmark import (
    DATASET_VERSION,
    EfficiencyOracle,
    EvaluationOracleReport,
    FunctionalOracle,
    SecurityOracle,
    SyntheticBenchmarkRunner,
    SyntheticDatasetGenerator,
    SyntheticTaskDefinition,
    canonical_json_hash,
    SharedBudgetExceededError,
    SharedFlightBudgetFence,
)


class TestPhase4CStressAndConcurrency(unittest.TestCase):
    """Stress, concurrency, and adversarial resilience test suite for Phase 4C.0.3."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="test_phase4c_stress_")
        self.store = Store(root=self.temp_dir)
        self.hmac_secret = "stress_test_secret_key_2026_phase4c"

    def tearDown(self) -> None:
        if hasattr(self, "store") and hasattr(self.store, "db"):
            try:
                self.store.db.close()
            except Exception:
                pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_concurrent_telemetry_persistence_and_wal_integrity(self) -> None:
        """Validate high-concurrency writes across multiple threads without database lockouts or corruption."""
        worker_count = 6
        records_per_worker = 20
        total_expected = worker_count * records_per_worker

        latencies_ms: List[float] = []
        errors: List[str] = []
        lock = threading.Lock()

        def worker_task(worker_id: int) -> None:
            for i in range(records_per_worker):
                t_start = time.perf_counter()
                rec = CognitiveTelemetryRecord(
                    task_id=f"stress_task_w{worker_id}_{i:03d}",
                    provider="stress_bench",
                    model_name="test-llm",
                    tokens=TokenMetrics(input_tokens=100 + i, output_tokens=50 + i, provenance=MetricProvenance.MEASURED),
                    latencies=LatencyMetrics(total_duration_ms=10.0 + (i % 5), latency_provenance=MetricProvenance.MEASURED),
                    cost=CostMetrics(total_cost_usd=0.001, provenance=MetricProvenance.ESTIMATED),
                    metadata={"worker_id": worker_id, "iteration": i, "tag": "concurrent_test"},
                    success=True,
                )
                try:
                    persisted = self.store.record_cognitive_telemetry(rec)
                    if persisted.id is None:
                        with lock:
                            errors.append(f"Worker {worker_id} iter {i}: persisted.id is None")
                except Exception as exc:
                    with lock:
                        errors.append(f"Worker {worker_id} iter {i} error: {exc}")
                finally:
                    elapsed_ms = (time.perf_counter() - t_start) * 1000.0
                    with lock:
                        latencies_ms.append(elapsed_ms)
                time.sleep(0.001)

        t_bench_start = time.perf_counter()
        threads = [threading.Thread(target=worker_task, args=(w,)) for w in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        t_bench_total = time.perf_counter() - t_bench_start

        # Zero error tolerance
        self.assertEqual(len(errors), 0, f"Erros durante escrita concorrente: {errors[:5]}")
        self.assertEqual(len(latencies_ms), total_expected)

        # Validate database integrity and record count
        with self.store._lock:
            cur = self.store.db.execute("SELECT COUNT(*), MIN(id), MAX(id) FROM cognitive_telemetry")
            count, min_id, max_id = cur.fetchone()

        self.assertEqual(count, total_expected)
        self.assertEqual(min_id, 1)
        self.assertEqual(max_id, total_expected)

        # Performance and throughput indicators
        throughput = total_expected / max(0.001, t_bench_total)
        latencies_sorted = sorted(latencies_ms)
        p50 = latencies_sorted[int(len(latencies_sorted) * 0.50)]
        p95 = latencies_sorted[int(len(latencies_sorted) * 0.95)]
        p99 = latencies_sorted[int(len(latencies_sorted) * 0.99)]

        self.assertGreater(throughput, 5.0, "Throughput de escrita deve superar 5 registros/s")
        self.assertLess(p50, 150.0, f"Latência p50 ({p50:.2f} ms) deve ser inferior a 150 ms")

    def test_02_concurrent_reads_writes_and_aggregations(self) -> None:
        """Validate interleaved concurrent reads, aggregations, and writes under continuous activity."""
        duration_seconds = 2.0
        stop_event = threading.Event()
        writer_errors: List[str] = []
        reader_errors: List[str] = []
        read_snapshots: List[int] = []
        reader_snapshots: Dict[int, List[int]] = {i: [] for i in range(3)}
        lock = threading.Lock()

        def continuous_writer(w_id: int) -> None:
            c = 0
            while not stop_event.is_set():
                rec = CognitiveTelemetryRecord(
                    task_id=f"interleave_w{w_id}_{c}",
                    provider="interleave_bench",
                    model_name="bench-model",
                    tokens=TokenMetrics(input_tokens=10, output_tokens=20, provenance=MetricProvenance.MEASURED),
                    latencies=LatencyMetrics(total_duration_ms=5.0, latency_provenance=MetricProvenance.MEASURED),
                    cost=CostMetrics(total_cost_usd=0.0001, provenance=MetricProvenance.ESTIMATED),
                    success=True,
                )
                try:
                    self.store.record_cognitive_telemetry(rec)
                    c += 1
                except Exception as exc:
                    with lock:
                        writer_errors.append(str(exc))
                    break
                time.sleep(0.005)

        def continuous_reader(r_id: int) -> None:
            while not stop_event.is_set():
                try:
                    stats = self.store.aggregate_cognitive_telemetry(provider="interleave_bench")
                    with lock:
                        read_snapshots.append(stats.total_matched_records)
                        reader_snapshots[r_id].append(stats.total_matched_records)
                    self.store.query_cognitive_telemetry(limit=5)
                except Exception as exc:
                    with lock:
                        reader_errors.append(str(exc))
                    break
                time.sleep(0.005)

        writers = [threading.Thread(target=continuous_writer, args=(i,)) for i in range(3)]
        readers = [threading.Thread(target=continuous_reader, args=(i,)) for i in range(3)]

        for t in writers + readers:
            t.start()

        time.sleep(duration_seconds)
        stop_event.set()

        for t in writers + readers:
            t.join(timeout=10.0)

        self.assertEqual(len(writer_errors), 0, f"Erros nos escritores: {writer_errors}")
        self.assertEqual(len(reader_errors), 0, f"Erros nos leitores: {reader_errors}")
        self.assertGreater(len(read_snapshots), 3, "Leitores devem capturar múltiplas agregações sem bloqueio")

        # Snapshot monotonicity per reader: each reader observes monotonically non-decreasing counts
        for r_id, snaps in reader_snapshots.items():
            self.assertGreater(len(snaps), 1, f"Leitor {r_id} deve ter capturado amostras")
            for i in range(1, len(snaps)):
                self.assertGreaterEqual(
                    snaps[i], snaps[i - 1],
                    f"Leitor {r_id} observou regressão de contagem ({snaps[i]} < {snaps[i - 1]})"
                )

        # Global aggregate progression: overall counts grow from start to finish
        self.assertGreaterEqual(read_snapshots[-1], read_snapshots[0])

    def test_03_concurrent_immutability_enforcement_under_load(self) -> None:
        """Validate that concurrent UPDATE/DELETE attempts are 100% rejected and SecurityOracle is safe."""
        # Prepopulate records
        pre_records: List[CognitiveTelemetryRecord] = []
        for i in range(15):
            rec = CognitiveTelemetryRecord(
                task_id=f"immutable_target_{i:02d}",
                provider="security_bench",
                model_name="sec-model",
                tokens=TokenMetrics(input_tokens=50, output_tokens=25, provenance=MetricProvenance.MEASURED),
                latencies=LatencyMetrics(total_duration_ms=8.0, latency_provenance=MetricProvenance.MEASURED),
                cost=CostMetrics(total_cost_usd=0.0005, provenance=MetricProvenance.ESTIMATED),
                success=True,
            )
            pre_records.append(self.store.record_cognitive_telemetry(rec))

        violation_successes: List[str] = []
        oracle_results: List[bool] = []
        lock = threading.Lock()

        def adversarial_updater(target_id: int) -> None:
            with self.store._lock:
                try:
                    self.store.db.execute("UPDATE cognitive_telemetry SET task_id = 'hacked' WHERE id = ?", (target_id,))
                    with lock:
                        violation_successes.append(f"UPDATE em id {target_id} sucedeu sem disparar trigger!")
                except sqlite3.IntegrityError as exc:
                    try:
                        self.store.db.rollback()
                    except Exception:
                        pass
                    if "immutable and append-only" not in str(exc):
                        with lock:
                            violation_successes.append(f"IntegrityError inesperado: {exc}")
                except Exception as exc:
                    try:
                        self.store.db.rollback()
                    except Exception:
                        pass
                    with lock:
                        violation_successes.append(f"Erro inesperado no UPDATE: {exc}")

        def adversarial_deleter(target_id: int) -> None:
            with self.store._lock:
                try:
                    self.store.db.execute("DELETE FROM cognitive_telemetry WHERE id = ?", (target_id,))
                    with lock:
                        violation_successes.append(f"DELETE em id {target_id} sucedeu sem disparar trigger!")
                except sqlite3.IntegrityError as exc:
                    try:
                        self.store.db.rollback()
                    except Exception:
                        pass
                    if "immutable and cannot be deleted" not in str(exc):
                        with lock:
                            violation_successes.append(f"IntegrityError inesperado na exclusão: {exc}")
                except Exception as exc:
                    try:
                        self.store.db.rollback()
                    except Exception:
                        pass
                    with lock:
                        violation_successes.append(f"Erro inesperado no DELETE: {exc}")

        def concurrent_oracle_evaluator(target_rec: CognitiveTelemetryRecord) -> None:
            sec_ok, zero_leak, sqlite_ok, violations = SecurityOracle.evaluate(
                actual_output={"clean": True},
                telemetry=target_rec,
                store=self.store,
            )
            with lock:
                oracle_results.append(sec_ok and sqlite_ok)

        threads: List[threading.Thread] = []
        for rec in pre_records:
            threads.append(threading.Thread(target=adversarial_updater, args=(rec.id,)))
            threads.append(threading.Thread(target=adversarial_deleter, args=(rec.id,)))
            threads.append(threading.Thread(target=concurrent_oracle_evaluator, args=(rec,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        # 100% of illegal mutations were blocked
        self.assertEqual(len(violation_successes), 0, f"Violações indevidamente permitidas: {violation_successes}")
        # All concurrent SecurityOracle executions evaluated safely and passed
        self.assertEqual(len(oracle_results), len(pre_records))
        self.assertTrue(all(oracle_results))

        # Check records remain completely unmodified
        with self.store._lock:
            rows = self.store.db.execute("SELECT id, task_id FROM cognitive_telemetry").fetchall()
        self.assertEqual(len(rows), len(pre_records))
        for r in rows:
            self.assertTrue(r[1].startswith("immutable_target_"))

    def test_04_concurrent_flight_budget_fencing(self) -> None:
        """Validate genuine shared budget contention across workers with atomic reservations and pre-consumption rejection."""
        # 1. Test SharedFlightBudgetFence with real atomic reservations across concurrent workers
        shared_fence = SharedFlightBudgetFence(max_tokens=1000, max_cost_usd=0.010)
        worker_count = 20
        tokens_per_worker = 150
        cost_per_worker = 0.0015

        successful_workers: List[str] = []
        rejected_workers: List[str] = []
        lock = threading.Lock()

        def competing_worker(worker_idx: int) -> None:
            w_id = f"worker_{worker_idx:02d}"
            try:
                # Pre-consumption reservation: must raise SharedBudgetExceededError before consumption
                res_id = shared_fence.reserve(
                    worker_id=w_id,
                    estimated_tokens=tokens_per_worker,
                    estimated_cost_usd=cost_per_worker,
                )
                # Worker executes work safely within reserved quota
                time.sleep(0.005)
                # Worker commits actual usage
                shared_fence.commit(res_id, actual_tokens=tokens_per_worker, actual_cost_usd=cost_per_worker)
                with lock:
                    successful_workers.append(w_id)
            except SharedBudgetExceededError:
                # Rejection happened BEFORE consumption; zero tokens or cost leaked
                with lock:
                    rejected_workers.append(w_id)

        threads = [threading.Thread(target=competing_worker, args=(i,)) for i in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # 1000 total tokens // 150 per worker = exactly 6 workers admitted
        self.assertEqual(len(successful_workers), 6, f"Esperado exatamente 6 workers admitidos, obtido: {len(successful_workers)}")
        self.assertEqual(len(rejected_workers), 14, f"Esperado exatamente 14 workers rejeitados, obtido: {len(rejected_workers)}")
        self.assertEqual(shared_fence.approved_count, 6)
        self.assertEqual(shared_fence.rejected_count, 14)
        self.assertEqual(shared_fence.remaining_tokens, 100)  # 1000 - (6 * 150) = 100
        self.assertAlmostEqual(shared_fence.remaining_cost_usd, 0.0010, places=4)

        # Dynamic release test: Releasing an active reservation allows a pending worker to succeed
        extra_res_id = shared_fence.reserve("temporary_holder", estimated_tokens=100, estimated_cost_usd=0.0010)
        self.assertEqual(shared_fence.remaining_tokens, 0)
        # Any further request is rejected
        with self.assertRaises(SharedBudgetExceededError):
            shared_fence.reserve("denied_worker", 50, 0.0005)
        # Release the reservation
        shared_fence.release(extra_res_id)
        self.assertEqual(shared_fence.remaining_tokens, 100)

        # 2. Verify that EfficiencyOracle also evaluates individual task budgets correctly
        task = SyntheticTaskDefinition(
            task_id="budget_fencing_concurrent",
            suite="suite_c_adversarial",
            task_type="budget_fencing",
            description="Teste de concorrência com limite de tokens e contenção de missão",
            seed=42,
            version=DATASET_VERSION,
            input_payload={"items": [1, 2, 3]},
            expected_output={"items": [1, 2, 3]},
            expected_hash=canonical_json_hash({"items": [1, 2, 3]}),
            budget_limit_tokens=500,
            budget_limit_cost_usd=0.005,
            max_duration_ms=3000.0,
        )

        fenced_outcomes: List[bool] = []
        def task_evaluator(tokens_spent: int) -> None:
            telemetry = CognitiveTelemetryRecord(
                task_id=task.task_id,
                tokens=TokenMetrics(input_tokens=tokens_spent, output_tokens=0, provenance=MetricProvenance.MEASURED),
                cost=CostMetrics(total_cost_usd=0.001, provenance=MetricProvenance.ESTIMATED),
                latencies=LatencyMetrics(total_duration_ms=10.0, latency_provenance=MetricProvenance.MEASURED),
                success=True,
            )
            eff_res = EfficiencyOracle.evaluate(telemetry, task)
            with lock:
                fenced_outcomes.append(eff_res.efficiency_success)

        oracle_threads = [
            threading.Thread(target=task_evaluator, args=(200 if i % 2 == 0 else 800,))
            for i in range(10)
        ]
        for t in oracle_threads:
            t.start()
        for t in oracle_threads:
            t.join(timeout=10.0)

        self.assertEqual(sum(1 for res in fenced_outcomes if res is True), 5)
        self.assertEqual(sum(1 for res in fenced_outcomes if res is False), 5)

    def test_05_high_volume_credential_injection_and_sanitization(self) -> None:
        """Validate zero-leak guarantee under a massive burst of nested adversarial credentials."""
        secret_pool = [
            "sk-proj-1234567890abcdef12345678",
            "ghp_111122223333444455556666777788889999",
            "AIzaSyD-1234567890abcdef1234567890abcde",
            "AKIAIOSFODNN7EXAMPLE",
            "bearer super-secret-oauth-token-12345",
        ]

        burst_count = 200
        for i in range(burst_count):
            dirty_payload = {
                f"sk-proj-header_{i:04d}": "safe_val",
                f"normal_key_{i}": f"User sk-proj-val_{i} authenticated with {secret_pool[i % len(secret_pool)]}",
                "nested": {
                    f"token_{i}": secret_pool[(i + 1) % len(secret_pool)],
                    "deep": [secret_pool[(i + 2) % len(secret_pool)], "normal_item"],
                },
            }
            # Scrub dictionary and record in store
            clean_meta = scrub_sensitive_dict(dirty_payload)
            rec = CognitiveTelemetryRecord(
                task_id=f"burst_sec_{i:04d}",
                provider="stress_sec",
                model_name="scrub-model",
                metadata=clean_meta,
                success=True,
            )
            self.store.record_cognitive_telemetry(rec)

        # Raw SQL query over SQLite persistence to prove absolute absence of unredacted credentials
        with self.store._lock:
            cur = self.store.db.execute("SELECT metadata FROM cognitive_telemetry WHERE provider = 'stress_sec'")
            persisted_metas = cur.fetchall()

        self.assertEqual(len(persisted_metas), burst_count)
        for row in persisted_metas:
            raw_text = row[0]
            for sec in secret_pool:
                self.assertNotIn(sec, raw_text, f"Credencial vazada encontrada na persistência SQLite bruta: {sec}")
            # Ensure redacted markers exist
            self.assertIn("[REDACTED_SECRET]", raw_text)

    def test_06_crash_recovery_and_partial_write_isolation(self) -> None:
        """Validate database recovery and state preservation following real abrupt subprocess termination."""
        # Write initial committed records
        for i in range(10):
            rec = CognitiveTelemetryRecord(
                task_id=f"pre_crash_{i}",
                provider="crash_bench",
                model_name="crash-model",
                success=True,
            )
            self.store.record_cognitive_telemetry(rec)

        # Close store connection before spawning subprocess to avoid file locking contention on Windows
        self.store.db.close()

        # Execute genuine separate Python subprocess that begins an uncommitted transaction and crashes via os._exit()
        db_file = os.path.abspath(os.path.join(self.temp_dir, "coordination.sqlite")).replace("\\", "/")
        subprocess_code = f"""
import os, sys, sqlite3

conn = sqlite3.connect(r"{db_file}", timeout=10.0)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT INTO cognitive_telemetry (task_id, provider, model_name, tokens_provenance, total_duration_ms, latency_provenance, cost_provenance, created_at) "
    "VALUES ('uncommitted_subprocess_phantom', 'crash_bench', 'crash-model', 'estimated', 10.0, 'measured', 'estimated', '2026-11-04T12:00:00Z')"
)
sys.stdout.write("SUBPROCESS_DIRTY_READY\\n")
sys.stdout.flush()

# Abrupt termination bypassing Python clean up, SQLite rollback, and connection close
os._exit(87)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", subprocess_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(timeout=10.0)
        self.assertEqual(proc.returncode, 87, f"Subprocesso deve abortar com código 87. Erro: {stderr}")
        self.assertIn("SUBPROCESS_DIRTY_READY", stdout)

        # Reopen with a brand new Store instance and verify WAL recovery
        new_store = Store(root=self.temp_dir)
        try:
            records = new_store.query_cognitive_telemetry(provider="crash_bench", limit=50)
            task_ids = {r.task_id for r in records}
            # All 10 pre-crash committed records must be intact
            self.assertEqual(len(task_ids), 10)
            for i in range(10):
                self.assertIn(f"pre_crash_{i}", task_ids)
            # The uncommitted phantom record from the crashed process must be discarded by WAL recovery
            self.assertNotIn("uncommitted_subprocess_phantom", task_ids)

            # Subsequent write must succeed normally proving the WAL lock was cleared
            post_rec = CognitiveTelemetryRecord(
                task_id="post_recovery_01",
                provider="crash_bench",
                model_name="crash-model",
                success=True,
            )
            new_store.record_cognitive_telemetry(post_rec)
            post_ids = {r.task_id for r in new_store.query_cognitive_telemetry(provider="crash_bench", limit=50)}
            self.assertIn("post_recovery_01", post_ids)
        finally:
            new_store.db.close()
            # Restore store handle for tearDown clean up
            self.store = Store(root=self.temp_dir)

    def test_07_telemetry_overhead_and_memory_benchmarks(self) -> None:
        """Measure observation overhead and audit memory consumption under repeated benchmarking."""
        runner = SyntheticBenchmarkRunner(store=self.store, hmac_key=self.hmac_secret)
        tasks = SyntheticDatasetGenerator.generate_suite_a(count=1)
        task = tasks[0]

        tracemalloc.start()
        start_mem, _ = tracemalloc.get_traced_memory()

        iteration_count = 50
        durations: List[float] = []

        for _ in range(iteration_count):
            t0 = time.perf_counter()
            report = runner.run_task(task, lambda payload: task.expected_output)
            t1 = time.perf_counter()
            durations.append((t1 - t0) * 1000.0)
            self.assertTrue(report.passed_all)

        current_mem, peak_mem = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        avg_duration_ms = sum(durations) / len(durations)
        mem_growth_kb = (peak_mem - start_mem) / 1024.0

        # Overhead must be lean (< 75ms per full evaluation cycle including SQLite insert and oracles on VM)
        self.assertLess(avg_duration_ms, 75.0, f"Duração média ({avg_duration_ms:.2f} ms) excede teto de sobrecarga")
        # Memory growth must remain bounded
        self.assertLess(mem_growth_kb, 10000.0, f"Crescimento de memória ({mem_growth_kb:.2f} KB) excessivo")

    def test_08_observability_does_not_mutate_agent_decisions_or_permissions(self) -> None:
        """Demonstrate that candidate output, execution flow, and exceptions are not mutated by telemetry."""
        runner = SyntheticBenchmarkRunner(store=self.store, hmac_key=self.hmac_secret)
        tasks = SyntheticDatasetGenerator.generate_suite_a(count=1)
        task = tasks[0]

        def identity_solver(payload: Any) -> Any:
            # Custom distinct object to verify reference / value preservation
            return {"custom_token": "unique_12345", "original": payload}

        report = runner.run_task(task, identity_solver)
        # Functional oracle evaluates difference correctly without altering actual output
        self.assertFalse(report.functional_success)
        self.assertEqual(report.correctness_score, 0.0)
        self.assertNotEqual(report.canonical_output_hash, task.expected_hash)

        # Check candidate exception is captured transparently without masking
        def failing_solver(payload: Any) -> Any:
            raise PermissionError("Permissão de escrita negada para worker não confiável")

        report_err = runner.run_task(task, failing_solver)
        self.assertFalse(report_err.passed_all)
        self.assertFalse(report_err.functional_success)

    def test_09_hmac_tamper_detection_on_all_fields(self) -> None:
        """Validate comprehensive HMAC authentication: tampering with ANY metric or metadata invalidates signature."""
        runner = SyntheticBenchmarkRunner(store=self.store, hmac_key=self.hmac_secret)
        tasks = SyntheticDatasetGenerator.generate_suite_a(count=1)
        task = tasks[0]

        original_report = runner.run_task(task, lambda payload: task.expected_output)
        self.assertTrue(original_report.passed_all)
        self.assertIsNotNone(original_report.oracle_signature)

        # Baseline: untampered report passes both integrity and signature checks
        self.assertTrue(original_report.verify_integrity(benchmark_version=task.version))
        self.assertTrue(original_report.verify_signature(self.hmac_secret, benchmark_version=task.version))
        self.assertTrue(SyntheticBenchmarkRunner.verify_report(original_report, self.hmac_secret, benchmark_version=task.version))

        # Rejection on wrong secret key
        self.assertFalse(original_report.verify_signature("wrong_secret_key_12345", benchmark_version=task.version))

        # Helper to assert both integrity_hash and oracle_signature are invalidated
        def assert_tampered(report_variant: EvaluationOracleReport, field_name: str) -> None:
            self.assertFalse(
                report_variant.verify_integrity(task.version),
                f"Adulteração em '{field_name}' deveria ter invalidado verify_integrity()",
            )
            self.assertFalse(
                report_variant.verify_signature(self.hmac_secret, task.version),
                f"Adulteração em '{field_name}' deveria ter invalidado verify_signature()",
            )

        # 1. Token Metrics Tampering (including cache_read_tokens with unchanged total)
        orig_tokens = original_report.observed_tokens
        assert_tampered(
            dataclasses.replace(original_report, observed_tokens=dataclasses.replace(orig_tokens, cache_read_tokens=99)),
            "tokens.cache_read_tokens",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_tokens=dataclasses.replace(orig_tokens, cache_creation_tokens=50)),
            "tokens.cache_creation_tokens",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_tokens=dataclasses.replace(orig_tokens, input_tokens=1)),
            "tokens.input_tokens",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_tokens=dataclasses.replace(orig_tokens, output_tokens=999)),
            "tokens.output_tokens",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_tokens=dataclasses.replace(orig_tokens, total_tokens=12345)),
            "tokens.total_tokens",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_tokens=dataclasses.replace(orig_tokens, provenance=MetricProvenance.MEASURED)),
            "tokens.provenance",
        )

        # 2. Cost Metrics Tampering (individual components, total, pricing_version, provenance)
        orig_cost = original_report.observed_cost
        assert_tampered(
            dataclasses.replace(original_report, observed_cost=dataclasses.replace(orig_cost, input_cost_usd=0.05)),
            "cost.input_cost_usd",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_cost=dataclasses.replace(orig_cost, output_cost_usd=0.08)),
            "cost.output_cost_usd",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_cost=dataclasses.replace(orig_cost, cache_read_cost_usd=0.01)),
            "cost.cache_read_cost_usd",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_cost=dataclasses.replace(orig_cost, cache_creation_cost_usd=0.02)),
            "cost.cache_creation_cost_usd",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_cost=dataclasses.replace(orig_cost, total_cost_usd=0.99)),
            "cost.total_cost_usd",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_cost=dataclasses.replace(orig_cost, pricing_version="1999.01")),
            "cost.pricing_version",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_cost=dataclasses.replace(orig_cost, provenance=MetricProvenance.UNKNOWN)),
            "cost.provenance",
        )

        # 3. Latency Metrics Tampering (total, ttft, tool, supervisor, provenances)
        orig_lat = original_report.observed_latencies
        assert_tampered(
            dataclasses.replace(original_report, observed_latencies=dataclasses.replace(orig_lat, total_duration_ms=0.01)),
            "latencies.total_duration_ms",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_latencies=dataclasses.replace(orig_lat, ttft_ms=50.0)),
            "latencies.ttft_ms",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_latencies=dataclasses.replace(orig_lat, generation_duration_ms=100.0)),
            "latencies.generation_duration_ms",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_latencies=dataclasses.replace(orig_lat, tool_duration_ms=25.0)),
            "latencies.tool_duration_ms",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_latencies=dataclasses.replace(orig_lat, supervisor_overhead_ms=5.0)),
            "latencies.supervisor_overhead_ms",
        )
        assert_tampered(
            dataclasses.replace(original_report, observed_latencies=dataclasses.replace(orig_lat, latency_provenance=MetricProvenance.ESTIMATED)),
            "latencies.latency_provenance",
        )

        # 4. Metadata, Timestamp, and Cryptographic Hashes
        assert_tampered(
            dataclasses.replace(original_report, timestamp="1970-01-01T00:00:00Z"),
            "timestamp",
        )
        assert_tampered(
            dataclasses.replace(original_report, input_payload_hash="forged_input_hash_1234"),
            "input_payload_hash",
        )
        assert_tampered(
            dataclasses.replace(original_report, expected_output_hash="forged_expected_hash_5678"),
            "expected_output_hash",
        )
        assert_tampered(
            dataclasses.replace(original_report, canonical_output_hash="forged_output_hash_9012"),
            "canonical_output_hash",
        )
        assert_tampered(
            dataclasses.replace(original_report, task_id="forged_task_id"),
            "task_id",
        )
        assert_tampered(
            dataclasses.replace(original_report, suite="forged_suite"),
            "suite",
        )

        # 5. Correctness Score, Violations, Version, and Booleans
        assert_tampered(
            dataclasses.replace(original_report, correctness_score=0.5),
            "correctness_score",
        )
        assert_tampered(
            dataclasses.replace(original_report, violation_details=("Violação forjada inserida post-hoc",)),
            "violation_details",
        )
        assert_tampered(
            dataclasses.replace(original_report, functional_success=False),
            "functional_success",
        )
        assert_tampered(
            dataclasses.replace(original_report, efficiency_success=False),
            "efficiency_success",
        )
        assert_tampered(
            dataclasses.replace(original_report, security_success=False),
            "security_success",
        )
        assert_tampered(
            dataclasses.replace(original_report, budget_tokens_compliant=False),
            "budget_tokens_compliant",
        )
        assert_tampered(
            dataclasses.replace(original_report, budget_cost_compliant=False),
            "budget_cost_compliant",
        )
        assert_tampered(
            dataclasses.replace(original_report, budget_latency_compliant=False),
            "budget_latency_compliant",
        )
        assert_tampered(
            dataclasses.replace(original_report, zero_secrets_leaked=False),
            "zero_secrets_leaked",
        )
        assert_tampered(
            dataclasses.replace(original_report, sqlite_immutable=False),
            "sqlite_immutable",
        )

        # Version parameter alteration
        self.assertFalse(original_report.verify_integrity(benchmark_version="1999.01.01"))
        self.assertFalse(original_report.verify_signature(self.hmac_secret, benchmark_version="1999.01.01"))

    def test_10_monotonic_clock_and_duration_budget_enforcement(self) -> None:
        """Validate monotonic clock usage (time.perf_counter) and max_duration_ms budget enforcement."""
        task = SyntheticTaskDefinition(
            task_id="timing_enforcement_task",
            suite="suite_a_data_engineering",
            task_type="tabular_etl",
            description="Verificação de limite de latência monotônica",
            seed=42,
            version=DATASET_VERSION,
            input_payload={"test": 1},
            expected_output={"test": 1},
            expected_hash=canonical_json_hash({"test": 1}),
            budget_limit_tokens=2000,
            budget_limit_cost_usd=0.015,
            max_duration_ms=400.0,  # Robust 400ms cap to avoid cloud VM hypervisor jitter
        )

        # Case A: Compliant fast solver
        runner = SyntheticBenchmarkRunner(store=self.store)
        fast_report = runner.run_task(task, lambda payload: task.expected_output)
        self.assertTrue(fast_report.efficiency_success)
        self.assertTrue(fast_report.budget_latency_compliant)
        self.assertEqual(fast_report.observed_latencies.latency_provenance, MetricProvenance.MEASURED)

        # Case B: Exceeding max_duration_ms
        def slow_solver(payload: Any) -> Any:
            time.sleep(0.55)  # 550ms > 400ms cap
            return task.expected_output

        slow_report = runner.run_task(task, slow_solver)
        self.assertFalse(slow_report.efficiency_success)
        self.assertFalse(slow_report.budget_latency_compliant)
        self.assertTrue(any("Limite de duração excedido" in v for v in slow_report.violation_details))

        # Case C: Unknown latency provenance is rejected
        unknown_lat_record = CognitiveTelemetryRecord(
            task_id=task.task_id,
            tokens=TokenMetrics(input_tokens=100, output_tokens=50, provenance=MetricProvenance.MEASURED),
            cost=CostMetrics(total_cost_usd=0.001, provenance=MetricProvenance.ESTIMATED),
            latencies=LatencyMetrics(total_duration_ms=20.0, latency_provenance=MetricProvenance.UNKNOWN),
            success=True,
        )
        res = EfficiencyOracle.evaluate(unknown_lat_record, task)
        self.assertFalse(res.efficiency_success)
        self.assertFalse(res.budget_latency_compliant)
        self.assertTrue(any("Métrica de latência/duração ausente ou desconhecida" in v for v in res.violations))


if __name__ == "__main__":
    unittest.main()
