# Telemetria Cognitiva e Rastreabilidade Experimental (Fase 4C.0.1)

Este documento especifica a infraestrutura de telemetria cognitiva, contabilidade de tokens, latências decompostas, custos de inferência, contratos de cache por provedor e garantias de proveniência de dados implementada no AI Coordinator para viabilizar as baselines experimentais da Fase 4C.

---

## 1. Princípios Arquiteturais e Não-Interferência

A instrumentação da Fase 4C obedece rigorosamente aos seguintes princípios:

1. **Passividade e Não-Interferência:** A coleta de telemetria é estritamente observacional. Ela **não** altera o fluxo de execução dos agentes, decisões de roteamento, geração de texto, invariantes ou políticas de segurança.
2. **Resiliência a Falhas:** Falhas de persistência ou parsing de telemetria são tratadas de forma não-bloqueante (`safe_record_telemetry`), nunca propagando exceções que interrompam a execução do supervisor ou do worker.
3. **Imutabilidade Auditável em Dois Níveis:**
   - **Camada de Código:** Todas as estruturas de dados (`TokenMetrics`, `LatencyMetrics`, `CostMetrics`, `CognitiveTelemetryRecord`, `TelemetryAggregate`) são dataclasses congeladas (`frozen=True`), impedindo mutações pós-criação.
   - **Camada de Banco de Dados:** A tabela `cognitive_telemetry` possui triggers SQLite (`trg_cognitive_telemetry_no_update` e `trg_cognitive_telemetry_no_delete`) que rejeitam tentativas de `UPDATE` ou `DELETE` com `IntegrityError`, garantindo persistência estritamente append-only.
4. **Sanitização Adversarial Recursiva Universal em Chaves e Valores:** Credenciais e segredos (`sk-*`, `ghp_*`, `github_pat_*`, chaves Google `AIza*`, chaves AWS `AKIA*`, tokens Slack `xox*`, tokens bearer e pares chave/valor) são completamente expurgados (`[REDACTED_SECRET]`) tanto nos valores quanto nos **nomes de chaves** em todos os níveis de aninhamento. Colisões de chaves resultantes de redação são resolvidas deterministicamente sem perda de dados (`[REDACTED_SECRET]_1`, etc.). Além disso, estruturas iteráveis (conjuntos `set` e tuplas `tuple`) são normalizadas para listas padrão compatíveis com JSON, eliminando falhas de serialização durante a persistência SQLite.

---

## 2. Proveniência Estrita de Dados (`MetricProvenance`)

Para conformidade com o rigor de pesquisa científica, **nenhuma métrica pode ser inventada ou simulada disfarçada de medição real**. Cada dimensão contábil possui classificação mandatória de proveniência:

| Proveniência | Definição | Exemplo de Aplicação |
| :--- | :--- | :--- |
| `measured` | Medição direta e objetiva obtida da API do provedor ou do relógio do sistema operacional. | `usage.prompt_tokens` retornado pelo provedor; `time.perf_counter()` para duração total e TTFT. |
| `estimated` | Valor calculado deterministicamente pelo coordenador a partir de fórmula ou tabela pública declarada. | Custo monetário calculado multiplicando tokens por tabela de preços (`PricingRegistry`); duração de geração calculada por subtração. |
| `unknown` | Dado não reportado pelo provedor e não calculável com precisão. | `cache_read_tokens` em endpoints sem suporte; custo para modelos sem precificação registrada. |

> [!IMPORTANT]
> **Diferenciação entre Zero e Desconhecido:**
> Se uma métrica for desconhecida ou não informada pelo provedor, seu valor é preservado como `None` e sua proveniência como `unknown`.
> É proibido mascarar custos ou tokens desconhecidos como `0.0` ou `0`. Valores iguais a `0` são reservados exclusivamente para quantidades comprovadamente nulas.

---

## 3. Contratos de Contabilidade de Cache por Provedor

Diferentes provedores adotam semânticas distintas para tokens em cache. Para evitar **dupla contagem**, a função `normalize_provider_tokens` aplica os seguintes contratos formais:

