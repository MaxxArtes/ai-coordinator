"""Synthetic Datasets and Evaluation Oracles for Adaptive Cognitive Optimization (Phase 4C.0.2).

Provides 100% offline, deterministic, reproducible benchmark suites (Suites A, B, and C)
and tri-dimensional evaluation oracles (Functional, Efficiency, Security) for scientific
cognitive baseline experimentation without external API costs or credential exposure.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import math
import random
import re
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


def canonical_json_hash(data: Any) -> str:
    """Compute deterministic SHA-256 hash of a JSON-serializable structure."""
    clean_data = scrub_sensitive_value(data)
    canonical_bytes = json.dumps(clean_data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
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
    security_success: bool
    zero_secrets_leaked: bool
    sqlite_immutable: bool
    violation_details: Tuple[str, ...] = field(default_factory=tuple)
    oracle_signature: str = ""
    timestamp: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    @property
    def passed_all(self) -> bool:
        return self.functional_success and self.efficiency_success and self.security_success

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "suite": self.suite,
            "passed_all": self.passed_all,
            "functional_success": self.functional_success,
            "correctness_score": self.correctness_score,
            "canonical_output_hash": self.canonical_output_hash,
            "expected_output_hash": self.expected_output_hash,
            "efficiency_success": self.efficiency_success,
            "budget_tokens_compliant": self.budget_tokens_compliant,
            "budget_cost_compliant": self.budget_cost_compliant,
            "observed_tokens": self.observed_tokens.to_dict(),
            "observed_cost": self.observed_cost.to_dict(),
            "observed_latencies": self.observed_latencies.to_dict(),
            "security_success": self.security_success,
            "zero_secrets_leaked": self.zero_secrets_leaked,
            "sqlite_immutable": self.sqlite_immutable,
            "violation_details": list(self.violation_details),
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


class EfficiencyOracle:
    """Oracle auditing budget limits, vendor cache accounting, and provenance classification."""

    @classmethod
    def evaluate(
        cls,
        telemetry: CognitiveTelemetryRecord,
        expected_task: SyntheticTaskDefinition,
    ) -> Tuple[bool, bool, bool, List[str]]:
        """Evaluate resource and telemetry compliance.

        Returns (efficiency_success, budget_tokens_compliant, budget_cost_compliant, violations).
        """
        violations: List[str] = []
        tok = telemetry.tokens
        cost = telemetry.cost

        # 1. Budget compliance
        tok_count = tok.total_tokens or 0
        tokens_ok = tok_count <= expected_task.budget_limit_tokens
        if not tokens_ok:
            violations.append(
                f"Limite de tokens excedido: observado {tok_count} > orçado {expected_task.budget_limit_tokens}"
            )

        cost_val = cost.total_cost_usd or 0.0
        cost_ok = cost_val <= expected_task.budget_limit_cost_usd
        if not cost_ok:
            violations.append(
                f"Limite de custo excedido: observado ${cost_val:.6f} > orçado ${expected_task.budget_limit_cost_usd:.6f}"
            )

        # 2. Strict provenance validation
        if tok.provenance not in MetricProvenance.ALL:
            violations.append(f"Proveniência de tokens inválida: '{tok.provenance}'")

        if cost.provenance not in MetricProvenance.ALL:
            violations.append(f"Proveniência de custo inválida: '{cost.provenance}'")

        # 3. Cache discount validation (no double-billing for OpenAI/Gemini)
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

        efficiency_success = tokens_ok and cost_ok and len(violations) == 0
        return efficiency_success, tokens_ok, cost_ok, violations


class SecurityOracle:
    """Oracle verifying zero secret leaks in text/metadata/keys and audit persistence immutability."""

    @classmethod
    def evaluate(
        cls,
        telemetry: CognitiveTelemetryRecord,
        store: Optional[Any] = None,
    ) -> Tuple[bool, bool, bool, List[str]]:
        """Evaluate zero-secret invariant and database immutability.

        Returns (security_success, zero_secrets_leaked, sqlite_immutable, violations).
        """
        violations: List[str] = []
        zero_leak = True
        sqlite_ok = True

        # 1. Inspect error message for unredacted credentials
        if telemetry.error_message:
            for pat in _SENSITIVE_LEAK_CHECK_PATTERNS:
                if pat.search(telemetry.error_message):
                    zero_leak = False
                    violations.append(f"Credencial vazada em error_message: {telemetry.error_message[:40]}...")
                    break

        # 2. Inspect metadata (keys and values)
        raw_meta = json.dumps(telemetry.metadata, ensure_ascii=False)
        for pat in _SENSITIVE_LEAK_CHECK_PATTERNS:
            if pat.search(raw_meta):
                zero_leak = False
                violations.append("Credencial em texto não sanitizado vazada no payload de metadata")
                break

        # 3. Test SQLite immutability triggers if a store is provided
        if store is not None and telemetry.id is not None:
            try:
                # Attempt illegal UPDATE
                store.db.execute(
                    "UPDATE cognitive_telemetry SET task_id = 'tampered' WHERE id = ?",
                    (telemetry.id,),
                )
                sqlite_ok = False
                violations.append("Violação de segurança: UPDATE na tabela cognitive_telemetry não foi rejeitado!")
            except Exception:
                # Expected IntegrityError from trigger
                sqlite_ok = True

        security_success = zero_leak and sqlite_ok and len(violations) == 0
        return security_success, zero_leak, sqlite_ok, violations


class SyntheticBenchmarkRunner:
    """Local, offline benchmark orchestrator that evaluates cognitive tasks using tri-dimensional oracles."""

    def __init__(self, store: Optional[Any] = None):
        self.store = store

    def run_task(
        self,
        task: SyntheticTaskDefinition,
        solver_fn: Any,
        provider: str = "synthetic",
        model_name: str = "test-model",
    ) -> EvaluationOracleReport:
        """Execute a synthetic task, collect fine-grained telemetry, and evaluate with all oracles."""
        t0 = dt.datetime.now(dt.timezone.utc)
        start_perf = dt.datetime.now().timestamp()

        # Execute candidate solver under test
        actual_output = None
        error_msg = None
        success = True
        try:
            actual_output = solver_fn(task.input_payload)
        except Exception as exc:
            success = False
            error_msg = str(exc)

        end_perf = dt.datetime.now().timestamp()
        duration_ms = max(0.0, (end_perf - start_perf) * 1000.0)

        # Build telemetry record
        input_tokens = len(json.dumps(task.input_payload)) // 4
        output_tokens = len(json.dumps(actual_output or {})) // 4
        tokens = TokenMetrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provenance=MetricProvenance.MEASURED,
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

        # 1. Functional Oracle
        func_ok, correctness, actual_hash = FunctionalOracle.evaluate(actual_output, task)

        # 2. Efficiency Oracle
        eff_ok, tok_ok, cost_ok, eff_violations = EfficiencyOracle.evaluate(telemetry, task)

        # 3. Security Oracle
        sec_ok, zero_leak, sqlite_ok, sec_violations = SecurityOracle.evaluate(telemetry, self.store)

        all_violations = tuple(eff_violations + sec_violations)
        signature = hashlib.sha256(
            f"{task.task_id}:{actual_hash}:{func_ok}:{eff_ok}:{sec_ok}".encode("utf-8")
        ).hexdigest()[:16]

        return EvaluationOracleReport(
            task_id=task.task_id,
            suite=task.suite,
            functional_success=func_ok,
            correctness_score=correctness,
            canonical_output_hash=actual_hash,
            expected_output_hash=task.expected_hash,
            efficiency_success=eff_ok,
            budget_tokens_compliant=tok_ok,
            budget_cost_compliant=cost_ok,
            observed_tokens=tokens,
            observed_cost=cost,
            observed_latencies=latencies,
            security_success=sec_ok,
            zero_secrets_leaked=zero_leak,
            sqlite_immutable=sqlite_ok,
            violation_details=all_violations,
            oracle_signature=signature,
        )
