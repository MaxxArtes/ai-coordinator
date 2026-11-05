# Plano Experimental de Campanha Piloto — OpenRouter Free (Fase 4C.1)

> **Status:** Proposta de Piloto Experimental — Aguardando Configuração de Credenciais e Autorização Formal  
> **Branch:** `feature/phase4c-1-openrouter-free`  
> **Política Financeira:** **Strict Zero-Cost Fail-Closed** (Custo Máximo Permitido: US$ 0.000000)

---

## 1. Objetivos da Trilha Experimental

1. Validar a resiliência operacional da infraestrutura ao interagir com modelos dinâmicos do catálogo gratuito da OpenRouter.
2. Mensurar latências reais, TTFT e contagem de tokens reportada pelos provedores, comparando-as com as estimativas sintéticas da linha de base v0.5.1.
3. Avaliar o comportamento do `FreeCostGuard` e do `AvailabilityLedger` frente a indisponibilidades, rate limits (HTTP 429) e eventuais mutações de catálogo.

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

## 3. Gestão Estrita de Quotas e Limites

Para contas gratuitas padrão da OpenRouter, a documentação oficial estabelece:
- **Limite por Minuto:** 20 requisições/minuto para modelos com terminação `:free`.
- **Limite Diário:** 50 requisições/dia no total da conta.

### Parâmetros Operacionais do Piloto
- **Tamanho da Amostra do Piloto:** 5 tarefas determinísticas $\times$ 2 repetições = **10 requisições no total**.
  - Deixa uma margem segura de 40 requisições restantes na quota diária para eventuais retentativas.
- **Concorrência Máxima:** 1 requisição concorrente (estritamente sequencial).
- **Intervalo Mínimo entre Requisições:** 3.0 segundos (máximo 20 req/min).
- **Circuit Breaker:** Trip após 3 falhas consecutivas, com tempo de recuperação de 30 segundos.
- **Retry Policy:** Máximo de 2 tentativas por tarefa para erros 429 e 503, com backoff exponencial e jitter.

---

## 4. Política de Privacidade e Governança de Dados

- **Dados Permitidos:** Exclusivamente prompts e gabaritos sintéticos públicos das Suítes A, B e C.
- **Dados Proibidos:** Nenhuma credencial, segredo, chave de API, dado pessoal ou código-fonte privado poderá transitar nos payloads enviados aos provedores.
- **Scrubber Ativo:** O `BaseModelAdapter` executa sanitização recursiva em todos os dicionários de entrada antes do envio.

---

## 5. Protocolo de Transição para Execução Real

1. **Fase Atual (100% Offline):** Implementação dos adaptadores, barreira `FreeCostGuard`, limitador de taxa, circuit breaker e testes unitários/adversariais validados na CI remota.
2. **Submissão à Auditoria:** Apresentar código, testes e plano do piloto.
3. **Ativação da Campanha Piloto Real (Somente após aprovação expressa):**
   - Configuração de credencial `OPENROUTER_API_KEY` exclusivamente via variável de ambiente.
   - Execução do piloto de 10 chamadas com registro no `AvailabilityLedger`.
   - Comparação formal de métricas com a baseline v0.5.1.
