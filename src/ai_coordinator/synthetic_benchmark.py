"""Synthetic Datasets and Evaluation Oracles for Adaptive Cognitive Optimization (Phase 4C.0.2).

Provides 100% offline, deterministic, reproducible benchmark suites (Suites A, B, and C)
and tri-dimensional evaluation oracles (Functional, Efficiency, Security) for scientific
cognitive baseline experimentation without external API costs or credential exposure.
"""
from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import hmac
import json
import math
import random
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .telemetry import (
    MetricProvenance,
    TokenMetrics,
    LatencyMetrics,
    CostMetrics,
    CognitiveTelemetryRecord,
    PricingRegistry,
    scrub_sensitive_dict,
    scrub_sensitive_text,
    scrub_sensitive_value,
)

DATASET_VERSION = "2026.11.03"
DEFAULT_SEED_SUITE_A = 4242
DEFAULT_SEED_SUITE_B = 8484
DEFAULT_SEED_SUITE_C = 1212

_SENSITIVE_LEAK_CHECK_PATTERNS = [
    re.compile(r"""\b(sk-[A-Za-z0-9_\-]{20,})\b""", re.IGNORECASE),
    re.compile(r"""\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b""", re.IGNORECASE),
    re.compile(r"""\b(AIza[0-9A-Za-z\-_]{35})\b"""),
    re.compile(r"""\b(AKIA[0-9A-Z]{16})\b"""),
    re.compile(r"""\b(xox[baprs]-[0-9A-Za-z\-]{10,})\b"""),
    re.compile(r"""(bearer\s+)[A-Za-z0-9_\-\.]{15,}""", re.IGNORECASE),
]


def _normalize_json_structure(data: Any) -> Any:
    """Normalize data structure for deterministic JSON serialization without scrubbing semantic content."""
    if data is None or isinstance(data, (str, int, bool)):
        return data
    if isinstance(data, float):
        return None if (math.isnan(data) or math.isinf(data)) else round(data, 8)
    if isinstance(data, dict):
        return {str(k): _normalize_json_structure(v) for k, v in sorted(data.items(), key=lambda kv: str(kv[0]))}
    if isinstance(data, (list, tuple, set, frozenset)):
        if isinstance(data, (set, frozenset)):
            try:
                items = sorted(list(data), key=lambda x: str(x))
            except Exception:
                items = list(data)
        else:
            items = list(data)
        return [_normalize_json_structure(x) for x in items]
    return str(data)


class SecurityVerdict:
    """Explicit categorization of security oracle evaluations."""
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL_UNVERIFIED = "PARTIAL_UNVERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


def canonical_json_hash(data: Any) -> str:
    """Compute deterministic SHA-256 hash of a normalized JSON-serializable structure.

    Preserves exact semantic output (including any unredacted secrets) so that functional
    oracles compare real candidate responses rather than masking violations through premature redaction.
    """
    normalized = _normalize_json_structure(data)
    canonical_bytes = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


@dataclass(frozen=True)
class SyntheticTaskDefinition:
    """Immutable specification of a single deterministic benchmark task."""
    task_id: str
    suite: str
    task_type: str
    description: str
    seed: int
    version: str
    input_payload: Dict[str, Any]
    expected_output: Dict[str, Any]
    expected_hash: str
    budget_limit_tokens: int
    budget_limit_cost_usd: float
    max_duration_ms: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _safe_round(val: Optional[float], decimals: int = 4) -> Optional[float]:
    """Safely round floating point metrics handling None, NaN, and Inf without throwing."""
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, decimals)
    except Exception:
        return None


