# Plano Experimental de Campanha Piloto — OpenRouter Free (Fase 4C.1)

> **Status:** Homologação Técnica Concluída — Revisão 7 (Origem HTTPS Autenticada e Proteção de Cabeçalhos)
> **Branch:** `feature/phase4c-1-openrouter-free`  
> **Política Financeira:** **Strict Zero-Cost Fail-Closed** (Custo Máximo Permitido: US$ 0.000000)

---

## 1. Objetivos da Trilha Experimental

1. Validar a resiliência operacional da infraestrutura ao interagir com modelos dinâmicos do catálogo gratuito da OpenRouter.
2. Mensurar latências reais, TTFT e contagem de tokens reportada pelos provedores (`MEASURED`), comparando-as com as estimativas sintéticas (`SYNTHETIC`) da linha de base v0.5.1.
3. Avaliar o comportamento do `FreeCostGuard` e do `AvailabilityLedger` frente a indisponibilidades, rate limits (HTTP 429 com `Retry-After`) e eventuais mutações de catálogo.

---

## 2. Modelos Alvo Candidatos (Modalidades Especializadas)

| Modalidade | Modelo Candidato | Contexto | Suporte a Ferramentas | Justificativa |
| :--- | :--- | :---: | :---: | :--- |
| **Texto / Chat** | `meta-llama/llama-3.2-3b-instruct:free` | 131k | Sim (`tools`) | Raciocínio rápido e estruturado |
| **Texto / Chat** | `google/gemini-2.0-flash-exp:free` | 1M | Sim (`tools`) | Janela de contexto estendida e precisão |
| **Reranking** | `nvidia/nemotron-rerank-8b:free` | 8k | N/A | Reranking neural de documentos e contexto |
| **Embeddings** | `baai/bge-large-en-v1.5:free` | 512 | N/A | Vetorização semântica para indexação |
| **Decisões** | `google/gemini-2.0-flash-exp:free` | 1M | Sim (`json_schema`) | Conformidade com esquemas e oráculos |
| **Voz / Áudio (Transcrição)** | `openai/whisper-large-v3:free` | 448 | N/A | Transcrição de áudio via endpoint dedicado `/audio/transcriptions` |
| **Voz / Áudio (Geração)** | Modelos TTS Gratuitos Homologados | N/A | N/A | Geração de áudio via endpoint `/audio/speech` (Fish Audio / HeyGen) |

---

## 3. Matriz de Riscos e Controles de Segurança (P0 / P1 / P2 — Revisão 4)

| Risco / Vetor | Nível | Controles Implementados | Comportamento Fail-Closed |
| :--- | :---: | :--- | :--- |
| **Bypass do Ponto de Saída HTTP** | **P0** | `OpenRouterHttpTransport.dispatch()` exige `FreeCostGuard` obrigatoriamente e valida modelo, preço, modalidade e payload final antes de emitir a requisição HTTP. | Falha imediata com `CostGuardError` ou `UnauthorizedModelError`. Nenhuma rota direta à rede externa pode ignorar o guard. |
| **Catálogo Antigo ou Desatualizado** | **P0** | `FreeModelRegistry` implementa TTL (`max_age_seconds`), hash de proveniência SHA-256 e método `ensure_fresh_catalog()`. | Se o snapshot expirar e não houver atualização confirmada, lança `StaleCatalogError` e bloqueia a execução. |
| **Preço Não-Nulo Oculto** | **P0** | `FreeCostGuard` + `parse_float_cost`: exige simultaneamente preço $0.00 em prompt, completion, image e request. Valores NaN, Inf, negativos ou ausentes são `INVALID`. | Abortamento imediato com `UnauthorizedModelError`. O sufixo `:free` isolado não qualifica modelo pago. |
| **Condição de Corrida em Quotas** | **P1** | `SQLiteQuotaStore` com transações atômicas `BEGIN IMMEDIATE` para reserva de cotas entre múltiplos processos concorrentes. | Falha com `RateLimitExceededError` se limite for atingido; falha fechada com `QuotaPersistenceError` se houver erro de escrita. |
| **Perda de Rastreabilidade** | **P1** | `AvailabilityLedger` grava eventos e snapshots sincronicamente em formato durável com `fsync`. Erros de I/O não são suprimidos. | Lança `AuditPersistenceError` se a gravação de auditoria falhar, impedindo execuções sem rastreabilidade. |
| **Fallback Pago Implícito** | **P1** | `inspect_request_payload` inspeciona rotas e arrays de fallback (`models`). | Rejeita qualquer rota desconhecida ou lista com modelo não-gratuito via `PaidFallbackBlockedError` e `AmbiguousRoutingError`. |
| **Incompatibilidade de Modalidade** | **P1** | `BaseModelAdapter._prepare_and_validate` e `OpenRouterHttpTransport` validam se a modalidade requisitada está declarada no catálogo e mapeada para um endpoint oficial. | Lança `IncompatibleModalityError` ou `IncompatibleModalityEndpointError`. Proibido fallback para `/chat/completions`. |
| **Vazamento de Segredos em Texto** | **P1** | Scrubber com varredura de expressões regulares para credenciais embutidas (`Bearer`, `sk-or-v1-...`, chaves PEM, etc.). | Lança `ConfidentialDataDetectedError` e bloqueia o trânsito do payload. |
| **Confusão de Proveniência** | **P2** | Métricas classificadas via `MetricProvenance` (`MEASURED`, `ESTIMATED`, `SYNTHETIC`, `UNKNOWN`). | Respostas sem campo `usage` reportam `UNKNOWN`; tokens heurísticos reportam `ESTIMATED`; mocks reportam `SYNTHETIC`. |
| **Disparo Acidental de Rede** | **P2** | `OpenRouterHttpTransport` mantido com `live_network_enabled=False` por padrão. | Chamadas sem opt-in explícito falham fechadas com `LiveNetworkDisabledError`. |

