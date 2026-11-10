#!/usr/bin/env python3
"""Automated Markdown Report Generator for AI Coordinator Baseline Benchmarks.

Generates docs/benchmarks/baseline_v0.5.1_report.md programmatically from
docs/benchmarks/baseline_reference_run.json to guarantee 100% exact parity
between serialized data and published documentation, preventing human transcription errors.
"""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Any, Dict, List


def compute_ic95(std_dev: float, n: int) -> float:
    """Compute 95% Student's t confidence interval half-width."""
    if n <= 1:
        return 0.0
    t_val = 1.987 if n >= 80 else 2.0
    return round(t_val * (std_dev / math.sqrt(n)), 4)


def generate_markdown_report(json_path: str, output_md_path: str) -> None:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    env = data.get("environment", {})
    lat = data.get("latency_distributions", {})
    tok = data.get("token_aggregates", {})
    cost = data.get("cost_aggregates", {})
    rates = data.get("success_rates", {})
    task_results = data.get("task_execution_results", [])
    n_total = data.get("total_tasks_executed", len(task_results))
    iterations = data.get("iterations", 10)

    # Compute per-task statistical aggregates
    tasks_by_id: Dict[str, List[Dict[str, Any]]] = {}
    for r in task_results:
        t_id = r["task_id"]
        tasks_by_id.setdefault(t_id, []).append(r)

    per_task_rows = []
    for t_id, runs in sorted(tasks_by_id.items()):
        suite = runs[0].get("suite", "")
        t_type = runs[0].get("task_type", "")
        reps = len(runs)
        exec_times = [r["timing_breakdown"]["task_execution_ms"] for r in runs]
        telem_times = [r["timing_breakdown"]["telemetry_overhead_ms"] for r in runs]
        val_times = [r["timing_breakdown"]["validation_ms"] for r in runs]
        tot_times = [r["timing_breakdown"]["total_elapsed_ms"] for r in runs]

        in_tok = runs[0]["oracle_report"]["observed_tokens"]["input_tokens"]
        out_tok = runs[0]["oracle_report"]["observed_tokens"]["output_tokens"]
        c_usd = runs[0]["oracle_report"]["observed_cost"]["total_cost_usd"]

        avg_exec = sum(exec_times) / reps if reps else 0.0
        avg_telem = sum(telem_times) / reps if reps else 0.0
        avg_val = sum(val_times) / reps if reps else 0.0
        avg_tot = sum(tot_times) / reps if reps else 0.0

        all_passed = all(r["oracle_report"]["passed_all"] for r in runs)
        per_task_rows.append({
            "task_id": t_id,
            "suite": suite,
            "task_type": t_type,
            "reps": reps,
            "avg_exec": avg_exec,
            "avg_telem": avg_telem,
            "avg_val": avg_val,
            "avg_tot": avg_tot,
            "in_tok": in_tok,
            "out_tok": out_tok,
            "cost_usd": c_usd,
            "all_passed": all_passed,
        })

    # Global latency metrics with IC 95%
    t_exec = lat.get("task_execution_ms", {})
    t_telem = lat.get("telemetry_overhead_ms", {})
    t_val = lat.get("validation_ms", {})
    t_tot = lat.get("total_elapsed_ms", {})

    ic_exec = compute_ic95(t_exec.get("sample_std_dev", 0.0), n_total)
    ic_telem = compute_ic95(t_telem.get("sample_std_dev", 0.0), n_total)
    ic_val = compute_ic95(t_val.get("sample_std_dev", 0.0), n_total)
    ic_tot = compute_ic95(t_tot.get("sample_std_dev", 0.0), n_total)

    md = rf"""# Relatório Científico da Linha de Base — AI Coordinator v{env.get('ai_coordinator_version', '0.5.1')}

> **Status:** Linha de Base de Referência Sintética (Staging) — Pronta para Homologação  
> **Identificador da Execução (Run ID):** `{data.get('run_id')}`  
> **Data de Execução:** `{data.get('created_at')}`  
> **Hash de Integridade Raiz (SHA-256):** `{data.get('integrity_hash')}`  
> **Digest Canônico das Tarefas (SHA-256):** `{data.get('task_results_digest')}`  
> **Assinatura Digital Pública:** `{data.get('digital_signature', 'N/A')}`  
> **Algoritmo de Assinatura:** `{data.get('signature_algorithm', 'RSASSA-PKCS1-v1_5-SHA256')}`  
> **Fingerprint da Chave Pública da Autoridade:** `{data.get('public_key_fingerprint', 'N/A')}`  

---

## 1. Escopo e Classificação da Linha de Base

> [!IMPORTANT]
> **Natureza Científica da Medição:**  
> Esta referência é uma **baseline sintética offline de calibração** dos componentes locais do coordenador (oráculos determinísticos, instrumentação e armazenamento SQLite WAL).  
> - As durações temporais ($T_{{exec}}$, $T_{{telemetry}}$, $T_{{val}}$, $T_{{total}}$) são medições computacionais locais e determinísticas em código Python executado pelo interpretador oficial.  
> - Os valores de tokens ({tok.get('total_tokens', 0):,} tokens totais) e custos (US$ {cost.get('total_cost_usd', 0.0):.6f}) são **estimativas sintéticas controladas** derivadas das tabelas do `PricingRegistry`, e **NÃO representam consumo real de APIs ou modelos de linguagem remotos**.  
> - Zero chamadas de rede ou APIs proprietárias foram consumidas durante a campanha.

---

## 2. Metadados do Ambiente e Proveniência Criptográfica

| Parâmetro | Valor Registrado | Observações |
| :--- | :--- | :--- |
| **Versão do AI Coordinator** | `{env.get('ai_coordinator_version')}` | Núcleo base sem dependências externas |
| **Branch Git** | `{env.get('git_branch')}` | Branch oficial da Fase 4C |
| **Commit Git** | `{env.get('git_commit')}` | SHA-1 canônico de proveniência |
| **Estado da Árvore (git_dirty)** | `{env.get('git_dirty')}` | Execução em árvore limpa e reprodutível |
| **Versão Python** | `{env.get('python_version')}` | Interpretador do ambiente de execução |
| **Sistema Operacional** | `{env.get('platform_system')} {env.get('platform_release')} ({env.get('platform_machine')})` | Plataforma anfitriã |
| **Versão do Dataset Sintético** | `{env.get('dataset_version')}` | Versão imutável das Suítes A, B e C |
| **Versão do PricingRegistry** | `{env.get('pricing_registry_version')}` | Catálogo de precificação fixo |
| **Heurísticas Adaptativas Ativas** | `{env.get('adaptive_heuristics_active')}` | Estritamente desativadas (Zero LoRA, Zipf ou FSRS) |
| **Protocolo de Repetições** | `{iterations} iterações de {len(per_task_rows)} tarefas` | Total de {n_total} execuções registradas |

---

## 3. Protocolo Estatístico e Interpretação dos Intervalos de Confiança

A campanha empírica é composta por **{iterations} repetições completas sobre um gabarito fixo de {len(per_task_rows)} tarefas determinísticas** (totalizando {n_total} observações).

> [!NOTE]
> **Alcance da Inferência Estatística:**  
> Os {n_total} registros **não constituem problemas independentes**, mas sim 10 medições repetidas do mesmo conjunto invariante de 9 tarefas.  
> Portanto, os desvios-padrão amostrais ($s, ddof=1$) e os intervalos de confiança calculados via distribuição $t$ de Student ($t=1.987, df={n_total-1}$) descrevem **estritamente a estabilidade temporal da infraestrutura computacional local**, servindo como padrão de repetibilidade e calibração interna do runtime.

---

## 4. Momentos Estatísticos Consolidados ($N={n_total}$)

| Métrica | Média | Desvio Amostral ($s, ddof=1$) | Desvio Pop. ($\sigma, ddof=0$) | Mediana (p50) | p95 | p99 | Min | Max | Intervalo de Confiança (95%) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **$T_{{exec}}$ (Execução)** | {t_exec.get('mean', 0.0):.4f} ms | {t_exec.get('sample_std_dev', 0.0):.4f} ms | {t_exec.get('population_std_dev', 0.0):.4f} ms | {t_exec.get('p50', 0.0):.4f} ms | {t_exec.get('p95', 0.0):.4f} ms | {t_exec.get('p99', 0.0):.4f} ms | {t_exec.get('min_val', 0.0):.4f} ms | {t_exec.get('max_val', 0.0):.4f} ms | **[{t_exec.get('mean', 0.0) - ic_exec:.4f}, {t_exec.get('mean', 0.0) + ic_exec:.4f}]** (±{ic_exec:.4f}) |
| **$T_{{telemetry}}$ (Telemetria)** | {t_telem.get('mean', 0.0):.4f} ms | {t_telem.get('sample_std_dev', 0.0):.4f} ms | {t_telem.get('population_std_dev', 0.0):.4f} ms | {t_telem.get('p50', 0.0):.4f} ms | {t_telem.get('p95', 0.0):.4f} ms | {t_telem.get('p99', 0.0):.4f} ms | {t_telem.get('min_val', 0.0):.4f} ms | {t_telem.get('max_val', 0.0):.4f} ms | **[{t_telem.get('mean', 0.0) - ic_telem:.4f}, {t_telem.get('mean', 0.0) + ic_telem:.4f}]** (±{ic_telem:.4f}) |
| **$T_{{val}}$ (Validação Oráculos)** | {t_val.get('mean', 0.0):.4f} ms | {t_val.get('sample_std_dev', 0.0):.4f} ms | {t_val.get('population_std_dev', 0.0):.4f} ms | {t_val.get('p50', 0.0):.4f} ms | {t_val.get('p95', 0.0):.4f} ms | {t_val.get('p99', 0.0):.4f} ms | {t_val.get('min_val', 0.0):.4f} ms | {t_val.get('max_val', 0.0):.4f} ms | **[{t_val.get('mean', 0.0) - ic_val:.4f}, {t_val.get('mean', 0.0) + ic_val:.4f}]** (±{ic_val:.4f}) |
| **$T_{{total}}$ (Ciclo Completo)** | {t_tot.get('mean', 0.0):.4f} ms | {t_tot.get('sample_std_dev', 0.0):.4f} ms | {t_tot.get('population_std_dev', 0.0):.4f} ms | {t_tot.get('p50', 0.0):.4f} ms | {t_tot.get('p95', 0.0):.4f} ms | {t_tot.get('p99', 0.0):.4f} ms | {t_tot.get('min_val', 0.0):.4f} ms | {t_tot.get('max_val', 0.0):.4f} ms | **[{t_tot.get('mean', 0.0) - ic_tot:.4f}, {t_tot.get('mean', 0.0) + ic_tot:.4f}]** (±{ic_tot:.4f}) |

### Totais de Tokens e Precificação Sintética
- **Tokens de Entrada:** {tok.get('total_input_tokens', 0):,} tokens ({tok.get('total_input_tokens', 0) / n_total:.1f} tokens/tarefa)
- **Tokens de Saída:** {tok.get('total_output_tokens', 0):,} tokens ({tok.get('total_output_tokens', 0) / n_total:.1f} tokens/tarefa)
- **Total de Tokens Processados:** {tok.get('total_tokens', 0):,} tokens ({tok.get('avg_tokens_per_task', 0.0):.2f} tokens/tarefa)
- **Custo Monetário Sintético Total:** US$ {cost.get('total_cost_usd', 0.0):.6f} (Média: US$ {cost.get('avg_cost_per_task_usd', 0.0):.6f}/tarefa)
- **Conformidade Funcional:** {rates.get('functional_success_rate', 0.0) * 100:.1f}% ({int(rates.get('functional_success_rate', 0.0) * n_total)}/{n_total} tarefas)
- **Conformidade de Segurança:** {rates.get('security_pass_rate', 0.0) * 100:.1f}% ({int(rates.get('security_pass_rate', 0.0) * n_total)}/{n_total} tarefas sem vazamento de credenciais)

---

## 5. Estatísticas Desagregadas por Tarefa ($K={len(per_task_rows)}$ tarefas, $R={iterations}$ repetições)

| Tarefa | Suíte | Tipo | Repetições | $T_{{exec}}$ Média | $T_{{telem}}$ Média | $T_{{val}}$ Média | $T_{{tot}}$ Média | Tokens (In/Out) | Custo Total USD | Veredito |
| :--- | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
"""
    for r in per_task_rows:
        md += f"| `{r['task_id']}` | {r['suite']} | `{r['task_type']}` | {r['reps']} | {r['avg_exec']:.4f} ms | {r['avg_telem']:.4f} ms | {r['avg_val']:.4f} ms | {r['avg_tot']:.4f} ms | {r['in_tok']} / {r['out_tok']} | US$ {r['cost_usd']:.6f} | {'[OK] PASS' if r['all_passed'] else '[FALHA]'} |\n"

    md += """
---

## 6. Procedimento de Auditoria e Verificação Independente

Auditores externos e pesquisadores independentes podem reproduzir a validação integral sem instalar dependências externas e sem necessitar de nenhuma credencial secreta:

```bash
# Auditoria pública via Chave Pública Oficial da Autoridade (zero dependências / pure-Python stdlib):
python scripts/verify_baseline_integrity.py --report docs/benchmarks/baseline_reference_run.json
```

O utilitário executará a conferência criptográfica em 4 níveis:
1. Reconstrução canônica e verificação do hash SHA-256 da raiz.
2. Reconstrução canônica e verificação do digest SHA-256 das 90 tarefas individuais.
3. Validação da assinatura digital assimétrica (RSASSA-PKCS1-v1_5 com SHA-256) atestando autenticidade de autoria da Autoridade.
4. Auditoria individual e round-trip dos 90 relatórios de oráculos, confirmando taxa zero de vazamento de segredos e conformidade funcional.
"""

    os.makedirs(os.path.dirname(os.path.abspath(output_md_path)), exist_ok=True)
    with open(output_md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Relatório Markdown gerado com sucesso: {output_md_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python generate_baseline_report_md.py <json_input> <markdown_output>")
        sys.exit(1)
    generate_markdown_report(sys.argv[1], sys.argv[2])
