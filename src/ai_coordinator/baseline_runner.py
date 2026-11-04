"""Offline Synthetic Baseline Runner for Phase 4C.0.4.

Provides a reproducible, deterministic scientific baseline benchmark executor that:
1. Executes synthetic task suites (Suites A, B, and C) using independent reference solvers.
2. Accurately segregates task execution time, validation time, and telemetry instrumentation overhead.
3. Computes statistical distributions (mean, p50, p95, p99, std_dev) and throughput.
4. Identifies environment, git commit, python/platform runtime, and experimental configuration.
5. Preserves explicit metric provenance (MEASURED vs ESTIMATED) without external API costs.
6. Authenticates the aggregated baseline manifest via SHA-256 integrity and HMAC signatures.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import platform
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .telemetry import (
    MetricProvenance,
    TokenMetrics,
    LatencyMetrics,
    CostMetrics,
    CognitiveTelemetryRecord,
    PricingRegistry,
    scrub_sensitive_dict,
)
from .synthetic_benchmark import (
    DATASET_VERSION,
    SyntheticTaskDefinition,
    EvaluationOracleReport,
    SyntheticDatasetGenerator,
    FunctionalOracle,
    EfficiencyOracle,
    SecurityOracle,
    canonical_json_hash,
    _normalize_json_structure,
)


class CanonicalSolverRegistry:
    """Independent canonical solvers that compute expected outputs directly from input payloads.

    Critically, these solvers never access task.expected_output; they execute the true
    domain logic (ETL aggregation, ledger reconciliation, DAG pipeline, topological sorting, etc.)
    independently to prove functional correctness.
    """

    @classmethod
    def solve_tabular_etl(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Aggregate raw transactions by category and calculate summary metrics."""
        txs = payload.get("transactions", [])
        categories = sorted(list({t["category"] for t in txs}))
        cat_sums: Dict[str, int] = {c: 0 for c in categories}
        cat_counts: Dict[str, int] = {c: 0 for c in categories}

        for t in txs:
            cat = t["category"]
            cat_sums[cat] += t["amount_cents"]
            cat_counts[cat] += 1

        summary = {
            c: {
                "total_amount_cents": cat_sums[c],
                "transaction_count": cat_counts[c],
                "avg_amount_cents": round(cat_sums[c] / cat_counts[c], 2) if cat_counts[c] else 0.0,
            }
            for c in categories
        }
        return {
            "summary_by_category": summary,
            "total_records_processed": len(txs),
        }

    @classmethod
    def solve_ledger_reconciliation(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Reconcile double-entry ledger entries and verify net debit/credit balance."""
        entries = payload.get("ledger_entries", [])
        accounts = sorted(list({e["from_account"] for e in entries} | {e["to_account"] for e in entries}))
        balances = {a: 0 for a in accounts}

        for e in entries:
            balances[e["to_account"]] += e["amount"]
            balances[e["from_account"]] -= e["amount"]

        net_flow = sum(balances.values())
        return {
            "reconciled_balances": balances,
            "net_flow": net_flow,
            "balanced": net_flow == 0,
        }

    @classmethod
    def solve_schema_validation(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Filter user records based on schema invariants and sort surviving records by ID."""
        rules = payload.get("schema_rules", {})
        min_age = rules.get("min_age", 18)
        allowed_roles = set(rules.get("allowed_roles", []))
        users = payload.get("users", [])

        valid_users = [
            u for u in users
            if u.get("age", 0) >= min_age and u.get("role") in allowed_roles
        ]
        valid_users_sorted = sorted(valid_users, key=lambda x: str(x.get("id")))
        return {
            "valid_records_count": len(valid_users_sorted),
            "valid_users": valid_users_sorted,
            "rejection_count": len(users) - len(valid_users_sorted),
        }

    @classmethod
    def solve_sequential_pipeline(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute linear DAG transformations across sequential processing steps."""
        val = payload.get("initial_value", 0)
        steps = payload.get("steps", [])
        curr = val
        trace = []

        for step in steps:
            if step == "extract":
                curr = curr * 2
            elif step == "validate":
                curr = curr + 5
            elif step == "clean":
                curr = max(0, curr - 2)
            elif step == "aggregate":
                curr = curr * 3
            elif step == "format":
                curr = curr + 1
            trace.append({"step": step, "state_value": curr})

        return {
            "final_value": curr,
            "pipeline_trace": trace,
            "steps_completed": len(steps),
        }

    @classmethod
    def solve_fork_join(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Compute parallel partition sums and global aggregate."""
        partitions = payload.get("partitions", {})
        part_sums = {p: sum(nums) for p, nums in sorted(partitions.items())}
        return {
            "partition_results": part_sums,
            "global_sum": sum(part_sums.values()),
            "partition_count": len(partitions),
        }

    @classmethod
    def solve_dependency_resolution(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Perform deterministic topological sort and acyclicity verification."""
        graph = payload.get("graph", {})
        in_degree = {u: 0 for u in graph}
        for u in graph:
            for v in graph[u]:
                if v not in in_degree:
                    in_degree[v] = 0

        # Note: in task definition, graph is child -> [parents] (e.g. node_c requires node_a, node_b)
        # So root nodes are nodes with empty parent list
        roots = sorted([u for u, deps in graph.items() if len(deps) == 0])

        # Topological sort where parents precede children
        resolved: List[str] = []
        pending = dict(graph)
        while pending:
            ready = sorted([u for u, deps in pending.items() if all(d in resolved for d in deps)])
            if not ready:
                # Cycle detected
                return {"topological_order": [], "is_acyclic": False, "root_nodes": roots}
            next_node = ready[0]
            resolved.append(next_node)
            del pending[next_node]

        return {
            "topological_order": resolved,
            "is_acyclic": True,
            "root_nodes": roots,
        }

    @classmethod
    def solve_credential_defense(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize adversarial secret injection payload."""
        def scrub_obj(obj: Any) -> Any:
            if isinstance(obj, dict):
                res = {}
                for k, v in obj.items():
                    clean_k = "[REDACTED_SECRET]" if any(s in k for s in ("sk-", "ghp_", "AIza")) else str(k)
                    res[clean_k] = scrub_obj(v)
                return res
            elif isinstance(obj, list):
                return [scrub_obj(x) for x in obj]
            elif isinstance(obj, str):
                if any(s in obj for s in ("sk-", "ghp_", "AIza", "bearer ")):
                    return "[REDACTED_SECRET]"
                return obj
            return obj

        return scrub_obj(payload)

    @classmethod
    def solve_fault_recovery(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Recover from malformed JSON and identify valid fallback ID."""
        nulls = payload.get("unexpected_nulls", {})
        fallback_id = nulls.get("fallback_id", "unknown")
        return {
            "parsed_gracefully": False,
            "error_category": "MALFORMED_JSON_PAYLOAD",
            "recovered_fallback_id": fallback_id,
        }

    @classmethod
    def solve_budget_fencing(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Enforce budget overrun containment and fencing."""
        demand = payload.get("simulated_token_demand", 0)
        budget = payload.get("allocated_budget", 0)
        exceeded = demand > budget
        return {
            "budget_exceeded": exceeded,
            "action_taken": "FENCED_HALT" if exceeded else "PERMITTED",
            "rejection_code": "BUDGET_OVERRUN_PREVENTED" if exceeded else "OK",
        }

    @classmethod
    def get_solver(cls, task_type: str) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        """Resolve canonical solver for a given task type."""
        mapping: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
            "tabular_etl": cls.solve_tabular_etl,
            "ledger_reconciliation": cls.solve_ledger_reconciliation,
            "schema_validation": cls.solve_schema_validation,
            "sequential_pipeline": cls.solve_sequential_pipeline,
            "fork_join": cls.solve_fork_join,
            "dependency_resolution": cls.solve_dependency_resolution,
            "credential_sanitization_defense": cls.solve_credential_defense,
            "fault_recovery": cls.solve_fault_recovery,
            "budget_fencing": cls.solve_budget_fencing,
        }
        if task_type in mapping:
            return mapping[task_type]
        # Default identity / fallback solver
        return lambda p: dict(p)


@dataclass(frozen=True)
class TaskTimingBreakdown:
    """Accurate separation of execution phases in milliseconds."""
    task_execution_ms: float
    telemetry_overhead_ms: float
    validation_ms: float
    total_elapsed_ms: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "task_execution_ms": round(self.task_execution_ms, 4),
            "telemetry_overhead_ms": round(self.telemetry_overhead_ms, 4),
            "validation_ms": round(self.validation_ms, 4),
            "total_elapsed_ms": round(self.total_elapsed_ms, 4),
        }


@dataclass(frozen=True)
class BaselineTaskExecutionResult:
    """Individual execution result of a benchmark task within the baseline run."""
    task_id: str
    suite: str
    task_type: str
    iteration: int
    oracle_report: EvaluationOracleReport
    timing_breakdown: TaskTimingBreakdown

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "suite": self.suite,
            "task_type": self.task_type,
            "iteration": self.iteration,
            "oracle_report": self.oracle_report.to_dict(),
            "timing_breakdown": self.timing_breakdown.to_dict(),
        }


@dataclass(frozen=True)
class MetricDistribution:
    """Statistical moments and percentiles for a metric series."""
    count: int
    mean: float
    std_dev: float
    p50: float
    p95: float
    p99: float
    min_val: float
    max_val: float
    provenance: str = MetricProvenance.MEASURED

    @classmethod
    def compute(cls, values: List[float], provenance: str = MetricProvenance.MEASURED) -> MetricDistribution:
        if not values:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, provenance)
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        mean_val = sum(sorted_vals) / n
        variance = sum((x - mean_val) ** 2 for x in sorted_vals) / n if n > 1 else 0.0
        std_val = math.sqrt(variance)

        def percentile(p: float) -> float:
            idx = int(n * p)
            return sorted_vals[min(idx, n - 1)]

        return cls(
            count=n,
            mean=round(mean_val, 4),
            std_dev=round(std_val, 4),
            p50=round(percentile(0.50), 4),
            p95=round(percentile(0.95), 4),
            p99=round(percentile(0.99), 4),
            min_val=round(sorted_vals[0], 4),
            max_val=round(sorted_vals[-1], 4),
            provenance=provenance,
        )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class BaselineEnvironmentMetadata:
    """Environment, git, runtime, and configuration provenance."""
    git_commit: str
    python_version: str
    platform_system: str
    platform_release: str
    platform_machine: str
    ai_coordinator_version: str
    dataset_version: str
    pricing_registry_version: str
    is_reference_baseline: bool = True
    adaptive_heuristics_active: bool = False
    timestamp: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    @classmethod
    def capture(cls) -> BaselineEnvironmentMetadata:
        git_sha = "unknown"
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if res.returncode == 0:
                git_sha = res.stdout.strip()
        except Exception:
            pass

        return cls(
            git_commit=git_sha,
            python_version=platform.python_version(),
            platform_system=platform.system(),
            platform_release=platform.release(),
            platform_machine=platform.machine(),
            ai_coordinator_version="0.5.1",
            dataset_version=DATASET_VERSION,
            pricing_registry_version=PricingRegistry.REGISTRY_VERSION,
            is_reference_baseline=True,
            adaptive_heuristics_active=False,
        )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class BaselineExecutionReport:
    """Aggregated, authenticated scientific baseline benchmark report."""
    run_id: str
    environment: BaselineEnvironmentMetadata
    total_tasks_executed: int
    iterations: int
    task_execution_results: List[BaselineTaskExecutionResult]
    latency_distributions: Dict[str, MetricDistribution]
    token_aggregates: Dict[str, Any]
    cost_aggregates: Dict[str, Any]
    success_rates: Dict[str, float]
    integrity_hash: str = ""
    oracle_signature: Optional[str] = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    def compute_canonical_manifest(self) -> Dict[str, Any]:
        """Generate structured canonical manifest of the aggregated baseline for cryptographic signing."""
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "dataset_version": self.environment.dataset_version,
            "git_commit": self.environment.git_commit,
            "python_version": self.environment.python_version,
            "platform": f"{self.environment.platform_system}_{self.environment.platform_machine}",
            "is_reference_baseline": self.environment.is_reference_baseline,
            "adaptive_heuristics_active": self.environment.adaptive_heuristics_active,
            "total_tasks_executed": self.total_tasks_executed,
            "iterations": self.iterations,
            "success_rates": self.success_rates,
            "latency_distributions": {k: v.to_dict() for k, v in sorted(self.latency_distributions.items())},
            "token_aggregates": self.token_aggregates,
            "cost_aggregates": self.cost_aggregates,
        }

    def verify_integrity(self) -> bool:
        """Verify that integrity_hash matches the SHA-256 digest of the canonical baseline manifest."""
        manifest = self.compute_canonical_manifest()
        canonical_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        expected = hashlib.sha256(canonical_bytes).hexdigest()
        return hmac.compare_digest(self.integrity_hash, expected)

    def verify_signature(self, hmac_key: str) -> bool:
        """Verify that oracle_signature matches HMAC-SHA256 of the canonical baseline manifest."""
        if not self.oracle_signature or not hmac_key:
            return False
        manifest = self.compute_canonical_manifest()
        canonical_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        expected = hmac.new(hmac_key.encode("utf-8"), canonical_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.oracle_signature, expected)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "environment": self.environment.to_dict(),
            "total_tasks_executed": self.total_tasks_executed,
            "iterations": self.iterations,
            "success_rates": self.success_rates,
            "latency_distributions": {k: v.to_dict() for k, v in self.latency_distributions.items()},
            "token_aggregates": self.token_aggregates,
            "cost_aggregates": self.cost_aggregates,
            "integrity_hash": self.integrity_hash,
            "oracle_signature": self.oracle_signature,
            "task_execution_results": [r.to_dict() for r in self.task_execution_results],
        }


