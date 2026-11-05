# Plano Experimental de Campanha Piloto — OpenRouter Free (Fase 4C.1)

> **Status:** Proposta de Piloto Experimental — Atualizado após Auditoria Independente  
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
| **Voz / Áudio** | `openai/whisper-large-v3:free` | 448 | N/A | Transcrição de áudio em módulo isolado |

---

## 3. Matriz de Riscos e Controles de Segurança (P0 / P1 / P2)

| Risco / Vetor | Nível | Controles Implementados | Comportamento Fail-Closed |
| :--- | :---: | :--- | :--- |
| **Preço Não-Nulo Oculto** | **P0** | `FreeCostGuard` + `parse_float_cost`: exige simultaneamente preço $0.00 em prompt, completion, image e request. Valores NaN, Inf, negativos ou ausentes são `INVALID`. | Abortamento imediato com `UnauthorizedModelError`. O sufixo `:free` isolado não qualifica modelo pago. |
| **Fallback Pago Implícito** | **P1** | `inspect_request_payload` inspeciona rotas e arrays de fallback (`models`). | Rejeita qualquer rota desconhecida ou lista com modelo não-gratuito via `PaidFallbackBlockedError` e `AmbiguousRoutingError`. |
| **Incompatibilidade de Modalidade** | **P1** | `BaseModelAdapter._prepare_and_validate` valida se a modalidade requisitada está declarada formalmente no catálogo. | Lança `IncompatibleModalityError` e bloqueia a execução preventivamente. |
| **Evasão de Cotas em Retries** | **P1** | Cada tentativa de retry consome sua própria reserva na janela deslizante do `SlidingWindowRateLimiter`. | Se a cota diária (50) ou por minuto (20) for atingida durante os retries, aborta com HTTP 429. |
| **Atraso de Retry Anulado** | **P1** | `RetryPolicy` e `runner.py` respeitam o tempo integral de backoff exponencial e o cabeçalho `Retry-After`. | Bloqueio de novas aquisições até expirar `Retry-After`. |
| **Vazamento de Segredos em Texto** | **P1** | Scrubber com varredura de expressões regulares para credenciais embutidas (`Bearer`, `sk-or-v1-...`, chaves PEM, etc.). | Lança `ConfidentialDataDetectedError` e bloqueia o trânsito do payload. |
| **Perda de Rastreabilidade** | **P2** | `AvailabilityLedger` grava eventos e snapshots sincronicamente em arquivo append-only (JSON Lines). | Histórico persistido e restaurado após reinício do processo. |
| **Confusão de Proveniência** | **P2** | Métricas classificadas via `MetricProvenance` (`MEASURED`, `ESTIMATED`, `SYNTHETIC`, `UNKNOWN`). | Mocks identificados exclusivamente como `SYNTHETIC`; tokens de provedor como `MEASURED`. |
| **Disparo Acidental de Rede** | **P2** | `OpenRouterHttpTransport` mantido com `live_network_enabled=False` por padrão. | Chamadas sem opt-in explícito falham fechadas com `LiveNetworkDisabledError`. |

---

## 4. Gestão Estrita de Cotas e Limites do Piloto

Para contas gratuitas padrão da OpenRouter, a documentação oficial estabelece:
- **Limite por Minuto:** 20 requisições/minuto para modelos com terminação `:free`.
- **Limite Diário:** 50 requisições/dia no total da conta (incluindo tentativas).

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

1. **Fase Atual (100% Offline e Corrigida):** Implementação dos adaptadores, transporte sob opt-in desligado por padrão, barreira `FreeCostGuard`, limitador de cota por retry, circuit breaker, ledger persistente e testes adversariais completos.
2. **Submissão à Auditoria:** Apresentar código corrigido, suíte adversarial e matriz de riscos.
3. **Ativação da Campanha Piloto Real (Somente após aprovação expressa do usuário):**
   - Configuração de credencial `OPENROUTER_API_KEY` exclusivamente via variável de ambiente.
   - Habilitação explícita de `live_network_enabled=True`.
   - Execução controlada do piloto de 10 chamadas com registro no `AvailabilityLedger`.
   - Comparação formal de métricas com a baseline v0.5.1.