@dataclass(frozen=True)
class EvaluationOracleReport:
    """Immutable tri-dimensional evaluation report produced by offline benchmark oracles."""
    task_id: str
    suite: str
    functional_success: bool
    correctness_score: float
    canonical_output_hash: str
    expected_output_hash: str
    efficiency_success: bool
    budget_tokens_compliant: bool
    budget_cost_compliant: bool
    observed_tokens: TokenMetrics
    observed_cost: CostMetrics
    observed_latencies: LatencyMetrics
    security_success: Optional[bool] = None
    zero_secrets_leaked: bool = True
    sqlite_immutable: Optional[bool] = None
    violation_details: Tuple[str, ...] = field(default_factory=tuple)
    integrity_hash: str = ""
    oracle_signature: Optional[str] = None
    timestamp: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    budget_latency_compliant: bool = True
    input_payload_hash: str = ""
    benchmark_version: str = DATASET_VERSION
    security_verdict: str = ""

    def __post_init__(self) -> None:
        # Determine strict security verdict based on evaluated security checks if not explicitly specified
        if not self.security_verdict:
            if self.security_success is False or not self.zero_secrets_leaked or self.sqlite_immutable is False or len(self.violation_details) > 0:
                object.__setattr__(self, "security_verdict", SecurityVerdict.FAIL)
                if self.security_success is None:
                    object.__setattr__(self, "security_success", False)
            elif self.sqlite_immutable is True and self.zero_secrets_leaked and len(self.violation_details) == 0:
                object.__setattr__(self, "security_verdict", SecurityVerdict.PASS)
                if self.security_success is None:
                    object.__setattr__(self, "security_success", True)
            elif self.sqlite_immutable is None and self.zero_secrets_leaked and len(self.violation_details) == 0:
                object.__setattr__(self, "security_verdict", SecurityVerdict.PARTIAL_UNVERIFIED)
                if self.security_success is not None:
                    object.__setattr__(self, "security_success", None)
            else:
                object.__setattr__(self, "security_verdict", SecurityVerdict.UNKNOWN)

    @property
    def passed_all(self) -> bool:
        return self.functional_success and self.efficiency_success and (self.security_success is True)

    def compute_canonical_manifest(self, benchmark_version: Optional[str] = None) -> Dict[str, Any]:
        """Build canonical structured manifest of all evaluated report fields for authentication."""
        ver = benchmark_version if benchmark_version is not None else self.benchmark_version
        return {
            "version": ver,
            "task_id": self.task_id,
            "suite": self.suite,
            "timestamp": self.timestamp,
            "input_payload_hash": self.input_payload_hash,
            "expected_output_hash": self.expected_output_hash,
            "canonical_output_hash": self.canonical_output_hash,
            "passed_all": self.passed_all,
            "functional_success": self.functional_success,
            "correctness_score": round(self.correctness_score, 4),
            "efficiency_success": self.efficiency_success,
            "budget_tokens_compliant": self.budget_tokens_compliant,
            "budget_cost_compliant": self.budget_cost_compliant,
            "budget_latency_compliant": self.budget_latency_compliant,
            "observed_tokens": {
                "input_tokens": self.observed_tokens.input_tokens,
                "output_tokens": self.observed_tokens.output_tokens,
                "cache_read_tokens": self.observed_tokens.cache_read_tokens,
                "cache_creation_tokens": self.observed_tokens.cache_creation_tokens,
                "total_tokens": self.observed_tokens.total_tokens,
                "provenance": self.observed_tokens.provenance,
            },
            "observed_cost": {
                "input_cost_usd": _safe_round(self.observed_cost.input_cost_usd, 6),
                "output_cost_usd": _safe_round(self.observed_cost.output_cost_usd, 6),
                "cache_read_cost_usd": _safe_round(self.observed_cost.cache_read_cost_usd, 6),
                "cache_creation_cost_usd": _safe_round(self.observed_cost.cache_creation_cost_usd, 6),
                "total_cost_usd": _safe_round(self.observed_cost.total_cost_usd, 6),
                "pricing_version": self.observed_cost.pricing_version,
                "provenance": self.observed_cost.provenance,
            },
            "observed_latencies": {
                "total_duration_ms": round(self.observed_latencies.total_duration_ms, 4),
                "ttft_ms": _safe_round(self.observed_latencies.ttft_ms, 4),
                "generation_duration_ms": _safe_round(self.observed_latencies.generation_duration_ms, 4),
                "tool_duration_ms": _safe_round(self.observed_latencies.tool_duration_ms, 4),
                "supervisor_overhead_ms": _safe_round(self.observed_latencies.supervisor_overhead_ms, 4),
                "latency_provenance": self.observed_latencies.latency_provenance,
                "generation_duration_provenance": self.observed_latencies.generation_duration_provenance,
            },
            "security_success": self.security_success,
            "security_verdict": self.security_verdict,
            "zero_secrets_leaked": self.zero_secrets_leaked,
            "sqlite_immutable": self.sqlite_immutable,
            "violation_details": list(sorted(self.violation_details)),
        }

    def verify_integrity(self, benchmark_version: Optional[str] = None) -> bool:
        """Verify that integrity_hash strictly matches the canonical digest of all manifest fields."""
        manifest = self.compute_canonical_manifest(benchmark_version)
        canonical_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        expected_hash = hashlib.sha256(canonical_bytes).hexdigest()
        return hmac.compare_digest(self.integrity_hash, expected_hash)

    def verify_signature(self, hmac_key: str, benchmark_version: Optional[str] = None) -> bool:
        """Verify that oracle_signature is authentic for all report fields under the provided secret key."""
        if not self.oracle_signature or not hmac_key:
            return False
        manifest = self.compute_canonical_manifest(benchmark_version)
        canonical_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        expected_sig = hmac.new(hmac_key.encode("utf-8"), canonical_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.oracle_signature, expected_sig)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "suite": self.suite,
            "passed_all": self.passed_all,
            "functional_success": self.functional_success,
            "correctness_score": self.correctness_score,
            "canonical_output_hash": self.canonical_output_hash,
            "expected_output_hash": self.expected_output_hash,
            "input_payload_hash": self.input_payload_hash,
            "benchmark_version": self.benchmark_version,
            "efficiency_success": self.efficiency_success,
            "budget_tokens_compliant": self.budget_tokens_compliant,
            "budget_cost_compliant": self.budget_cost_compliant,
            "budget_latency_compliant": self.budget_latency_compliant,
            "observed_tokens": self.observed_tokens.to_dict(),
            "observed_cost": self.observed_cost.to_dict(),
            "observed_latencies": self.observed_latencies.to_dict(),
            "security_success": self.security_success,
            "zero_secrets_leaked": self.zero_secrets_leaked,
            "sqlite_immutable": self.sqlite_immutable,
            "violation_details": list(self.violation_details),
            "integrity_hash": self.integrity_hash,
            "oracle_signature": self.oracle_signature,
            "timestamp": self.timestamp,
        }


