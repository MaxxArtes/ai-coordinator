# Suíte de Benchmarks Sintéticos e Oráculos de Avaliação (Fase 4C.0.2)

Este documento especifica a infraestrutura de datasets sintéticos determinísticos e oráculos de avaliação tridimensionais (Funcional, Eficiência e Segurança) implementada no AI Coordinator para viabilizar experimentos científicos rigorosos, reproduzíveis e 100% offline da Fase 4C.

---

## 1. Princípios Arquiteturais e Garantias Metodológicas

A infraestrutura da Subetapa 4C.0.2 atende rigorosamente às diretrizes de governança:

1. **Reprodutibilidade Determinística Bit a Bit:** Todos os datasets e tarefas são gerados proceduralmente a partir de sementes pseudo-aleatórias registradas (`Random(seed)`), garantindo saídas e hashes SHA-256 canônicos idênticos em qualquer sistema operacional (Windows, Ubuntu, macOS).
2. **Custo Financeiro e de Rede Zero:** Toda a suíte opera exclusivamente sobre geradores sintéticos e modelos de teste locais (`mock providers`), sem invocar APIs pagas ou expor tráfego de rede.
3. **Isolamento de Dados e Não-Interferência:** Nenhum dado confidencial ou real é consumido. O runtime central de produção em `C:\Users\10864\.local\share\ai-coordinator-runtime` permanece intacto na versão estável `0.4.0+local.1`.
4. **Ausência de Otimizações Precoces:** Top-P adaptativo, Zipf, Heaps, FSRS e LoRA permanecem desabilitados e fora do escopo desta subetapa de infraestrutura.

---

## 2. Taxonomia das Suítes de Tarefas Sintéticas

O módulo `SyntheticDatasetGenerator` implementa três suítes complementares:

### 2.1 Suíte A — Engenharia de Dados Estruturada (`suite_a_data_engineering`)
- **Semente Canônica:** `DEFAULT_SEED_SUITE_A = 4242`
- **Tarefas:**
  1. `suite_a_01_etl_aggregation`: Agregação analítica e sumarização de transações brutas por categoria de serviço (volume total em centavos, contagem e ticket médio).
  2. `suite_a_02_reconciliation`: Reconciliação contábil estrita baseada em partidas dobradas (*double-entry bookkeeping*), com conservação exata de balanço e fluxo líquido zero (`net_flow == 0`).
  3. `suite_a_03_schema_invariants`: Validação e filtragem de registros violadores contra limites numéricos e domínios permitidos.

### 2.2 Suíte B — Coordenação Multi-Hop e Precedência DAG (`suite_b_multi_hop_dag`)
- **Semente Canônica:** `DEFAULT_SEED_SUITE_B = 8484`
- **Tarefas:**
  1. `suite_b_01_sequential_dag_5_step`: Pipeline linear de 5 etapas (`extract` $\to$ `validate` $\to$ `clean` $\to$ `aggregate` $\to$ `format`), validando rastreabilidade de estado passo a passo.
  2. `suite_b_02_fork_join_parallel`: Decomposição paralela em 4 partições disjuntas seguida por barreira de sincronização e agregação distributiva.
  3. `suite_b_03_dependency_fencing`: Resolução topológica determinística de grafos de dependência e verificação de aciclicidade.

### 2.3 Suíte C — Robustez Adversarial e Injeção de Falhas (`suite_c_adversarial`)
- **Semente Canônica:** `DEFAULT_SEED_SUITE_C = 1212`
- **Tarefas:**
  1. `suite_c_01_adversarial_secret_injection`: Injeção de credenciais sintéticas em **chaves de metadados** (`sk-proj-...`, `ghp_...`), valores (`bearer ...`, `AIza...`) e listas aninhadas, avaliando o expurgo compulsório pelo oráculo de segurança.
  2. `suite_c_02_malformed_payload_recovery`: Injeção de payloads truncados com recuperação graciosa e classificação estrita de erros (`MALFORMED_JSON_PAYLOAD`).
  3. `suite_c_03_budget_overrun_fencing`: Demanda inflada de tokens para validação do bloqueio preventivo de missões (`FlightBudget`).

---

## 3. Oráculos de Avaliação Tridimensionais

O sistema avalia cada execução de forma automatizada e independente do agente testado:

```
                  ┌───────────────────────────────┐
                  │      Candidato Sob Teste      │
                  └──────────────┬────────────────┘
                                 │ Execução & Telemetria
                                 ▼
   ┌─────────────────────────────────────────────────────────────┐
   │             Oráculos de Avaliação Desacoplados              │
   ├───────────────────┬───────────────────┬─────────────────────┤
   │ Oráculo Funcional │ Oráculo Eficiência│  Oráculo Segurança  │
   │  - Schema match   │  - Token Budget   │  - Zero secrets     │
   │  - Canonical hash │  - Cache discount │  - Keys & values    │
   │  - Correctness    │  - Provenance     │  - Imutab. SQLite   │
   └───────────────────┴───────────────────┴─────────────────────┘
                                 │
                                 ▼
                  ┌───────────────────────────────┐
                  │    EvaluationOracleReport     │
                  │ (Assinado criptograficamente) │
                  └───────────────────────────────┘
```

1. **`FunctionalOracle`:**
   - Calcula o hash SHA-256 canônico (`canonical_json_hash`) da saída normalizada **sem expurgo artificial prévio**, garantindo que a equivalência reflita a fidelidade semântica real. Candidatos que emitirem credenciais literais não mascaradas produzirão hashes discrepantes e serão reprovados funcionalmente.
   - Atribui nota 1.0 para correspondência exata ou score ponderado de schema em caso de discrepância.
2. **`EfficiencyOracle`:**
   - Valida limites de tokens e custos configurados na tarefa (`budget_limit_tokens`, `budget_limit_cost_usd`).
   - Rejeita execuções com métricas desconhecidas (`total_tokens is None` ou `total_cost_usd is None`), impedindo falsas aprovações por conversão indevida para zero.
   - Verifica classificação correta de proveniência (`measured`, `estimated`, `unknown`).
   - Audita a aplicação de desconto de cache por provedor, impedindo sobretaxação.
3. **`SecurityOracle`:**
   - Examina a saída original bruta do candidato (`actual_output`) **antes de qualquer transformação ou sanitização**, detectando imediatamente qualquer vazamento de credenciais.
   - Varre logs, erros e metadados procurando credenciais de alta entropia.
   - Testa a imutabilidade do banco SQLite de forma estritamente não-destrutiva, utilizando `SAVEPOINT` com `ROLLBACK` mandatório e exigindo especificamente `sqlite3.IntegrityError` decorrente do trigger `immutable and append-only` (rejeitando erros operacionais espúrios).

---

## 4. Orquestrador Local (`SyntheticBenchmarkRunner`)

O `SyntheticBenchmarkRunner` provê um harness de teste unificado que executa tarefas sintéticas, registra telemetria observacional e consolida relatórios imutáveis (`EvaluationOracleReport`):
- **Proveniência Rigorosa:** Contagens de tokens estimadas deterministicamente por contagem de caracteres são registradas obrigatoriamente como `MetricProvenance.ESTIMATED` (jamais `MEASURED`).
- **Identificador de Integridade vs Assinatura Autenticada:** O relatório expõe separadamente o `integrity_hash` (digest SHA-256 determinístico dos resultados) e a `oracle_signature` (assinatura criptograficamente autenticada via HMAC-SHA256 quando fornecida chave secreta `hmac_key`).
