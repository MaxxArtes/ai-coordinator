#!/usr/bin/env python3
"""Standalone Offline Verifier for AI Coordinator Baseline Reports (Phase 4C.0.5).

Enables independent external auditors and peer nodes to cryptographically verify:
1. Root manifest SHA-256 hash integrity.
2. Complete environment and experimental configuration metadata authentication.
3. Deterministic task_results_digest matching all individual task results.
4. Individual task report SHA-256 integrity for every executed task.
5. Optional HMAC-SHA256 authenticated signatures for root and individual task reports.
6. Strict rejection of missing, stripped, or tampered signatures in authenticated mode.

Usage:
    python scripts/verify_baseline_integrity.py --report path/to/baseline.json [--hmac-key SECRET]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure repository root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from ai_coordinator.baseline_runner import BaselineExecutionReport


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verificador Independente de Integridade de Baseline (Fase 4C.0.5 — AI Coordinator)"
    )
    parser.add_argument("--report", "-r", required=True, help="Caminho para o arquivo JSON do relatório de baseline")
    parser.add_argument("--hmac-key", "-k", default=None, help="Chave secreta para verificação de autenticidade HMAC-SHA256")
    args = parser.parse_args()

    if not os.path.exists(args.report):
        print(f"[ERRO] Arquivo de relatório não encontrado: '{args.report}'", file=sys.stderr)
        return 1

    try:
        with open(args.report, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except Exception as exc:
        print(f"[ERRO] Falha ao decodificar JSON de '{args.report}': {exc}", file=sys.stderr)
        return 1

    try:
        report = BaselineExecutionReport.from_dict(raw_data)
    except Exception as exc:
        print(f"[ERRO] Falha ao deserializar relatório de baseline: {exc}", file=sys.stderr)
        return 1

    mode_label = f"AUTENTICADO (HMAC-SHA256)" if args.hmac_key else "INTEGRIDADE ESTRUTURAL (SHA-256)"
    print(f"===========================================================")
    print(f" AI Coordinator — Verificador Independente de Baseline")
    print(f"===========================================================")
    print(f"  Arquivo:       {args.report}")
    print(f"  Modo:          {mode_label}")
    print(f"  Run ID:        {report.run_id}")
    print(f"  Git Commit:    {report.environment.git_commit[:12]}...")
    print(f"  Git Branch:    {report.environment.git_branch} (dirty={report.environment.git_dirty})")
    print(f"  Tarefas:       {report.total_tasks_executed} ({report.iterations} iterações, {report.environment.total_tasks_per_iteration} tarefas/iteração)")
    print(f"  Dataset Versão:{report.environment.dataset_version}")
    print(f"-----------------------------------------------------------")

    # Step 1: Root manifest hash integrity
    root_ok = report.verify_integrity()
    print(f"  [1/4] Hash de Integridade Raiz:        {'[OK] VÁLIDO' if root_ok else '[FALHA] DIVERGENTE'}")

    # Step 2: Task results digest
    digest_ok = report.task_results_digest == report.compute_task_results_digest()
    print(f"  [2/4] Digest Canônico das Tarefas:     {'[OK] VÁLIDO' if digest_ok else '[FALHA] DIVERGENTE'}")

    # Step 3: Root signature check (if key supplied)
    if args.hmac_key:
        sig_ok = report.verify_signature(args.hmac_key)
        print(f"  [3/4] Assinatura HMAC Raiz:            {'[OK] AUTÊNTICA' if sig_ok else '[FALHA] INVÁLIDA OU AUSENTE'}")
    else:
        sig_ok = True
        print(f"  [3/4] Assinatura HMAC Raiz:            [IGNORADO - Modo sem chave]")

    # Step 4: Deep report verification
    full_ok = report.verify_full_report_integrity(args.hmac_key)
    print(f"  [4/4] Verificação Profunda Completa:    {'[OK] APROVADA' if full_ok else '[FALHA] REPROVADA'}")
    print(f"===========================================================")

    if full_ok:
        print(f"\n>>> SUCESSO: O relatório de baseline é 100% íntegro e autêntico.")
        print(f"    Hash Raiz:    {report.integrity_hash}")
        print(f"    Digest Tasks: {report.task_results_digest}")
        if report.oracle_signature:
            print(f"    HMAC Raiz:    {report.oracle_signature}")
        return 0
    else:
        print(f"\n>>> REPROVADO: Falha na validação de integridade ou autenticidade do relatório.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