### 3.1 OpenAI / Azure
- O campo `usage.prompt_tokens` **já inclui** `usage.prompt_tokens_details.cached_tokens`.
- Portanto, os tokens não-cacheados são `prompt_tokens - cached_tokens`.
- O total de tokens é estritamente:
  $$\text{total\_tokens} = \text{prompt\_tokens} + \text{completion\_tokens}$$
  *(Tokens de leitura de cache NÃO são somados novamente ao total).*

### 3.2 Anthropic Claude
- O campo `usage.input_tokens` representa **estritamente** os tokens de entrada não-cacheados.
- Os tokens `cache_read_input_tokens` e `cache_creation_input_tokens` são **disjuntos** de `input_tokens`.
- O total de tokens é calculado como:
  $$\text{total\_tokens} = \text{input\_tokens} + \text{output\_tokens} + \text{cache\_read\_tokens} + \text{cache\_creation\_tokens}$$

### 3.3 Google Gemini
- O campo `usageMetadata.promptTokenCount` **já inclui** `cachedContentTokenCount`.
- O total de tokens é:
  $$\text{total\_tokens} = \text{promptTokenCount} + \text{candidatesTokenCount}$$

---

## 4. Decomposição de Métricas e Latências

### 4.1 Latências e Diferenciação de Durações Derivadas (`LatencyMetrics`)
Grandezas temporais são expressas em **milissegundos (ms)** com temporizador de alta precisão (`time.perf_counter()`):
- `total_duration_ms`: Duração ponta a ponta medida diretamente no relógio (`latency_provenance = measured`).
- `ttft_ms`: *Time to First Token* — medido diretamente na chegada do primeiro fragmento de streaming (`measured`).
- `tool_duration_ms`: Duração cumulativa medida diretamente durante invocações de ferramentas (`measured`).
- `supervisor_overhead_ms`: Tempo de validação, auditoria e coordenação medido diretamente (`measured`).
- `generation_duration_ms`: Tempo de inferência do modelo. Quando derivado pela subtração $\text{total} - (\text{ferramentas} + \text{overhead})$, sua proveniência é explicitamente classificada como:
  $$\text{generation\_duration\_provenance} = \text{estimated}$$

### 4.2 Custos Monetários, Semântica de Faturamento de Cache e Versionamento (`PricingRegistry`)
Custos são expressos em USD por 1 milhão de tokens (1M tokens). O registro possui versionamento explícito (`REGISTRY_VERSION = "2026.11.01"`):

| Modelo / Família | Input / 1M | Output / 1M | Cache Read / 1M | Cache Creation / 1M |
| :--- | :--- | :--- | :--- | :--- |
| `claude-3-5-sonnet` | $3.00 | $15.00 | $0.30 | $3.75 |
| `claude-3-opus` | $15.00 | $75.00 | $1.50 | $18.75 |
| `claude-3-5-haiku` | $1.00 | $5.00 | $0.10 | $1.25 |
| `gpt-4o` | $2.50 | $10.00 | $1.25 | $0.00 |
| `gpt-4o-mini` | $0.15 | $0.60 | $0.075 | $0.00 |
| `o1` | $15.00 | $60.00 | $7.50 | $0.00 |
| `gemini-1.5-pro` | $3.50 | $10.50 | $0.875 | $0.00 |
| `gemini-1.5-flash` | $0.075 | $0.30 | $0.01875 | $0.00 |

#### Regras Contábeis de Faturamento por Provedor
Para refletir o modelo financeiro real de cada provedor e evitar sobretarifação artificial:
1. **OpenAI / Azure / Google Gemini:**
   O campo de prompt já contém os tokens em cache. O faturamento calcula a parcela uncached deduzindo os tokens em cache:
   $$\text{uncached\_in} = \max(0, \text{input\_tokens} - \text{cache\_read\_tokens})$$
   $$\text{custo\_total} = (\text{uncached\_in} \times \text{tarifa\_in} + \text{cache\_read\_tokens} \times \text{tarifa\_cache} + \text{output\_tokens} \times \text{tarifa\_out}) / 10^6$$
   *Exemplo canônico:* 1.000 tokens de entrada no `gpt-4o` (500 em cache) e 250 de saída:
   - Entrada não-cacheada: $500 \times \$2.50 / 10^6 = \$0.001250$
   - Leitura de cache: $500 \times \$1.25 / 10^6 = \$0.000625$
   - Saída: $250 \times \$10.00 / 10^6 = \$0.002500$
   - Custo exato: **US$ 0,004375** (evita a distorção anterior de US$ 0,005625).
