# Relatório Científico da Linha de Base — AI Coordinator v0.5.1

> **Status:** Linha de Base de Referência Sintética (Staging) — Pronta para Homologação  
> **Identificador da Execução (Run ID):** `baseline_run_1793886354`  
> **Data de Execução:** `2026-11-05T13:45:54.953835+00:00`  
> **Hash de Integridade Raiz (SHA-256):** `399fc5c4fc77167fb64166b2adef6facb26c351682131e38fa8676de3cb6ef91`  
> **Digest Canônico das Tarefas (SHA-256):** `25490d8db589e32b38cd07a7cb87ffd86b4e3ce82156b6798172392224eaf8a0`  
> **Assinatura Digital Pública:** `19a8bc228dc4d5b439f41b66013dcd7857928b4a0f38a89daff79f1428b4bcb460e95dc8b086082eeea7906698b884c84a1f9b50459beb9e13c7a5228b236250ec96b49db1a5354593acc7231257455cf05450caa89f1ad9afc629b5adbf0a37a040747650d3c39327df281423f53df7adde801312659a507ff51d7f6e528821004c235f67b7a1bcca4dc11da5cf25ce6a1b866b9bd2fef9e731641eb5e9fa279a44a03890518484186138ef507ff739842ccc722507862740261054a30419060508b12c6f34c65745338d98e0226b022bd3235daac2edc39454dada66080b6f1a9b879ebff26cc4d4c36510d8a7830fcf4dbdba440e088859fbd7ab66f86009`  
> **Algoritmo de Assinatura:** `RSASSA-PKCS1-v1_5-SHA256`  
> **Fingerprint da Chave Pública da Autoridade:** `f0f3d377306e00092daf6dff94826e69d24c1174bc4b32ab81086419f4c995c6`  

---

## 1. Escopo e Classificação da Linha de Base

> [!IMPORTANT]
> **Natureza Científica da Medição:**  
> Esta referência é uma **baseline sintética offline de calibração** dos componentes locais do coordenador (oráculos determinísticos, instrumentação e armazenamento SQLite WAL).  
> - As durações temporais ($T_{exec}$, $T_{telemetry}$, $T_{val}$, $T_{total}$) são medições computacionais locais e determinísticas em código Python executado pelo interpretador oficial.  
> - Os valores de tokens (18,060 tokens totais) e custos (US$ 0.023240) são **estimativas sintéticas controladas** derivadas das tabelas do `PricingRegistry`, e **NÃO representam consumo real de APIs ou modelos de linguagem remotos**.  
> - Zero chamadas de rede ou APIs proprietárias foram consumidas durante a campanha.

---

## 2. Metadados do Ambiente e Proveniência Criptográfica

| Parâmetro | Valor Registrado | Observações |
| :--- | :--- | :--- |
| **Versão do AI Coordinator** | `0.5.1` | Núcleo base sem dependências externas |
| **Branch Git** | `feature/phase4c-0-baseline` | Branch oficial da Fase 4C |
| **Commit Git** | `9e7265b90e3af6562b2c85b08c9fbceee217e34f` | SHA-1 canônico de proveniência |
| **Estado da Árvore (git_dirty)** | `False` | Execução em árvore limpa e reprodutível |
| **Versão Python** | `3.14.5` | Interpretador do ambiente de execução |
| **Sistema Operacional** | `Windows 11 (AMD64)` | Plataforma anfitriã |
| **Versão do Dataset Sintético** | `2026.11.03` | Versão imutável das Suítes A, B e C |
| **Versão do PricingRegistry** | `2026.11.01` | Catálogo de precificação fixo |
| **Heurísticas Adaptativas Ativas** | `False` | Estritamente desativadas (Zero LoRA, Zipf ou FSRS) |
| **Protocolo de Repetições** | `10 iterações de 9 tarefas` | Total de 90 execuções registradas |

---

## 3. Protocolo Estatístico e Interpretação dos Intervalos de Confiança

A campanha empírica é composta por **10 repetições completas sobre um gabarito fixo de 9 tarefas determinísticas** (totalizando 90 observações).

> [!NOTE]
> **Alcance da Inferência Estatística:**  
> Os 90 registros **não constituem problemas independentes**, mas sim 10 medições repetidas do mesmo conjunto invariante de 9 tarefas.  
> Portanto, os desvios-padrão amostrais ($s, ddof=1$) e os intervalos de confiança calculados via distribuição $t$ de Student ($t=1.987, df=89$) descrevem **estritamente a estabilidade temporal da infraestrutura computacional local**, servindo como padrão de repetibilidade e calibração interna do runtime.

---

## 4. Momentos Estatísticos Consolidados ($N=90$)