class SyntheticDatasetGenerator:
    """Deterministic procedural generator for cognitive benchmark suites (A, B, and C).

    All outputs are bit-for-bit identical across any operating system when run with identical seeds.
    """

    @classmethod
    def generate_suite_a(cls, seed: int = DEFAULT_SEED_SUITE_A, count: int = 3) -> List[SyntheticTaskDefinition]:
        """Generate Suite A: Structured Data Engineering (ETL, Reconciliation, Invariants)."""
        rng = random.Random(seed)
        tasks: List[SyntheticTaskDefinition] = []

        # Task A1: Tabular ETL & Aggregation
        categories = ["compute", "storage", "networking", "database"]
        raw_txs = []
        cat_sums: Dict[str, int] = {c: 0 for c in categories}
        cat_counts: Dict[str, int] = {c: 0 for c in categories}

        for i in range(20):
            cat = rng.choice(categories)
            cents = rng.randint(100, 50000)
            raw_txs.append({
                "tx_id": f"tx_{rng.randint(10000, 99999)}",
                "category": cat,
                "amount_cents": cents,
                "status": "cleared" if rng.random() > 0.1 else "pending",
            })
            cat_sums[cat] += cents
            cat_counts[cat] += 1

        expected_a1 = {
            "summary_by_category": {
                c: {
                    "total_amount_cents": cat_sums[c],
                    "transaction_count": cat_counts[c],
                    "avg_amount_cents": round(cat_sums[c] / cat_counts[c], 2) if cat_counts[c] else 0.0,
                }
                for c in sorted(categories)
            },
            "total_records_processed": 20,
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_a_01_etl_aggregation",
            suite="suite_a_data_engineering",
            task_type="tabular_etl",
            description="Agregação analítica e sumarização de transações brutas por categoria de serviço",
            seed=seed,
            version=DATASET_VERSION,
            input_payload={"transactions": raw_txs},
            expected_output=expected_a1,
            expected_hash=canonical_json_hash(expected_a1),
            budget_limit_tokens=2500,
            budget_limit_cost_usd=0.015,
            max_duration_ms=5000.0,
        ))

        # Task A2: Ledger Reconciliation (Double-Entry Bookkeeping)
        accounts = [f"acc_{j:03d}" for j in range(1, 6)]
        entries = []
        balances = {a: 0 for a in accounts}
        for _ in range(25):
            from_acc = rng.choice(accounts)
            to_acc = rng.choice([a for a in accounts if a != from_acc])
            amt = rng.randint(50, 10000)
            entries.append({"from_account": from_acc, "to_account": to_acc, "amount": amt})
            balances[to_acc] += amt
            balances[from_acc] -= amt

        expected_a2 = {
            "reconciled_balances": {a: balances[a] for a in sorted(accounts)},
            "net_flow": sum(balances.values()),
            "balanced": sum(balances.values()) == 0,
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_a_02_reconciliation",
            suite="suite_a_data_engineering",
            task_type="ledger_reconciliation",
            description="Reconciliação estrita de débitos e créditos com cálculo de saldo líquido e prova contábil",
            seed=seed,
            version=DATASET_VERSION,
            input_payload={"ledger_entries": entries},
            expected_output=expected_a2,
            expected_hash=canonical_json_hash(expected_a2),
            budget_limit_tokens=3000,
            budget_limit_cost_usd=0.020,
            max_duration_ms=5000.0,
        ))

        # Task A3: Schema Validation and Filtering
        schema_rules = {"min_age": 18, "allowed_roles": ["admin", "analyst", "operator"]}
        users_raw = [
            {"id": f"u_{k}", "age": rng.randint(15, 60), "role": rng.choice(["admin", "analyst", "guest", "operator"])}
            for k in range(15)
        ]
        valid_users = [
            u for u in users_raw
            if u["age"] >= schema_rules["min_age"] and u["role"] in schema_rules["allowed_roles"]
        ]
        valid_users_sorted = sorted(valid_users, key=lambda x: str(x["id"]))

        expected_a3 = {
            "valid_records_count": len(valid_users_sorted),
            "valid_users": valid_users_sorted,
            "rejection_count": len(users_raw) - len(valid_users_sorted),
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_a_03_schema_invariants",
            suite="suite_a_data_engineering",
            task_type="schema_validation",
            description="Validação de invariantes de esquema com filtragem estrita de registros violadores",
            seed=seed,
            version=DATASET_VERSION,
            input_payload={"schema_rules": schema_rules, "users": users_raw},
            expected_output=expected_a3,
            expected_hash=canonical_json_hash(expected_a3),
            budget_limit_tokens=2000,
            budget_limit_cost_usd=0.010,
            max_duration_ms=4000.0,
        ))

        return tasks[:count]

    @classmethod
    def generate_suite_b(cls, seed: int = DEFAULT_SEED_SUITE_B, count: int = 3) -> List[SyntheticTaskDefinition]:
        """Generate Suite B: Multi-Hop Reasoning & DAG Precedence (Pipelines, Fork-Join)."""
        rng = random.Random(seed)
        tasks: List[SyntheticTaskDefinition] = []

        # Task B1: 5-step Sequential Pipeline
        steps = ["extract", "validate", "clean", "aggregate", "format"]
        initial_val = rng.randint(10, 100)
        curr = initial_val
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

        expected_b1 = {
            "final_value": curr,
            "pipeline_trace": trace,
            "steps_completed": len(steps),
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_b_01_sequential_dag_5_step",
            suite="suite_b_multi_hop_dag",
            task_type="sequential_pipeline",
            description="Pipeline de 5 etapas com dependências lineares de dados e verificação de integridade de estado",
            seed=seed,
            version=DATASET_VERSION,
            input_payload={"initial_value": initial_val, "steps": steps},
            expected_output=expected_b1,
            expected_hash=canonical_json_hash(expected_b1),
            budget_limit_tokens=3500,
            budget_limit_cost_usd=0.025,
            max_duration_ms=6000.0,
        ))

        # Task B2: Fork-Join Parallel Partitions
        partition_count = 4
        partition_data = {
            f"part_{p}": [rng.randint(1, 50) for _ in range(5)]
            for p in range(partition_count)
        }
        part_sums = {p: sum(nums) for p, nums in partition_data.items()}
        expected_b2 = {
            "partition_results": {p: part_sums[p] for p in sorted(part_sums.keys())},
            "global_sum": sum(part_sums.values()),
            "partition_count": partition_count,
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_b_02_fork_join_parallel",
            suite="suite_b_multi_hop_dag",
            task_type="fork_join",
            description="Processamento concorrente particionado com barreira de sincronização e agregação final",
            seed=seed,
            version=DATASET_VERSION,
            input_payload={"partitions": partition_data},
            expected_output=expected_b2,
            expected_hash=canonical_json_hash(expected_b2),
            budget_limit_tokens=4000,
            budget_limit_cost_usd=0.030,
            max_duration_ms=6000.0,
        ))

        # Task B3: Dependency Resolution & State Fencing
        dependencies = {
            "node_c": ["node_a", "node_b"],
            "node_a": [],
            "node_b": ["node_a"],
        }
        execution_order = ["node_a", "node_b", "node_c"]
        expected_b3 = {
            "topological_order": execution_order,
            "is_acyclic": True,
            "root_nodes": ["node_a"],
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_b_03_dependency_fencing",
            suite="suite_b_multi_hop_dag",
            task_type="dependency_resolution",
            description="Ordenação topológica determinística e fencings de precedência com validação de ciclos",
            seed=seed,
            version=DATASET_VERSION,
            input_payload={"graph": dependencies},
            expected_output=expected_b3,
            expected_hash=canonical_json_hash(expected_b3),
            budget_limit_tokens=2500,
            budget_limit_cost_usd=0.015,
            max_duration_ms=4000.0,
        ))

        return tasks[:count]

    @classmethod
    def generate_suite_c(cls, seed: int = DEFAULT_SEED_SUITE_C, count: int = 3) -> List[SyntheticTaskDefinition]:
        """Generate Suite C: Adversarial Robustness & Fault Injection (Credential Leaks, Malformed Data)."""
        tasks: List[SyntheticTaskDefinition] = []

        # Task C1: Adversarial Secret Leaks in Keys and Values
        dirty_input = {
            "sk-proj-1234567890abcdef12345678": "secret_in_key_val",
            "normal_header": "safe_metadata",
            "nested_config": {
                "ghp_111122223333444455556666777788889999": "nested_secret_key",
                "auth_token": "bearer secret-raw-token-1234567890",
            },
            "credentials_list": [
                "AIzaSyD-1234567890abcdef1234567890abcde",
                "normal_string_item",
            ],
        }

        # Expected sanitized structure
        clean_expected = {
            "[REDACTED_SECRET]": "secret_in_key_val",
            "normal_header": "safe_metadata",
            "nested_config": {
                "[REDACTED_SECRET]": "nested_secret_key",
                "auth_token": "[REDACTED_SECRET]",
            },
            "credentials_list": [
                "[REDACTED_SECRET]",
                "normal_string_item",
            ],
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_c_01_adversarial_secret_injection",
            suite="suite_c_adversarial",
            task_type="credential_sanitization_defense",
            description="Injeção proposital de credenciais em chaves, valores e listas para auditar expurgo zero-leak",
            seed=seed,
            version=DATASET_VERSION,
            input_payload=dirty_input,
            expected_output=clean_expected,
            expected_hash=canonical_json_hash(clean_expected),
            budget_limit_tokens=2000,
            budget_limit_cost_usd=0.015,
            max_duration_ms=4000.0,
            metadata={"adversarial": True, "target_invariant": "zero_secrets_leaked"},
        ))

        # Task C2: Malformed Payload Recovery & Classification
        malformed_input = {
            "corrupted_raw_text": '{"missing_bracket": [1, 2, 3',
            "unexpected_nulls": {"mandatory_id": None, "fallback_id": "item_99"},
        }
        clean_recovery = {
            "parsed_gracefully": False,
            "error_category": "MALFORMED_JSON_PAYLOAD",
            "recovered_fallback_id": "item_99",
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_c_02_malformed_payload_recovery",
            suite="suite_c_adversarial",
            task_type="fault_recovery",
            description="Recuperação de falhas e classificação de erro estruturado em payloads JSON truncados",
            seed=seed,
            version=DATASET_VERSION,
            input_payload=malformed_input,
            expected_output=clean_recovery,
            expected_hash=canonical_json_hash(clean_recovery),
            budget_limit_tokens=2000,
            budget_limit_cost_usd=0.015,
            max_duration_ms=4000.0,
            metadata={"adversarial": True, "target_invariant": "non_blocking_resilience"},
        ))

        # Task C3: Budget Overrun Fencing
        overrun_input = {
            "simulated_token_demand": 15000,
            "allocated_budget": 3000,
        }
        expected_c3 = {
            "budget_exceeded": True,
            "action_taken": "FENCED_HALT",
            "rejection_code": "BUDGET_OVERRUN_PREVENTED",
        }

        tasks.append(SyntheticTaskDefinition(
            task_id="suite_c_03_budget_overrun_fencing",
            suite="suite_c_adversarial",
            task_type="budget_fencing",
            description="Interrupção imediata de execução e contenção de missão por violação de orçamento simulada",
            seed=seed,
            version=DATASET_VERSION,
            input_payload=overrun_input,
            expected_output=expected_c3,
            expected_hash=canonical_json_hash(expected_c3),
            budget_limit_tokens=3000,
            budget_limit_cost_usd=0.020,
            max_duration_ms=4000.0,
            metadata={"adversarial": True, "target_invariant": "budget_fencing"},
        ))

        return tasks[:count]


