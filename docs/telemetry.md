# Telemetria Cognitiva e Rastreabilidade Experimental (Fase 4C.0.1)

Este documento especifica a infraestrutura de telemetria cognitiva, contabilidade de tokens, latências decompostas, custos de inferência e garantias de proveniência de dados implementada no AI Coordinator para viabilizar as baselines experimentais da Fase 4C.

---

## 1. Princípios Arquiteturais e Não-Interferência

A instrumentação da Fase 4C obedece rigorosamente aos seguintes princípios:

1. **Passividade e Não-Interferência:** A coleta de telemetria é estritamente observacional. Ela **não** altera o fluxo de execução dos agentes, decisões de roteamento, geração de texto, invariantes ou políticas de segurança.
2. **Resiliência a Falhas:** Falhas de persistência ou parsing de telemetria são tratadas de forma não-bloqueante (`safe_record_telemetry`), nunca propagando exceções que interrompam a execução do supervisor ou do worker.
3. **Imutabilidade e Rastreabilidade:** Cada registro é persistido com vínculos inequívocos a `task_id`, `run_id`, identidade do worker, PID do processo, revisão de planejamento (`plan_revision`), provedor e modelo.
4. **Sanitização Obrigatória:** Credenciais sensíveis (chaves de API `sk-*`, `ghp_*`, tokens bearer, senhas e credenciais) presentes em mensagens de erro ou metadados são automaticamente expurgadas (`[REDACTED_SECRET]`) antes de qualquer persistência no banco.

---

## 2. Proveniência Estrita de Dados (`MetricProvenance`)

Para conformidade com o rigor de pesquisa científica, **nenhuma métrica pode ser inventada ou simulada disfarçada de medição real**. Cada dimensão contábil possui classificação mandatória de proveniência:

| Proveniência | Definição | Exemplo de Aplicação |
| :--- | :--- | :--- |
| `measured` | Medição direta e objetiva obtida da API do provedor ou do relógio do sistema operacional. | `usage.prompt_tokens` retornado pelo provedor; `time.perf_counter()` para duração total e TTFT. |
| `estimated` | Valor calculado deterministicamente pelo coordenador a partir de fórmula ou tabela pública declarada. | Custo monetário calculado multiplicando tokens por tabela de preços (`PricingRegistry`); estimativa de tokens via tokenizer. |
| `unknown` | Dado não reportado pelo provedor e não calculável com precisão. | `cache_read_tokens` em endpoints que não oferecem suporte ou métrica de cache; modelo sem tabela de preços registrada. |

> [!IMPORTANT]
> Se um provedor ou endpoint não informar dados de cache ou TTFT, o valor **deve** permanecer `None` ou com proveniência `unknown`. É expressamente proibido preencher métricas inexistentes com estimativas não identificadas.

---

## 3. Modelo de Dados e Esquema no SQLite

A tabela `cognitive_telemetry` armazena os registros no banco de dados SQLite (`coordination.sqlite`):

```sql
CREATE TABLE IF NOT EXISTS cognitive_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id TEXT,
    worker_identity TEXT,
    worker_pid INTEGER,
    plan_revision INTEGER,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    tokens_provenance TEXT NOT NULL,
    ttft_ms REAL,
    generation_duration_ms REAL,
    tool_duration_ms REAL,
    supervisor_overhead_ms REAL,
    total_duration_ms REAL NOT NULL,
    latency_provenance TEXT NOT NULL,
    total_cost_usd REAL,
    cost_provenance TEXT NOT NULL,
    tool_calls_count INTEGER DEFAULT 0,
    retries_count INTEGER DEFAULT 0,
    stream_chunks_count INTEGER DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1,
    error_category TEXT,
    error_message TEXT,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cognitive_telemetry_task ON cognitive_telemetry(task_id);
CREATE INDEX IF NOT EXISTS idx_cognitive_telemetry_run ON cognitive_telemetry(run_id);
CREATE INDEX IF NOT EXISTS idx_cognitive_telemetry_model ON cognitive_telemetry(model_name);
CREATE INDEX IF NOT EXISTS idx_cognitive_telemetry_created ON cognitive_telemetry(created_at);
```

---

## 4. Decomposição de Métricas

### 4.1 Contabilidade de Tokens (`TokenMetrics`)
- `input_tokens`: Tokens de entrada (prompt).
- `output_tokens`: Tokens gerados (completion/saída).
- `cache_read_tokens`: Tokens recuperados de cache de contexto prévio.
- `cache_creation_tokens`: Tokens adicionados à janela de cache de contexto.
- `total_tokens`: Total cumulativo de tokens processados.
- `provenance`: `measured` | `estimated` | `unknown`.

### 4.2 Decomposição Temporal e Latências (`LatencyMetrics`)
Todas as grandezas temporais são expressas em **milissegundos (ms)** com temporizador de alta precisão (`time.perf_counter()`):
- `ttft_ms`: *Time to First Token* — tempo transcorrido desde a emissão da requisição até a chegada do primeiro fragmento de streaming.
- `generation_duration_ms`: Tempo efetivo de inferência pelo modelo (tempo total subtraído das durações de ferramentas e do overhead).
- `tool_duration_ms`: Tempo cumulativo gasto na execução de ferramentas externas via Tool Broker.
- `supervisor_overhead_ms`: Tempo de processamento do supervisor (auditoria, validação de invariantes, serialização e persistência).
- `total_duration_ms`: Duração ponta a ponta da invocação.
- `provenance`: `measured` | `estimated` | `unknown`.