2. **Anthropic Claude:**
   `input_tokens` já é estritamente não-cacheado, sendo faturado pela tarifa cheia e somado aos custos de leitura e criação de cache:
   $$\text{custo\_total} = (\text{input\_tokens} \times \text{tarifa\_in} + \text{cache\_read} \times \text{tarifa\_read} + \text{cache\_creation} \times \text{tarifa\_creat} + \text{output\_tokens} \times \text{tarifa\_out}) / 10^6$$
3. **Componentes Indispensáveis Ausentes:**
   Se `input_tokens` ou `output_tokens` forem desconhecidos (`None`), ou se o modelo não constar na tabela de preços, `total_cost_usd` permanece rigorosamente `None`, `pricing_version` permanece `None` e a proveniência é `unknown` (nunca gerando números fabricados ou zeros artificiais).

---

## 5. Esquema no SQLite e Triggers de Imutabilidade

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
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    total_tokens INTEGER,
    tokens_provenance TEXT NOT NULL,
    ttft_ms REAL,
    generation_duration_ms REAL,
    tool_duration_ms REAL,
    supervisor_overhead_ms REAL,
    total_duration_ms REAL NOT NULL,
    latency_provenance TEXT NOT NULL,
    generation_duration_provenance TEXT NOT NULL DEFAULT 'estimated',
    total_cost_usd REAL,
    cost_provenance TEXT NOT NULL,
    pricing_version TEXT,
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

-- Proteção de imutabilidade append-only (nível de banco de dados)
CREATE TRIGGER IF NOT EXISTS trg_cognitive_telemetry_no_update
BEFORE UPDATE ON cognitive_telemetry
BEGIN
    SELECT RAISE(FAIL, 'cognitive_telemetry records are immutable and append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_cognitive_telemetry_no_delete
BEFORE DELETE ON cognitive_telemetry
BEGIN
    SELECT RAISE(FAIL, 'cognitive_telemetry records are immutable and cannot be deleted');
END;
```

---

## 6. Agregações Estatísticas e Não-Truncamento (`TelemetryAggregate`)

O método `Store.aggregate_cognitive_telemetry` não impõe mais limite silencioso de 10.000 registros:
- Por padrão (`max_records=None`), agrega **todos** os registros correspondentes aos filtros sem truncamento (`is_truncated = False`).
- Quando `max_records` é especificado para amostragem limitada, a agregação sinaliza explicitamente:
  - `total_matched_records`: Contagem real de registros no banco.
  - `sample_size`: Quantidade efetivamente amostrada.
  - `is_truncated`: `True` quando `total_matched_records > sample_size`.
  - `truncation_note`: Mensagem informativa declarando a taxa de amostragem.
- **Diferenciação de Nulo/Desconhecido:**
  - `total_cost_usd`: Retorna `None` se nenhum registro possuir custo conhecido (em vez de `0.0`).
  - `total_tokens`: Retorna `None` se nenhum registro possuir tokens conhecidos.
  - Contadores dedicados: `records_with_measured_cost`, `records_with_estimated_cost`, `records_with_unknown_cost`, `records_with_measured_tokens`, etc.

---

## 7. Limitações Metodológicas Mantidas

1. **Top-P, Top-K, Heaps, Zipf e FSRS:** Permanecem rigorosamente **desabilitados** nesta subetapa (4C.0.1). O escopo é estritamente observacional.
2. **Ambiente Local e Virtualização:** Variações nas latências de rede e CPU local podem influenciar `generation_duration_ms`. Para a construção da baseline (4C.0.4), devem ser computadas médias sobre múltiplas execuções com intervalos de confiança.