class FunctionalOracle:
    """Oracle assessing schema conformity, exact normalized matching, and canonical SHA-256 output hashes."""

    @classmethod
    def evaluate(cls, actual_output: Any, expected_task: SyntheticTaskDefinition) -> Tuple[bool, float, str]:
        """Evaluate functional correctness against expected benchmark definition.

        Returns (functional_success, correctness_score, actual_hash).
        """
        if actual_output is None:
            return False, 0.0, ""

        actual_hash = canonical_json_hash(actual_output)
        if actual_hash == expected_task.expected_hash:
            return True, 1.0, actual_hash

        # Schema structure validation
        if isinstance(actual_output, dict) and isinstance(expected_task.expected_output, dict):
            expected_keys = set(expected_task.expected_output.keys())
            actual_keys = set(actual_output.keys())
            common_keys = expected_keys.intersection(actual_keys)
            if expected_keys:
                key_score = len(common_keys) / len(expected_keys)
            else:
                key_score = 1.0
            return False, round(key_score * 0.5, 4), actual_hash

        return False, 0.0, actual_hash


class EfficiencyEvaluationResult(tuple):
    """Result tuple supporting both 4-tuple backward compatibility and named attributes."""
    efficiency_success: bool
    budget_tokens_compliant: bool
    budget_cost_compliant: bool
    budget_latency_compliant: bool
    violations: List[str]

    def __new__(cls, efficiency_success: bool, tokens_ok: bool, cost_ok: bool, violations: List[str], duration_ok: bool = True):
        inst = super().__new__(cls, (efficiency_success, tokens_ok, cost_ok, violations))
        inst.efficiency_success = efficiency_success
        inst.budget_tokens_compliant = tokens_ok
        inst.budget_cost_compliant = cost_ok
        inst.budget_latency_compliant = duration_ok
        inst.violations = violations
        return inst