---

## 4. Gestão Estrita de Cotas e Limites do Piloto

Para contas gratuitas padrão da OpenRouter, a documentação oficial estabelece:
- **Limite por Minuto:** 20 requisições/minuto para modelos com terminação `:free`.
- **Limite Diário:** 50 requisições/dia no total da conta (incluindo tentativas de retry).

### Parâmetros Operacionais do Piloto
- **Tamanho da Amostra do Piloto:** 5 tarefas determinísticas $\times$ 2 repetições = **10 requisições no total**.
  - Deixa uma margem segura de 40 requisições restantes na cota diária para absorver eventuais retentativas.
- **Concorrência Máxima:** 1 requisição concorrente ($C=1$, estritamente sequencial).
- **Intervalo Mínimo entre Requisições:** 3.0 segundos (máximo 20 req/min).
- **Circuit Breaker:** Trip após 3 falhas consecutivas, com tempo de recuperação de 30 segundos.
- **Retry Policy:** Máximo de 2 tentativas por tarefa para erros 429 e 503, com backoff exponencial e respeito a `Retry-After`.

### 4.1. Proteções da Conta, Chaves de API e Reconhecimento Explícito de Volatilidade de Preços

Em atendimento estrito à determinação 8 da Auditoria Independente:

1. **Spend Cap no Painel OpenRouter:** A conta de serviço configurada para o experimento deve manter limite de gastos (*credit limit / spend cap*) configurado explicitamente em **US$ 0.00**. Nenhuma recarga automática, cartão de crédito ou método de faturamento deve estar ativo.
2. **Escopo e Permissões da Chave de API:** A chave de API criada no portal da OpenRouter deve possuir permissão exclusiva de inferência (`completions`, `embeddings`), sem privilégios administrativos de consulta de faturamento ou mutação de limites.
3. **Reconhecimento Formal de Volatilidade:** Reconhece-se taxativamente que **uma consulta de preços com valor US$ 0.00 não garante gratuidade perpétua nem estabilidade futura de preços**. O catálogo gratuito de provedores na OpenRouter é dinâmico e pode ser alterado a qualquer momento sem aviso prévio. Por esta razão:
   - A obtenção do catálogo em modo operacional é feita exclusivamente via HTTPS oficial autenticado (`OfficialOpenRouterCatalogClient`) com verificação TLS e validação de schema.
   - O `FreeCostGuard` aplica controle de frescor temporal (`max_age_seconds`) e revalidação no próprio ponto indivisível de saída HTTP (`OpenRouterHttpTransport.dispatch`).
   - Se um modelo previamente gratuito passar a ter custo não-nulo em qualquer dimensão (prompt, completion, image ou request), a tentativa de disparo é interceptada e abortada com `UnauthorizedModelError`, registrando o evento `PRICE_CHANGED_BLOCKED` no `AvailabilityLedger` durável.

---

## 5. Política de Privacidade e Governança de Dados

