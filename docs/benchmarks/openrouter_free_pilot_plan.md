# Plano Experimental de Campanha Piloto — OpenRouter Free (Fase 4C.1)

> **Status:** Proposta de Piloto Experimental — Atualizado após Auditoria Independente (Revisão 4)  
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
   - Execução controlada do piloto de 10 chamadas com registro no `AvailabilityLedger`.
   - Comparação formal de métricas com a baseline v0.5.1.