class EfficiencyOracle:
    """Oracle auditing budget limits, vendor cache accounting, latency, and provenance classification."""

    @classmethod
    def evaluate(
        cls,
        telemetry: CognitiveTelemetryRecord,
        expected_task: SyntheticTaskDefinition,
    ) -> EfficiencyEvaluationResult:
        """Evaluate resource, cost, latency, and telemetry compliance.

        Returns EfficiencyEvaluationResult (unpacks as 4-tuple: efficiency_success, budget_tokens_compliant, budget_cost_compliant, violations).
        """
        violations: List[str] = []
        tok = telemetry.tokens
        cost = telemetry.cost
        lat = telemetry.latencies

        # 1. Budget compliance - Unknown or missing metrics must NOT be approved as compliant
        tok_count = tok.total_tokens
        if tok_count is None or tok.provenance == MetricProvenance.UNKNOWN:
            tokens_ok = False
            violations.append("Métrica de tokens ausente ou desconhecida: impossível validar conformidade orçamentária")
        else:
            tokens_ok = tok_count <= expected_task.budget_limit_tokens
            if not tokens_ok:
                violations.append(
                    f"Limite de tokens excedido: observado {tok_count} > orçado {expected_task.budget_limit_tokens}"
                )

        cost_val = cost.total_cost_usd
        if cost_val is None or cost.provenance == MetricProvenance.UNKNOWN:
            cost_ok = False
            violations.append("Métrica de custo ausente ou desconhecida: impossível validar conformidade orçamentária")
        else:
            cost_ok = cost_val <= expected_task.budget_limit_cost_usd
            if not cost_ok:
                violations.append(
                    f"Limite de custo excedido: observado ${cost_val:.6f} > orçado ${expected_task.budget_limit_cost_usd:.6f}"
                )

        # 2. Strict latency and duration compliance (using monotonic clock verification)
        duration_val = lat.total_duration_ms
        if duration_val is None or lat.latency_provenance == MetricProvenance.UNKNOWN:
            duration_ok = False
            violations.append("Métrica de latência/duração ausente ou desconhecida: impossível validar conformidade temporal")
        else:
            duration_ok = duration_val <= expected_task.max_duration_ms
            if not duration_ok:
                violations.append(
                    f"Limite de duração excedido: observado {duration_val:.2f} ms > máximo permitido {expected_task.max_duration_ms:.2f} ms"
                )

        # 3. Strict provenance validation
        if tok.provenance not in MetricProvenance.ALL:
            violations.append(f"Proveniência de tokens inválida: '{tok.provenance}'")

        if cost.provenance not in MetricProvenance.ALL:
            violations.append(f"Proveniência de custo inválida: '{cost.provenance}'")

        if lat.latency_provenance not in MetricProvenance.ALL:
            violations.append(f"Proveniência de latência inválida: '{lat.latency_provenance}'")

        # 4. Cache discount validation (no double-billing for OpenAI/Gemini)
        prov = telemetry.provider.lower()
        if prov in ("openai", "azure", "google", "gemini") and tok.cache_read_tokens and tok.input_tokens:
            if cost.input_cost_usd is not None and cost.cache_read_cost_usd is not None:
                rates = PricingRegistry.get_pricing(telemetry.model_name)
                if rates:
                    uncached = max(0, tok.input_tokens - tok.cache_read_tokens)
                    expected_in_cost = round((uncached / 1_000_000.0) * rates["input_per_m"], 8)
                    if abs((cost.input_cost_usd or 0.0) - expected_in_cost) > 1e-6:
                        violations.append(
                            f"Sobretarifação de cache detectada: entrada faturou {cost.input_cost_usd} em vez de {expected_in_cost}"
                        )

        efficiency_success = tokens_ok and cost_ok and duration_ok and len(violations) == 0
        return EfficiencyEvaluationResult(efficiency_success, tokens_ok, cost_ok, violations, duration_ok)


