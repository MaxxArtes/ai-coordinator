# Relatório Científico da Linha de Base — AI Coordinator v0.5.1 (Fase 4C.0.5)

> **Status do Documento:** Candidato Preliminar de Staging (Não promovido a definitivo até homologação final da auditoria)  
> **Data de Execução:** 4 de novembro de 2026  
> **Branch de Desenvolvimento:** `feature/phase4c-0-baseline`  
> **Commit de Referência:** [`a0f6abfdb9ab725c97e5f736303c67d5f4023121`](https://github.com/MaxxArtes/ai-coordinator/commit/a0f6abfdb9ab725c97e5f736303c67d5f4023121)  
> **Referência Estável de Produção:** `v0.5.1` (Commit [`a2f8f65`](https://github.com/MaxxArtes/ai-coordinator/commit/a2f8f65dbb470d0acba853b5036e6d9af841b0e5))  
> **Runtime Central:** `C:\Users\10864\.local\share\ai-coordinator-runtime` (`0.4.0+local.1` — Estritamente preservado)  
> **Coordenador Central:** `D:\ai-memory\coordinator` (Tarefa `c220d508596e42d4`)  

---

## 1. Proveniência e Metadados do Experimento

| Metadado | Valor Registrado no Manifesto Canônico | Classificação |
| :--- | :--- | :--- |
| **Run ID** | `baseline_run_1793830314` | Identificador único de campanha |
| **Commit Git** | `a0f6abfdb9ab725c97e5f736303c67d5f4023121` | Hash de proveniência rastreável |
| **Branch Git** | `feature/phase4c-0-baseline` | Isolamento em branch de pesquisa |
| **Estado do Repositório (`git_dirty`)** | `False` (limpo no momento da execução oficial) | Imutabilidade do workspace |
| **Versão do Pacote** | `0.5.1` | Baseline da release v0.5.1 |
| **Versão do Dataset** | `2026.11.03` | Especificação congelada das 9 tarefas |
| **Versão da Tabela de Preços** | `2026.11.01` | Matriz determinística offline |
| **Runtime Python** | `3.14.0rc2` | Ambiente de referência |
| **Plataforma / Arquitetura** | `Windows 11 (AMD64)` | Runner local controlado |
| **Otimizações Adaptativas Ativas** | `False` | Zero Top-P, Zipf, Heaps, FSRS ou LoRA |
| **Chamadas a APIs Tarifadas** | `0` (Zero — 100% Offline) | Custo financeiro externo nulo |

---

## 2. Metodologia Experimental e Protocolo de Execução

### 2.1 Justificativa Amostral e Ciclo de Aquecimento (Warmup)
Para neutralizar ruídos transientes de *cold start*, alocação dinâmica de páginas do sistema operacional e compilação de bytecode Python:
1. **Ciclo de Aquecimento (Warmup):** Uma iteração preliminar de 9 tarefas ($N_{warmup} = 9$) foi executada sobre uma conexão descartável SQLite em memória/WAL. O tempo médio de ciclo no aquecimento foi de $1.48 \text{ ms}$, estabilizando caches de descritores de arquivo e tabelas hash.
2. **Campanha Oficial:** Executada imediatamente após o aquecimento, compreendendo **10 iterações completas** sobre as 9 tarefas das Suítes A (3), B (3) e C (3), totalizando uma amostra de **$N = 90$ execuções de tarefas**.
3. **Poder Estatístico:** Com $N = 90$ ($df = 89$), o erro padrão da média decresce com fator $1/\sqrt{90} \approx 0.1054$, viabilizando intervalos de confiança de 95% estreitos ($t_{0.025; 89} \approx 1.987$) de ordem sub-milissegundo.

### 2.2 Classificação Estrita de Métricas
Em conformidade com as diretrizes de integridade científica da auditoria:
- **Medidas (`MEASURED`):** Duração de execução do solver ($T_{exec}$), overhead computacional da telemetria ($T_{telemetry}$), tempo de validação dos oráculos ($T_{val}$) e duração total ($T_{total}$), aferidos pelo relógio monotônico de alta precisão `time.perf_counter()`.
- **Estimadas (`ESTIMATED`):** Volume de tokens de entrada e saída, calculados deterministicamente pelo comprimento dos payloads JSON normalizados ($4 \text{ caracteres} \approx 1 \text{ token}$). **Não constituem medição física de consumo de APIs remotas**.
- **Sintéticas (`ESTIMATED / SYNTHETIC`):** Custos em dólares (USD), derivados da aplicação estática da matriz de precificação offline do `PricingRegistry` sobre os tokens estimados.

---

## 3. Resultados Estatísticos Consolidados ($N = 90$ Tarefas)

### 3.1 Distribuição Temporal e Segregação de Fases (em Milissegundos)

| Fase Temporal | Símbolo | Média ($\mu$) | IC 95% ($\pm t \cdot \frac{s}{\sqrt{N}}$) | Desvio Amostral ($s$, $ddof=1$) | Desvio Populacional ($\sigma$, $ddof=0$) | Mediana (p50) | Percentil 95 (p95) | Percentil 99 (p99) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Execução Pura do Solver** | $T_{exec}$ | **$0.0148 \text{ ms}$** | $\pm 0.0031 \text{ ms}$ | $0.0147 \text{ ms}$ | $0.0146 \text{ ms}$ | $0.0120 \text{ ms}$ | $0.0321 \text{ ms}$ | $0.1057 \text{ ms}$ |
| **Overhead da Telemetria** | $T_{telemetry}$ | **$0.5616 \text{ ms}$** | $\pm 0.0228 \text{ ms}$ | $0.1090 \text{ ms}$ | $0.1084 \text{ ms}$ | $0.5460 \text{ ms}$ | $0.7311 \text{ ms}$ | $1.0659 \text{ ms}$ |
| **Validação dos Oráculos** | $T_{val}$ | **$0.9700 \text{ ms}$** | $\pm 0.0519 \text{ ms}$ | $0.2479 \text{ ms}$ | $0.2466 \text{ ms}$ | $0.9332 \text{ ms}$ | $1.7220 \text{ ms}$ | $1.8997 \text{ ms}$ |
| **Duração Total Transcorrida** | $T_{total}$ | **$1.5463 \text{ ms}$** | $\pm 0.0670 \text{ ms}$ | $0.3198 \text{ ms}$ | $0.3180 \text{ ms}$ | $1.5120 \text{ ms}$ | $2.2979 \text{ ms}$ | $2.8517 \text{ ms}$ |

> [!NOTE]
> A segregação comprova que o custo de execução algorítmica pura ($T_{exec}$) é de apenas $0.015 \text{ ms}$. A validação profunda de oráculos triplos consome $0.97 \text{ ms}$ ($62.7\%$ do ciclo), e a instrumentação observacional da telemetria SQLite em WAL consome $0.56 \text{ ms}$ ($36.3\%$). O overhead total médio por ciclo de tarefa é rigorosamente sub-2ms.

### 3.2 Taxas de Conformidade e Vereditos dos Oráculos

| Métrica de Conformidade | Taxa Homologada ($N = 90$) | Contagem Absoluta | Critério do Oráculo |
| :--- | :--- | :--- | :--- |
| **`passed_all_rate`** | **$100.00\%$** | $90 / 90$ | Aprovação simultânea funcional, de eficiência e segurança plena |
| **`functional_success_rate`** | **$100.00\%$** | $90 / 90$ | Correspondência estrita de hash SHA-256 de saída vs. gabarito |
| **`efficiency_success_rate`** | **$100.00\%$** | $90 / 90$ | Respeito estrito aos limites de tokens, custo e latência |
| **`security_pass_rate`** | **$100.00\%$** | $90 / 90$ | Zero vazamentos de credenciais e imutabilidade SQLite comprovada |
| **`security_unverified_rate`** | **$0.00\%$** | $0 / 90$ | Nenhuma tarefa com verificação omitida ou store nulo |
| **`security_fail_rate`** | **$0.00\%$** | $0 / 90$ | Zero violações de segurança |

### 3.3 Agregados de Tokens e Custos Sintéticos

- **Total de Tokens Estimados:** $18,060 \text{ tokens}$ (Média: $200.67 \text{ tokens/tarefa}$).
  - Tokens de Entrada: $12,740 \text{ tokens}$ (Média: $141.56 \text{ tokens/tarefa}$).
  - Tokens de Saída: $5,320 \text{ tokens}$ (Média: $59.11 \text{ tokens/tarefa}$).
  - Proveniência: `MetricProvenance.ESTIMATED`.
- **Custo Sintético Projetado:** $\$0.023240 \text{ USD}$ (Média: $\$0.000258 \text{ USD/tarefa}$).
  - Proveniência: `MetricProvenance.ESTIMATED / SYNTHETIC`.

---

## 4. Prova Criptográfica de Integridade e Autenticidade

O artefato [`docs/benchmarks/baseline_reference_run.json`](file:///D:/ai-coordinator/docs/benchmarks/baseline_reference_run.json) foi autenticado hierarquicamente:

```text
===========================================================
 AI Coordinator — Verificador Independente de Baseline
===========================================================
  Arquivo:          docs/benchmarks/baseline_reference_run.json
  Modo:             AUTENTICADO (HMAC-SHA256)
  Run ID:           baseline_run_1793830314
  Git Commit:       a0f6abfdb9ab...
  Git Branch:       feature/phase4c-0-baseline (dirty=False)
  Tarefas:          90 (10 iterações, 9 tarefas/iteração)
  Dataset Versão:   2026.11.03
  Pricing Versão:   2026.11.01
-----------------------------------------------------------
  [1/4] Hash de Integridade Raiz:        [OK] VÁLIDO
  [2/4] Digest Canônico das Tarefas:     [OK] VÁLIDO
  [3/4] Assinatura HMAC Raiz:            [OK] AUTÊNTICA
  [4/4] Verificação Profunda Completa:    [OK] APROVADA
  [CROSS-CHECK] Validador Interno:       [OK] CONCORDANTE
===========================================================
```

### Âncoras Criptográficas
- **Hash SHA-256 do Manifesto Raiz:**  
  `5af407c3b08e46feb32aa5b715e55619f9f6b96d626b15b6fc622366e1f04f3a`
- **Digest Determinístico das 90 Tarefas (`task_results_digest`):**  
  `6bd966ef9a16e36e03fd5686c3c9f6d56e47a9630693609fa3a323b319350b2c`
- **Assinatura HMAC-SHA256 da Raiz:**  
  `504fdb353b86236252134a394bc6724bdad14c578409fbee583aef4a6a4d6647`

### Procedimento de Auditoria Independente Desacoplada
Qualquer auditor ou nó par pode verificar a autenticidade e a integridade de forma 100% autônoma, sem instalar dependências externas:

```bash
# Verificação de integridade estrutural (sem expor segredos):
python scripts/verify_baseline_integrity.py --report docs/benchmarks/baseline_reference_run.json

# Verificação criptográfica de autenticidade (via variável de ambiente segura):
export AI_COORDINATOR_HMAC_KEY="ai-coordinator-baseline-v0.5.1-reference-secret-2026"
python scripts/verify_baseline_integrity.py --report docs/benchmarks/baseline_reference_run.json --key-env AI_COORDINATOR_HMAC_KEY --cross-check
```

---

## 5. Limitações Conhecidas e Fronteiras Experimentais

1. **Solvers Algorítmicos Puros:** Os resultados de $T_{exec}$ refletem a velocidade de execução do código Python local (`CanonicalSolverRegistry`), e não a latência de geração de um modelo de linguagem real.
2. **Ausência de Estocasticidade de Rede:** Por operar 100% offline, os tempos não sofrem variações de jitter de conectividade, filas em nuvem ou rate limits de provedores.
3. **Ponto de Partida Imutável:** Este relatório e o arquivo `baseline_reference_run.json` servem unicamente como **ponto zero de calibração** (*zero-point baseline*). O ganho das futuras otimizações cognitivas adaptativas (Fases 4C.1 a 4C.4 — Zipf, Heaps, FSRS, LoRA) será medido pela variação percentual relativa sobre esta base imutável.
4. **Status de Governança:** O arquivo `docs/benchmarks/baseline_reference_run.json` encontra-se em ambiente de staging e **não deve ser promovido a arquivo de produção final** até a homologação formal expressa pelo auditor independente.