| Métrica | Média | Desvio Amostral ($s, ddof=1$) | Desvio Pop. ($\sigma, ddof=0$) | Mediana (p50) | p95 | p99 | Min | Max | Intervalo de Confiança (95%) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **$T_{exec}$ (Execução)** | 0.0132 ms | 0.0111 ms | 0.0110 ms | 0.0113 ms | 0.0378 ms | 0.0574 ms | 0.0016 ms | 0.0574 ms | **[0.0109, 0.0155]** (±0.0023) |
| **$T_{telemetry}$ (Telemetria)** | 0.5898 ms | 0.1782 ms | 0.1772 ms | 0.5364 ms | 0.9621 ms | 1.2234 ms | 0.3993 ms | 1.2234 ms | **[0.5525, 0.6271]** (±0.0373) |
| **$T_{val}$ (Validação Oráculos)** | 1.0219 ms | 0.5800 ms | 0.5767 ms | 0.8954 ms | 1.5347 ms | 5.6169 ms | 0.6589 ms | 5.6169 ms | **[0.9004, 1.1434]** (±0.1215) |
| **$T_{total}$ (Ciclo Completo)** | 1.6249 ms | 0.6477 ms | 0.6440 ms | 1.5076 ms | 2.4348 ms | 6.2963 ms | 1.0598 ms | 6.2963 ms | **[1.4892, 1.7606]** (±0.1357) |

### Totais de Tokens e Precificação Sintética
- **Tokens de Entrada:** 12,880 tokens (143.1 tokens/tarefa)
- **Tokens de Saída:** 5,180 tokens (57.6 tokens/tarefa)
- **Total de Tokens Processados:** 18,060 tokens (200.67 tokens/tarefa)
- **Custo Monetário Sintético Total:** US$ 0.023240 (Média: US$ 0.000258/tarefa)
- **Conformidade Funcional:** 100.0% (90/90 tarefas)
- **Conformidade de Segurança:** 100.0% (90/90 tarefas sem vazamento de credenciais)

---

## 5. Estatísticas Desagregadas por Tarefa ($K=9$ tarefas, $R=10$ repetições)

| Tarefa | Suíte | Tipo | Repetições | $T_{exec}$ Média | $T_{telem}$ Média | $T_{val}$ Média | $T_{tot}$ Média | Tokens (In/Out) | Custo Total USD | Veredito |
| :--- | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `suite_a_01_etl_aggregation` | suite_a_data_engineering | `tabular_etl` | 10 | 0.0226 ms | 0.6040 ms | 1.0857 ms | 1.7122 ms | 457 / 110 | US$ 0.000677 | [OK] PASS |
| `suite_a_02_reconciliation` | suite_a_data_engineering | `ledger_reconciliation` | 10 | 0.0225 ms | 0.5991 ms | 0.9620 ms | 1.5836 ms | 441 / 36 | US$ 0.000513 | [OK] PASS |
| `suite_a_03_schema_invariants` | suite_a_data_engineering | `schema_validation` | 10 | 0.0138 ms | 0.5575 ms | 1.4714 ms | 2.0427 ms | 192 / 141 | US$ 0.000474 | [OK] PASS |
| `suite_b_01_sequential_dag_5_step` | suite_b_multi_hop_dag | `sequential_pipeline` | 10 | 0.0051 ms | 0.5870 ms | 1.0849 ms | 1.6769 ms | 21 / 66 | US$ 0.000153 | [OK] PASS |
| `suite_b_02_fork_join_parallel` | suite_b_multi_hop_dag | `fork_join` | 10 | 0.0066 ms | 0.6628 ms | 0.9970 ms | 1.6664 ms | 35 / 30 | US$ 0.000095 | [OK] PASS |
| `suite_b_03_dependency_fencing` | suite_b_multi_hop_dag | `dependency_resolution` | 10 | 0.0220 ms | 0.6224 ms | 0.8613 ms | 1.5057 ms | 19 / 24 | US$ 0.000067 | [OK] PASS |
| `suite_c_01_adversarial_secret_injection` | suite_c_adversarial | `credential_sanitization_defense` | 10 | 0.0221 ms | 0.5044 ms | 0.9815 ms | 1.5080 ms | 78 / 59 | US$ 0.000196 | [OK] PASS |
| `suite_c_02_malformed_payload_recovery` | suite_c_adversarial | `fault_recovery` | 10 | 0.0024 ms | 0.6160 ms | 0.8933 ms | 1.5117 ms | 31 / 27 | US$ 0.000085 | [OK] PASS |
| `suite_c_03_budget_overrun_fencing` | suite_c_adversarial | `budget_fencing` | 10 | 0.0018 ms | 0.5545 ms | 0.8603 ms | 1.4167 ms | 14 / 25 | US$ 0.000064 | [OK] PASS |

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