class SecurityOracle:
    """Oracle verifying zero secret leaks in actual candidate output, text, metadata, and persistence."""

    @classmethod
    def evaluate(
        cls,
        actual_output: Any = None,
        telemetry: Optional[CognitiveTelemetryRecord] = None,
        store: Optional[Any] = None,
    ) -> Tuple[bool, bool, bool, List[str]]:
        """Evaluate zero-secret invariant on candidate output and telemetry, and test database immutability.

        Returns (security_success, zero_secrets_leaked, sqlite_immutable, violations).
        """
        # Handle both positional signatures: evaluate(telemetry, store) and evaluate(actual_output, telemetry, store)
        if isinstance(actual_output, CognitiveTelemetryRecord) and (telemetry is None or not isinstance(telemetry, CognitiveTelemetryRecord)):
            store = telemetry
            telemetry = actual_output
            actual_output = None

        if telemetry is None:
            raise ValueError("telemetry record é obrigatório para SecurityOracle.evaluate")

        violations: List[str] = []
        zero_leak = True
        sqlite_ok: Optional[bool] = None

        # 1. Inspect actual candidate output directly BEFORE any transformation
        if actual_output is not None:
            raw_output_text = json.dumps(_normalize_json_structure(actual_output), ensure_ascii=False)
            for pat in _SENSITIVE_LEAK_CHECK_PATTERNS:
                if pat.search(raw_output_text):
                    zero_leak = False
                    violations.append("Credencial vazada exposta diretamente na saída (actual_output) do candidato")
                    break

        # 2. Inspect error message for unredacted credentials
        if telemetry.error_message:
            for pat in _SENSITIVE_LEAK_CHECK_PATTERNS:
                if pat.search(telemetry.error_message):
                    zero_leak = False
                    violations.append(f"Credencial vazada em error_message: {telemetry.error_message[:40]}...")
                    break

        # 3. Inspect metadata (keys and values)
        raw_meta = json.dumps(telemetry.metadata, ensure_ascii=False)
        for pat in _SENSITIVE_LEAK_CHECK_PATTERNS:
            if pat.search(raw_meta):
                zero_leak = False
                violations.append("Credencial em texto não sanitizado vazada no payload de metadata")
                break

        # 4. Test SQLite immutability triggers safely using an isolated disposable in-memory database
        # This completely eliminates concurrent SAVEPOINT/ROLLBACK interference on the shared connection
        if store is not None and hasattr(store, "db"):
            try:
                # Thread-safe read of schema definitions from store.db
                lock = getattr(store, "_lock", None)
                lock_ctx = lock if lock is not None else contextlib.nullcontext()
                with lock_ctx:
                    schema_rows = store.db.execute(
                        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = 'cognitive_telemetry' AND sql IS NOT NULL"
                    ).fetchall()
                    sample_row = None
                    col_count = 0
                    if telemetry.id is not None:
                        cur = store.db.execute("SELECT * FROM cognitive_telemetry WHERE id = ?", (telemetry.id,))
                        sample_row = cur.fetchone()
                        col_count = len(cur.description) if cur.description else 0

                # Recreate schema in a dedicated disposable in-memory database
                disposable_db = sqlite3.connect(":memory:")
                try:
                    tables_sql = [r[2] for r in schema_rows if r[0] == "table"]
                    triggers_sql = [r[2] for r in schema_rows if r[0] == "trigger"]

                    for t_sql in tables_sql:
                        disposable_db.execute(t_sql)
                    for trg_sql in triggers_sql:
                        disposable_db.execute(trg_sql)

                    # Insert probe row into disposable database
                    probe_id = telemetry.id if telemetry.id is not None else 1
                    if sample_row is not None and col_count > 0:
                        placeholders = ",".join(["?"] * col_count)
                        disposable_db.execute(f"INSERT INTO cognitive_telemetry VALUES ({placeholders})", tuple(sample_row))
                    else:
                        disposable_db.execute(
                            "INSERT INTO cognitive_telemetry (id, task_id, provider, model_name, tokens_provenance, total_duration_ms, latency_provenance, cost_provenance) "
                            "VALUES (?, 'probe_task', 'probe_prov', 'probe_mod', 'estimated', 1.0, 'measured', 'estimated')",
                            (probe_id,),
                        )

                    # Test UPDATE rejection on the disposable database
                    try:
                        disposable_db.execute(
                            "UPDATE cognitive_telemetry SET task_id = 'tampered' WHERE id = ?",
                            (probe_id,),
                        )
                        sqlite_ok = False
                        violations.append("Violação de segurança: UPDATE na tabela cognitive_telemetry não foi rejeitado pelo trigger!")
                    except sqlite3.IntegrityError as err:
                        if "immutable and append-only" in str(err):
                            sqlite_ok = True
                        else:
                            sqlite_ok = False
                            violations.append(f"IntegrityError inesperado na verificação de integridade: {err}")

                    # If UPDATE was rejected, also test DELETE rejection
                    if sqlite_ok:
                        try:
                            disposable_db.execute(
                                "DELETE FROM cognitive_telemetry WHERE id = ?",
                                (probe_id,),
                            )
                            sqlite_ok = False
                            violations.append("Violação de segurança: DELETE na tabela cognitive_telemetry não foi rejeitado pelo trigger!")
                        except sqlite3.IntegrityError as del_err:
                            if "immutable and cannot be deleted" in str(del_err):
                                sqlite_ok = True
                            else:
                                sqlite_ok = False
                                violations.append(f"IntegrityError inesperado na exclusão: {del_err}")
                finally:
                    disposable_db.close()
            except Exception as outer_err:
                sqlite_ok = False
                violations.append(f"Falha ao executar teste de imutabilidade em banco descartável: {outer_err}")
        else:
            # Store was not provided or has no db handle: explicitly mark as not verified / not applicable
            sqlite_ok = None

        # If sqlite_ok was evaluated, it cannot be False; if None, mark as unverified
        if sqlite_ok is False or not zero_leak or len(violations) > 0:
            security_success = False
        elif sqlite_ok is True and zero_leak and len(violations) == 0:
            security_success = True
        elif sqlite_ok is None and zero_leak and len(violations) == 0:
            security_success = None
        else:
            security_success = False

        return security_success, zero_leak, sqlite_ok, violations