- **Dados Permitidos:** Exclusivamente prompts e gabaritos sintéticos públicos das Suítes A, B e C.
- **Dados Proibidos:** Nenhuma credencial, segredo, chave de API, dado pessoal ou código-fonte privado poderá transitar nos payloads enviados aos provedores.
- **Scrubber Ativo:** O `BaseModelAdapter` executa sanitização recursiva e detecção de credenciais embutidas antes do envio.

---

## 6. Protocolo de Transição para Execução Real

1. **Fase Atual (100% Offline e Homologada):** Implementação dos adaptadores, transporte sob opt-in desligado por padrão com Egress Gate, barreira `FreeCostGuard`, cotas atômicas SQLite, circuit breaker, ledger durável e testes adversariais completos.
2. **Submissão à Auditoria:** Apresentar código corrigido, suíte adversarial e matriz de riscos.
3. **Ativação da Campanha Piloto Real (Somente após aprovação expressa do usuário):**
   - Configuração de credencial `OPENROUTER_API_KEY` exclusivamente via variável de ambiente.
   - Habilitação explícita de `live_network_enabled=True`.
   - Execução controlada do piloto com registro no `AvailabilityLedger`.
   - Comparação formal de métricas com a baseline v0.5.1.

---

## 7. Procedimento Operacional do Piloto TEXT — Chamada Inicial Única (Pendente de Autorização)

Em cumprimento estrito às determinações da auditoria, o piloto operacional em ambiente real **NÃO foi executado** nesta etapa e permanece dependente de autorização formal expressa do usuário.

Quando autorizado pelo usuário, o procedimento operacional consistirá em uma **única chamada determinística inicial controlada** seguindo os 6 passos sequenciais obrigatórios:

### Passo 1: Verificação Prévia de Controles Financeiros e Conta
- Confirmar no painel da OpenRouter que o **Spend Cap / Credit Limit** da conta está estritamente em **US$ 0.00**.
- Confirmar que a chave de API fornecida via `$env:OPENROUTER_API_KEY` possui permissão exclusiva de inferência, sem métodos de faturamento associados.

### Passo 2: Inicialização com Origem HTTPS Autenticada
- Instanciar `OfficialOpenRouterCatalogClient` para obter `AuthenticatedCatalogSnapshot` assinado e atestado diretamente de `https://openrouter.ai/api/v1/models` (com validação estrita de URL, redirecionamentos e esquema HTTPS).
- Carregar o snapshot no `FreeModelRegistry(require_trusted_source=True)` via `load_authenticated_snapshot()`.
- Validar `registry.is_authenticated_origin == True` (objetos fabricados localmente são terminantemente rejeitados).

### Passo 3: Verificação de Elegibilidade Estrita do Modelo Alvo
- Confirmar que o modelo escolhido é `meta-llama/llama-3.2-3b-instruct:free` (ou `google/gemini-2.0-flash-exp:free`).
- Revalidar no `FreeCostGuard` que os preços declarados em todas as 4 dimensões (prompt, completion, image, request) são matematicamente iguais a US$ 0.000000.
- Confirmar ausência de rotas de fallback pagas.

### Passo 4: Configuração Durável de Quotas e Auditoria
- Criar banco SQLite local para controle de quota atômica (`SlidingWindowRateLimiter(sqlite_path="data/pilot_quotas.db")`).
- Criar arquivo durável de auditoria com `fsync` (`AvailabilityLedger(storage_path="docs/benchmarks/pilot_audit.jsonl", require_durable=True)`).

### Passo 5: Execução da Chamada Única Controlada ($N=1$, Sequencial)
- Modalidade: Estritamente **TEXT** (outras modalidades bloqueadas por `PILOT_SCOPE_RESTRICTION`).
- Payload: Prompt sintético padrão determinístico da Suíte A (`"Classify sentiment: This product exceeded all my expectations."`).
- Egress Gate: O `OpenRouterHttpTransport.dispatch()` inspeciona recursivamente o payload antes do envio (Gate 1), valida integridade de cabeçalhos proibindo sobrescrita de `Authorization`, `Content-Type` e `Host` (Gate 1b), reserva 1 unidade de quota no SQLite, registra a intenção pré-flight, despacha a requisição HTTPS e grava o desfecho pós-flight.

### Passo 6: Auditoria Pós-Execução e Classificação Científica
- Verificar status HTTP (esperado 200).
- Se presente o campo `usage` completo, classificar tokens como `MEASURED`; se ausente/parcial, classificar estritamente como `UNKNOWN`.
- Registrar latência total reportada e definir `ttft_ms = None` com `ttft_provenance = UNKNOWN` (ausência de instrumentação de streaming).
- Confirmar custo efetivo registrado: US$ 0.000000.
