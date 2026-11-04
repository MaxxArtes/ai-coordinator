#!/usr/bin/env python3
"""Execute controlled empirical baseline campaign for Phase 4C.0.5.

Executes:
1. Warmup cycle (1 iteration, 9 tasks) to prime OS page caches, SQLite JIT/WAL, and bytecode caches.
2. Candidate reference baseline run (10 iterations, 90 task executions).
3. Computes statistical distributions (sample and population std dev, p50, p95, p99) and 95% Student's t confidence intervals.
4. Serializes candidate run to docs/benchmarks/baseline_reference_run.json.
5. Verifies cryptographic integrity and authenticity via scripts/verify_baseline_integrity.py.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List

# Ensure repository root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from ai_coordinator import BaselineRunner, Store


def compute_confidence_interval_95(sample_std: float, n: int) -> float:
    """Compute 95% confidence interval half-width using Student's t distribution approximation."""
    if n <= 1:
        return 0.0
    # For n=90 (df=89), t_0.025 ≈ 1.98698
    t_val = 1.987
    return round(t_val * (sample_std / math.sqrt(n)), 4)


def main() -> int:
    output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs", "benchmarks"))
    os.makedirs(output_dir, exist_ok=True)
    candidate_json_path = os.path.join(output_dir, "baseline_reference_run.json")

    hmac_key = os.environ.get("AI_COORDINATOR_HMAC_KEY", "ai-coordinator-baseline-v0.5.1-reference-secret-2026")

    print("==================================================================")
    print(" AI Coordinator — Campanha Empírica de Linha de Base (Fase 4C.0.5)")
    print("==================================================================")

    # Step 1: Warmup Run
    print("\n[1/4] Executando ciclo de aquecimento (Warmup: 1 iteração, 9 tarefas)...")
    warmup_dir = tempfile.mkdtemp(prefix="ai_coord_warmup_")
    try:
        warmup_store = Store(warmup_dir)
        warmup_runner = BaselineRunner(store=warmup_store, hmac_key=hmac_key)
        t_w0 = time.perf_counter()
        warmup_report = warmup_runner.run_baseline(iterations=1)
        t_w1 = time.perf_counter()
        print(f"      Aquecimento concluído em {(t_w1 - t_w0)*1000:.2f} ms ({warmup_report.total_tasks_executed} tarefas).")
        print(f"      Tempo total das tarefas no aquecimento: {warmup_report.latency_distributions['total_elapsed_ms'].mean:.2f} ms (média).")
    finally:
        if hasattr(warmup_store, "db") and warmup_store.db:
            warmup_store.db.close()
        shutil.rmtree(warmup_dir, ignore_errors=True)

    # Step 2: Candidate Reference Baseline Run (10 iterations)
    print("\n[2/4] Executando campanha de referência empírica (10 iterações, 90 tarefas)...")
    run_dir = tempfile.mkdtemp(prefix="ai_coord_candidate_baseline_")
    try:
        run_store = Store(run_dir)
        runner = BaselineRunner(store=run_store, hmac_key=hmac_key)
        t0 = time.perf_counter()
        candidate_report = runner.run_baseline(iterations=10)
        t1 = time.perf_counter()
        total_campaign_sec = t1 - t0
        print(f"      Campanha concluída em {total_campaign_sec:.2f} s ({candidate_report.total_tasks_executed} tarefas executadas).")
    finally:
        if hasattr(run_store, "db") and run_store.db:
            run_store.db.close()
        shutil.rmtree(run_dir, ignore_errors=True)

    # Step 3: Serialize candidate JSON
    print(f"\n[3/4] Serializando relatório candidato em: {candidate_json_path}")
    report_dict = candidate_report.to_dict()
    with open(candidate_json_path, "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=2, ensure_ascii=False)
    file_size_kb = os.path.getsize(candidate_json_path) / 1024
    print(f"      Arquivo gerado com sucesso ({file_size_kb:.2f} KB).")

    # Step 4: Validate with standalone scripts/verify_baseline_integrity.py
    print("\n[4/4] Executando verificação independente com scripts/verify_baseline_integrity.py...")
    script_path = os.path.join(os.path.dirname(__file__), "verify_baseline_integrity.py")

    env_vars = os.environ.copy()
    env_vars["AI_COORDINATOR_HMAC_KEY"] = hmac_key

    res = subprocess.run(
        [sys.executable, script_path, "--report", candidate_json_path, "--key-env", "AI_COORDINATOR_HMAC_KEY", "--cross-check"],
        capture_output=True,
        text=True,
        env=env_vars,
    )
    print(res.stdout)
    if res.returncode != 0:
        print(f"[ERRO] Falha na verificação independente:\n{res.stderr}", file=sys.stderr)
        return 1

    # Statistical summary
    print("==================================================================")
    print(" RESUMO ESTATÍSTICO DA LINHA DE BASE CANDIDATA (v0.5.1)")
    print("==================================================================")
    dists = candidate_report.latency_distributions
    n_samples = candidate_report.total_tasks_executed

    for metric_name in ["task_execution_ms", "telemetry_overhead_ms", "validation_ms", "total_elapsed_ms"]:
        dist = dists[metric_name]
        ci95 = compute_confidence_interval_95(dist.sample_std_dev, n_samples)
        print(
            f"  * {metric_name:<24}: Media = {dist.mean:7.4f} ms +/- {ci95:6.4f} ms (95% CI) | "
            f"s = {dist.sample_std_dev:6.4f} | sigma = {dist.population_std_dev:6.4f} | "
            f"p50 = {dist.p50:6.4f} | p95 = {dist.p95:6.4f} | p99 = {dist.p99:6.4f}"
        )

    print("\nTaxas de Sucesso e Conformidade:")
    for rate_k, rate_v in candidate_report.success_rates.items():
        print(f"  * {rate_k:<24}: {rate_v * 100:.2f}%")

    print("\nAgregados de Tokens e Custos:")
    print(f"  * Total de Tokens:      {candidate_report.token_aggregates['total_tokens']:,} (média: {candidate_report.token_aggregates['avg_tokens_per_task']:.2f}/tarefa)")
    print(f"  * Custo Total USD:      ${candidate_report.cost_aggregates['total_cost_usd']:.6f} (média: ${candidate_report.cost_aggregates['avg_cost_per_task_usd']:.6f}/tarefa)")
    print("==================================================================")

    return 0


if __name__ == "__main__":
    sys.exit(main())