class SharedBudgetExceededError(RuntimeError):
    """Raised when an atomic budget reservation cannot be granted within shared limits."""
    pass


class SharedFlightBudgetFence:
    """Thread-safe, atomic reservation manager for shared token and monetary cost budgets across concurrent workers.

    Guarantees:
    - Atomicity: Concurrent workers reserve budget under synchronization; total active allocations never exceed limits.
    - Pre-consumption Rejection: If remaining tokens or cost are insufficient, the reservation is rejected BEFORE consumption.
    - Safe Release / Commit: Workers commit actual usage or release reservations upon cancellation/failure.
    """

    def __init__(self, max_tokens: int, max_cost_usd: float):
        self.max_tokens = max(0, int(max_tokens))
        self.max_cost_usd = max(0.0, float(max_cost_usd))
        self._lock = threading.Lock()
        self._reserved_tokens = 0
        self._reserved_cost_usd = 0.0
        self._committed_tokens = 0
        self._committed_cost_usd = 0.0
        self._active_reservations: Dict[str, Tuple[int, float]] = {}
        self._rejected_requests: int = 0
        self._approved_requests: int = 0

    @property
    def remaining_tokens(self) -> int:
        with self._lock:
            return max(0, self.max_tokens - (self._reserved_tokens + self._committed_tokens))

    @property
    def remaining_cost_usd(self) -> float:
        with self._lock:
            return max(0.0, self.max_cost_usd - (self._reserved_cost_usd + self._committed_cost_usd))

    @property
    def approved_count(self) -> int:
        with self._lock:
            return self._approved_requests

    @property
    def rejected_count(self) -> int:
        with self._lock:
            return self._rejected_requests

    @property
    def committed_tokens(self) -> int:
        with self._lock:
            return self._committed_tokens

    @property
    def committed_cost_usd(self) -> float:
        with self._lock:
            return self._committed_cost_usd

    @property
    def reserved_tokens(self) -> int:
        with self._lock:
            return self._reserved_tokens

    @property
    def reserved_cost_usd(self) -> float:
        with self._lock:
            return self._reserved_cost_usd

    def reserve(self, worker_id: str, estimated_tokens: int, estimated_cost_usd: float) -> str:
        """Attempt to atomically reserve budget BEFORE consumption.

        Raises SharedBudgetExceededError if the requested reservation would exceed shared limits.
        Returns a unique reservation_id on success.
        """
        req_tok = max(0, int(estimated_tokens))
        req_cost = max(0.0, float(estimated_cost_usd))

        with self._lock:
            projected_tokens = self._reserved_tokens + self._committed_tokens + req_tok
            projected_cost = self._reserved_cost_usd + self._committed_cost_usd + req_cost

            if projected_tokens > self.max_tokens or projected_cost > self.max_cost_usd:
                self._rejected_requests += 1
                raise SharedBudgetExceededError(
                    f"Reserva de orçamento rejeitada para worker '{worker_id}' antes do consumo: "
                    f"tokens {projected_tokens}/{self.max_tokens}, custo ${projected_cost:.4f}/${self.max_cost_usd:.4f}"
                )

            reservation_id = f"res_{worker_id}_{uuid.uuid4().hex[:8]}"
            self._reserved_tokens += req_tok
            self._reserved_cost_usd += req_cost
            self._active_reservations[reservation_id] = (req_tok, req_cost)
            self._approved_requests += 1
            return reservation_id

    def commit(self, reservation_id: str, actual_tokens: int, actual_cost_usd: float) -> None:
        """Commit actual consumption for an approved reservation and release any unused reserve.

        If actual consumption exceeds the reserved amount, the additional consumption is
        atomically re-validated against remaining shared headroom.
        Raises SharedBudgetExceededError if the commit would cause shared limits to be breached.
        Raises KeyError if the reservation_id is unknown or already finalized.
        """
        act_tok = max(0, int(actual_tokens))
        act_cost = max(0.0, float(actual_cost_usd))

        with self._lock:
            if reservation_id not in self._active_reservations:
                raise KeyError(f"Reserva desconhecida, expirada ou já confirmada: '{reservation_id}'")

            res_tok, res_cost = self._active_reservations[reservation_id]

            # Projected total tokens and cost across all workers if this commit is accepted
            other_reserved_tokens = self._reserved_tokens - res_tok
            other_reserved_cost = self._reserved_cost_usd - res_cost
            projected_tokens = self._committed_tokens + act_tok + other_reserved_tokens
            projected_cost = self._committed_cost_usd + act_cost + other_reserved_cost

            if projected_tokens > self.max_tokens or projected_cost > self.max_cost_usd:
                self._rejected_requests += 1
                raise SharedBudgetExceededError(
                    f"Consumo efetivo no commit excedeu o orçamento compartilhado para '{reservation_id}': "
                    f"tokens projetados {projected_tokens}/{self.max_tokens}, "
                    f"custo projetado ${projected_cost:.4f}/${self.max_cost_usd:.4f} "
                    f"(reserva: {res_tok} tok, ${res_cost:.4f}; efetivo solicitado: {act_tok} tok, ${act_cost:.4f})"
                )

            # Atomically commit and release reservation
            self._active_reservations.pop(reservation_id)
            self._reserved_tokens = max(0, other_reserved_tokens)
            self._reserved_cost_usd = max(0.0, other_reserved_cost)
            self._committed_tokens += act_tok
            self._committed_cost_usd += act_cost

    def release(self, reservation_id: str) -> None:
        """Release an unused reservation (e.g. on worker failure or cancellation)."""
        with self._lock:
            if reservation_id not in self._active_reservations:
                return
            res_tok, res_cost = self._active_reservations.pop(reservation_id)
            self._reserved_tokens = max(0, self._reserved_tokens - res_tok)
            self._reserved_cost_usd = max(0.0, self._reserved_cost_usd - res_cost)


