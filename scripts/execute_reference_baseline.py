#!/usr/bin/env python3
"""Execute controlled empirical baseline campaign for Phase 4C.0.5.

Executes:
1. Warmup cycle (1 iteration, 9 tasks) to prime OS page caches, SQLite JIT/WAL, and bytecode caches.
2. Candidate reference baseline run (10 iterations, 90 task executions).
3. Signs root manifest with Benchmark Authority RSASSA-PKCS1-v1_5 digital signature (asymmetric public key).
4. Computes statistical distributions (sample and population std dev, p50, p95, p99) and 95% Student's t confidence intervals.
5. Serializes candidate run to docs/benchmarks/baseline_reference_run.json.
6. Programmatically generates docs/benchmarks/baseline_v0.5.1_report.md from the validated JSON.
7. Verifies cryptographic integrity and authenticity via scripts/verify_baseline_integrity.py.
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
from typing import Any, Dict, List, Optional

# Ensure repository root is in sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from ai_coordinator import BaselineRunner, Store
from ai_coordinator.crypto_signatures import load_private_key
from generate_baseline_report_md import generate_markdown_report


def compute_confidence_interval_95(sample_std: float, n: int) -> float:
    """Compute 95% confidence interval half-width using Student's t distribution approximation."""
    if n <= 1:
        return 0.0
    # For n=90 (df=89), t_0.025 ≈ 1.98698
    t_val = 1.987
    return round(t_val * (sample_std / math.sqrt(n)), 4)


def resolve_authority_signing_key() -> Optional[Dict[str, Any]]:
    """Resolve authority private signing key for asymmetric public digital signatures."""
    # 1. Environment variable: JSON string or path
    env_val = os.environ.get("AI_COORDINATOR_SIGNING_KEY")
    if env_val:
        if env_val.strip().startswith("{"):
            return json.loads(env_val)
        if os.path.exists(env_val):
            return load_private_key(env_val)

    # 2. Local protected authority keyfile
    local_privkey_path = os.path.join(REPO_ROOT, ".keys", "baseline_authority_privkey.json")
    if os.path.exists(local_privkey_path):
        return load_private_key(local_privkey_path)

    return None


def main() -> int:
    output_dir = os.path.join(REPO_ROOT, "docs", "benchmarks")
    os.makedirs(output_dir, exist_ok=True)
    candidate_json_path = os.path.join(output_dir, "baseline_reference_run.json")
    candidate_md_path = os.path.join(output_dir, "baseline_v0.5.1_report.md")

    # Secure HMAC key resolution: NO HARDCODED DEFAULT KEY (P1 security requirement)
    hmac_key = os.environ.get("AI_COORDINATOR_HMAC_KEY", None)

    # Authority private signing key resolution (pure-Python RSA PKCS#1 v1.5)
    signing_privkey = resolve_authority_signing_key()

    print("==================================================================")
    print(" AI Coordinator — Campanha Empírica de Linha de Base (Fase 4C.0.5)")
    print("==================================================================")
    if signing_privkey:
        print(f"  [AUTH] Assinatura Digital Assimétrica: Habilitada (Key ID: {signing_privkey.get('key_id')})")
    else:
        print(f"  [AUTH] Assinatura Digital Assimétrica: Desabilitada (nenhuma chave privada encontrada)")
    if hmac_key:
        print(f"  [HMAC] Autenticação Interna de Cluster: Habilitada via AI_COORDINATOR_HMAC_KEY")
    else:
        print(f"  [HMAC] Autenticação Interna de Cluster: Desabilitada (Modo Público / Zero Segredos)")

    # Step 1: Warmup Run
    print("\n[1/5] Executando ciclo de aquecimento (Warmup: 1 iteração, 9 tarefas)...")
    warmup_dir = tempfile.mkdtemp(prefix="ai_coord_warmup_")
    try:
        warmup_store = Store(warmup_dir)
        warmup_runner = BaselineRunner(store=warmup_store, hmac_key=hmac_key, signing_private_key=signing_privkey)
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
    print("\n[2/5] Executando campanha de referência empírica (10 iterações, 90 tarefas)...")
    run_dir = tempfile.mkdtemp(prefix="ai_coord_candidate_baseline_")
    try:
        run_store = Store(run_dir)
        runner = BaselineRunner(store=run_store, hmac_key=hmac_key, signing_private_key=signing_privkey)
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
    print(f"\n[3/5] Serializando relatório candidato em: {candidate_json_path}")
    report_dict = candidate_report.to_dict()
    with open(candidate_json_path, "w", encoding="utf-8") as f:
        json.dump(report_dict, f, indent=2, ensure_ascii=False)
    file_size_kb = os.path.getsize(candidate_json_path) / 1024
    print(f"      Arquivo gerado com sucesso ({file_size_kb:.2f} KB).")

    # Step 4: Programmatically generate Markdown report to ensure 100% data parity
    print(f"\n[4/5] Gerando relatório Markdown programático em: {candidate_md_path}")
    generate_markdown_report(candidate_json_path, candidate_md_path)

    # Step 5: Validate with standalone scripts/verify_baseline_integrity.py
    print("\n[5/5] Executando verificação independente com scripts/verify_baseline_integrity.py...")
    script_path = os.path.join(os.path.dirname(__file__), "verify_baseline_integrity.py")

    cmd = [sys.executable, script_path, "--report", candidate_json_path, "--cross-check"]
    env_vars = os.environ.copy()
    if hmac_key:
        cmd.extend(["--key-env", "AI_COORDINATOR_HMAC_KEY"])
        env_vars["AI_COORDINATOR_HMAC_KEY"] = hmac_key

    res = subprocess.run(cmd, capture_output=True, text=True, env=env_vars)
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