### 4.3 Custos Monetários e Tabela de Referência (`PricingRegistry`)
Custos são expressos em USD por 1 milhão de tokens (1M tokens). O cálculo utiliza a classe `PricingRegistry`:

| Modelo / Família | Input / 1M | Output / 1M | Cache Read / 1M | Cache Creation / 1M |
| :--- | :--- | :--- | :--- | :--- |
| `claude-3-5-sonnet` | $3.00 | $15.00 | $0.30 | $3.75 |
| `claude-3-opus` | $15.00 | $75.00 | $1.50 | $18.75 |
| `claude-3-5-haiku` | $1.00 | $5.00 | $0.10 | $1.25 |
| `claude-3-haiku` | $0.25 | $1.25 | $0.025 | $0.30 |
| `gpt-4o` | $2.50 | $10.00 | $1.25 | $0.00 |
| `gpt-4o-mini` | $0.15 | $0.60 | $0.075 | $0.00 |
| `o1` | $15.00 | $60.00 | $7.50 | $0.00 |
| `o1-mini` | $3.00 | $12.00 | $1.50 | $0.00 |
| `gemini-1.5-pro` | $3.50 | $10.50 | $0.875 | $0.00 |
| `gemini-1.5-flash` | $0.075 | $0.30 | $0.01875 | $0.00 |
| `gemini-2.0-flash` | $0.10 | $0.40 | $0.025 | $0.00 |

Modelos personalizados ou locais podem ser registrados dinamicamente via `PricingRegistry.register_pricing(name, input_per_m, output_per_m, ...)`. Se um modelo não constar na tabela de preços, o custo é registrado como `None` e a proveniência como `unknown`.

---

## 5. Agregações Estatísticas (`TelemetryAggregate`)

A função `compute_telemetry_aggregate(records)` e o método `Store.aggregate_cognitive_telemetry(...)` calculam resumos analíticos reproduzíveis:
- **Totais:** `record_count`, `total_tokens`, `total_cost_usd`, `total_duration_ms`.
- **Percentis de Latência:** p50, p90, p95 e p99 da duração total.
- **Médias Decompostas:** `avg_ttft_ms`, `avg_generation_duration_ms`, `avg_tool_duration_ms`, `avg_supervisor_overhead_ms`.
- **Segregação por Proveniência:** Dicionários contendo a soma de tokens e custos segmentados por `measured`, `estimated` e `unknown`.
- **Qualidade Operacional:** `success_rate`, contagem de sucessos/falhas e detalhamento de erros por categoria (`error_breakdown`).

---

## 6. Exemplos de Uso

### 6.1 Coleta via Context Manager (`TelemetryCollector`)
```python
from ai_coordinator import TelemetryCollector, MetricProvenance, safe_record_telemetry

collector = TelemetryCollector(
    task_id="task_4c_001",
    run_id="run_01",
    worker_identity="agent_worker",
    worker_pid=12345,
    plan_revision=1,
    provider="anthropic",
    model_name="claude-3-5-sonnet",
)

with collector:
    # 1. Requisição de streaming
    # ao chegar o primeiro chunk:
    collector.mark_first_token()
    
    # 2. Execução de ferramentas mediada
    with collector.measure_tool():
        # executar ferramenta externa
        pass

    # 3. Registrar tokens reportados pelo provedor
    collector.set_tokens(
        input_tokens=1500,
        output_tokens=300,
        cache_read_tokens=1000,
        provenance=MetricProvenance.MEASURED,
    )

record = collector.finalize(success=True)
safe_record_telemetry(store, record)
```

### 6.2 Consulta e Agregação Estatística
```python
# Consulta com paginação e filtros
records = store.query_cognitive_telemetry(
    task_id="task_4c_001",
    model_name="claude-3-5-sonnet",
    limit=50,
)

# Agregação estatística consolidada
aggregate = store.aggregate_cognitive_telemetry(task_id="task_4c_001")
print(f"Sucesso: {aggregate.success_rate * 100:.1f}%")
print(f"p50: {aggregate.p50_duration_ms}ms, p95: {aggregate.p95_duration_ms}ms")
print(f"Custo total: ${aggregate.total_cost_usd:.6f}")
```

---

## 7. Limitações Metodológicas

1. **Top-P, Top-K, Heaps, Zipf e FSRS:** Estão deliberadamente desabilitados nesta subetapa (4C.0.1). O objetivo desta entrega é unicamente a instrumentação e captura fidedigna de métricas.
2. **Ambiente Local e Virtualização:** Variações nas latências de rede e CPU local podem influenciar `generation_duration_ms`. Para a construção da baseline (4C.0.4), devem ser computadas médias sobre múltiplas execuções com intervalos de confiança.