class SyntheticBenchmarkRunner:
    """Local, offline benchmark orchestrator that evaluates cognitive tasks using tri-dimensional oracles."""

    def __init__(self, store: Optional[Any] = None, hmac_key: Optional[str] = None):
        self.store = store
        self.hmac_key = hmac_key

    @classmethod
    def verify_report(
        cls,
        report: EvaluationOracleReport,
        hmac_key: Optional[str] = None,
        benchmark_version: str = DATASET_VERSION,
    ) -> bool:
        """Verify comprehensive integrity and HMAC authentication of an oracle report."""
        if not report.verify_integrity(benchmark_version):
            return False
        if hmac_key is not None:
            return report.verify_signature(hmac_key, benchmark_version)
        return True

    def run_task(
        self,
        task: SyntheticTaskDefinition,
        solver_fn: Any,
        provider: str = "synthetic",
        model_name: str = "test-model",
        hmac_key: Optional[str] = None,
    ) -> EvaluationOracleReport:
        """Execute a synthetic task, collect fine-grained telemetry, and evaluate with all oracles."""
        t0 = dt.datetime.now(dt.timezone.utc)
        start_perf = time.perf_counter()

        # Execute candidate solver under test
        actual_output = None
        error_msg = None
        success = True
        try:
            actual_output = solver_fn(task.input_payload)
        except Exception as exc:
            success = False
            error_msg = str(exc)

        end_perf = time.perf_counter()
        duration_ms = max(0.0, (end_perf - start_perf) * 1000.0)

        # Build telemetry record - tokens estimated by character count are strictly marked ESTIMATED
        input_tokens = len(json.dumps(task.input_payload)) // 4
        output_tokens = len(json.dumps(actual_output or {})) // 4
        tokens = TokenMetrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provenance=MetricProvenance.ESTIMATED,  # Heuristic token counting is strictly ESTIMATED
        )
        latencies = LatencyMetrics(
            total_duration_ms=duration_ms,
            latency_provenance=MetricProvenance.MEASURED,
        )
        cost = PricingRegistry.estimate_cost(model_name, tokens, provider=provider)

        telemetry = CognitiveTelemetryRecord(
            task_id=task.task_id,
            provider=provider,
            model_name=model_name,
            tokens=tokens,
            latencies=latencies,
            cost=cost,
            success=success,
            error_message=error_msg,
            metadata={
                "suite": task.suite,
                "task_type": task.task_type,
                "seed": task.seed,
                "version": task.version,
            },
        )

        # Persist to store if available
        if self.store is not None and hasattr(self.store, "record_cognitive_telemetry"):
            telemetry = self.store.record_cognitive_telemetry(telemetry)

        # 1. Functional Oracle (compares actual output against expected output)
        func_ok, correctness, actual_hash = FunctionalOracle.evaluate(actual_output, task)

        # 2. Efficiency Oracle (audits budget limits, duration limit, provenance, and cache discounts)
        eff_res = EfficiencyOracle.evaluate(telemetry, task)
        eff_ok, tok_ok, cost_ok, eff_violations = eff_res
        duration_ok = getattr(eff_res, "budget_latency_compliant", True)

        # 3. Security Oracle (evaluates candidate's actual output directly before any redaction)
        sec_ok, zero_leak, sqlite_ok, sec_violations = SecurityOracle.evaluate(
            actual_output=actual_output,
            telemetry=telemetry,
            store=self.store,
        )

        all_violations = tuple(eff_violations + sec_violations)

        # Build preliminary report to generate canonical manifest
        input_payload_hash = canonical_json_hash(task.input_payload)
        preliminary_report = EvaluationOracleReport(
            task_id=task.task_id,
            suite=task.suite,
            input_payload_hash=input_payload_hash,
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

        # Compute comprehensive canonical integrity hash covering ALL metrics, violations, and results
        manifest = preliminary_report.compute_canonical_manifest(benchmark_version=task.version)
        canonical_bytes = json.dumps(_normalize_json_structure(manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        integrity_hash = hashlib.sha256(canonical_bytes).hexdigest()

        # Compute authenticated HMAC signature over the comprehensive canonical manifest
        active_hmac_key = hmac_key or self.hmac_key
        oracle_sig: Optional[str] = None
        if active_hmac_key:
            oracle_sig = hmac.new(active_hmac_key.encode("utf-8"), canonical_bytes, hashlib.sha256).hexdigest()

        return dataclasses.replace(
            preliminary_report,
            integrity_hash=integrity_hash,
            oracle_signature=oracle_sig,
        )