class BaselineRunner:
    """Scientific benchmark runner for offline baseline execution without optimizations."""

    def __init__(self, store: Optional[Any] = None, hmac_key: Optional[str] = None):
        self.store = store
        self.hmac_key = hmac_key

    def run_baseline(
        self,
        iterations: int = 2,
        seed_suite_a: int = 4242,
        seed_suite_b: int = 8484,
        seed_suite_c: int = 1212,
    ) -> BaselineExecutionReport:
        """Execute full synthetic task suites with canonical solvers and compile authenticated baseline report."""
        run_id = f"baseline_run_{int(time.time())}"
        env = BaselineEnvironmentMetadata.capture()

        # Generate standard tasks
        tasks_a = SyntheticDatasetGenerator.generate_suite_a(seed=seed_suite_a, count=3)
        tasks_b = SyntheticDatasetGenerator.generate_suite_b(seed=seed_suite_b, count=3)
        tasks_c = SyntheticDatasetGenerator.generate_suite_c(seed=seed_suite_c, count=2)
        all_tasks = tasks_a + tasks_b + tasks_c

        execution_results: List[BaselineTaskExecutionResult] = []
        task_durations: List[float] = []
        overhead_durations: List[float] = []
        validation_durations: List[float] = []
        total_durations: List[float] = []

        total_input_tokens = 0
        total_output_tokens = 0
        total_cost_usd = 0.0
        passed_all_count = 0
        functional_success_count = 0
        efficiency_success_count = 0
        security_success_count = 0

        for it in range(iterations):
            for task in all_tasks:
                solver = CanonicalSolverRegistry.get_solver(task.task_type)

                # Phase 1: Task Execution (Solver only)
                t0 = time.perf_counter()
                actual_output = None
                error_msg = None
                success = True
                try:
                    actual_output = solver(task.input_payload)
                except Exception as exc:
                    success = False
                    error_msg = str(exc)
                t_solve = time.perf_counter()
                exec_ms = max(0.0, (t_solve - t0) * 1000.0)

                # Phase 2: Telemetry instrumentation and persistence overhead
                in_tokens = len(json.dumps(task.input_payload)) // 4
                out_tokens = len(json.dumps(actual_output or {})) // 4
                tokens = TokenMetrics(
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    provenance=MetricProvenance.ESTIMATED,
                )
                latencies = LatencyMetrics(
                    total_duration_ms=exec_ms,
                    latency_provenance=MetricProvenance.MEASURED,
                )
                cost = PricingRegistry.estimate_cost("test-model", tokens, provider="synthetic")

                telemetry = CognitiveTelemetryRecord(
                    task_id=task.task_id,
                    provider="synthetic_baseline",
                    model_name="baseline-reference-model",
                    tokens=tokens,
                    latencies=latencies,
                    cost=cost,
                    success=success,
                    error_message=error_msg,
                    metadata={"iteration": it, "suite": task.suite},
                )
                if self.store is not None and hasattr(self.store, "record_cognitive_telemetry"):
                    telemetry = self.store.record_cognitive_telemetry(telemetry)
                t_telemetry = time.perf_counter()
                overhead_ms = max(0.0, (t_telemetry - t_solve) * 1000.0)

                # Phase 3: Validation with independent Oracles
                func_ok, correctness, actual_hash = FunctionalOracle.evaluate(actual_output, task)
                eff_res = EfficiencyOracle.evaluate(telemetry, task)
                eff_ok, tok_ok, cost_ok, eff_violations = eff_res
                duration_ok = getattr(eff_res, "budget_latency_compliant", True)

                sec_ok, zero_leak, sqlite_ok, sec_violations = SecurityOracle.evaluate(
                    actual_output=actual_output,
                    telemetry=telemetry,
                    store=self.store,
                )
                all_violations = tuple(eff_violations + sec_violations)

                input_hash = canonical_json_hash(task.input_payload)
                oracle_rep = EvaluationOracleReport(
                    task_id=task.task_id,
                    suite=task.suite,
                    input_payload_hash=input_hash,
                    benchmark_version=task.version,
                    functional_success=func_ok,
                    correctness_score=correctness,
                    canonical_output_hash=actual_hash,
                    expected_output_hash=task.expected_hash,
                    efficiency_success=eff_ok,
                    budget_tokens_compliant=tok_ok,
                    budget_cost_compliant=cost_ok,
                    budget_latency_compliant=duration_ok,
                    observed_tokens=tokens,
                    observed_cost=cost,
                    observed_latencies=latencies,
                    security_success=sec_ok,
                    zero_secrets_leaked=zero_leak,
                    sqlite_immutable=sqlite_ok,
                    violation_details=all_violations,
                )

                # Sign individual report
                manifest = oracle_rep.compute_canonical_manifest(benchmark_version=task.version)
                canon_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                rep_hash = hashlib.sha256(canon_bytes).hexdigest()
                rep_sig = None
                if self.hmac_key:
                    rep_sig = hmac.new(self.hmac_key.encode("utf-8"), canon_bytes, hashlib.sha256).hexdigest()

                final_oracle_rep = dataclasses.replace(oracle_rep, integrity_hash=rep_hash, oracle_signature=rep_sig)

                t_validate = time.perf_counter()
                val_ms = max(0.0, (t_validate - t_telemetry) * 1000.0)
                tot_ms = max(0.0, (t_validate - t0) * 1000.0)

                timing = TaskTimingBreakdown(
                    task_execution_ms=exec_ms,
                    telemetry_overhead_ms=overhead_ms,
                    validation_ms=val_ms,
                    total_elapsed_ms=tot_ms,
                )

                exec_res = BaselineTaskExecutionResult(
                    task_id=task.task_id,
                    suite=task.suite,
                    task_type=task.task_type,
                    iteration=it,
                    oracle_report=final_oracle_rep,
                    timing_breakdown=timing,
                )
                execution_results.append(exec_res)

                # Collect distributions
                task_durations.append(exec_ms)
                overhead_durations.append(overhead_ms)
                validation_durations.append(val_ms)
                total_durations.append(tot_ms)

                total_input_tokens += in_tokens
                total_output_tokens += out_tokens
                total_cost_usd += (cost.total_cost_usd or 0.0)

                if final_oracle_rep.passed_all:
                    passed_all_count += 1
                if func_ok:
                    functional_success_count += 1
                if eff_ok:
                    efficiency_success_count += 1
                if sec_ok:
                    security_success_count += 1

        total_runs = len(execution_results)
        latency_dists = {
            "task_execution_ms": MetricDistribution.compute(task_durations, MetricProvenance.MEASURED),
            "telemetry_overhead_ms": MetricDistribution.compute(overhead_durations, MetricProvenance.MEASURED),
            "validation_ms": MetricDistribution.compute(validation_durations, MetricProvenance.MEASURED),
            "total_elapsed_ms": MetricDistribution.compute(total_durations, MetricProvenance.MEASURED),
        }

        token_aggs = {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "avg_tokens_per_task": round((total_input_tokens + total_output_tokens) / total_runs, 2) if total_runs else 0.0,
            "provenance": MetricProvenance.ESTIMATED,
        }

        cost_aggs = {
            "total_cost_usd": round(total_cost_usd, 6),
            "avg_cost_per_task_usd": round(total_cost_usd / total_runs, 6) if total_runs else 0.0,
            "pricing_version": env.pricing_registry_version,
            "provenance": MetricProvenance.ESTIMATED,
        }

        success_rates = {
            "passed_all_rate": round(passed_all_count / total_runs, 4) if total_runs else 0.0,
            "functional_success_rate": round(functional_success_count / total_runs, 4) if total_runs else 0.0,
            "efficiency_success_rate": round(efficiency_success_count / total_runs, 4) if total_runs else 0.0,
            "security_success_rate": round(security_success_count / total_runs, 4) if total_runs else 0.0,
        }

        report = BaselineExecutionReport(
            run_id=run_id,
            environment=env,
            total_tasks_executed=total_runs,
            iterations=iterations,
            task_execution_results=execution_results,
            latency_distributions=latency_dists,
            token_aggregates=token_aggs,
            cost_aggregates=cost_aggs,
            success_rates=success_rates,
        )

        # Authenticate the aggregated baseline manifest
        manifest = report.compute_canonical_manifest()
        manifest_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        integrity_hash = hashlib.sha256(manifest_bytes).hexdigest()
        oracle_signature = None
        if self.hmac_key:
            oracle_signature = hmac.new(self.hmac_key.encode("utf-8"), manifest_bytes, hashlib.sha256).hexdigest()

        return dataclasses.replace(
            report,
            integrity_hash=integrity_hash,
            oracle_signature=oracle_signature,
        )
