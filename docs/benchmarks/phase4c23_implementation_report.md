# AI Coordinator — Relatório Técnico de Implementação da Fase 4C.2.3 (Revisão 10)

**Documento:** Relatório Técnico de Implementação do Orçamento Atômico, Transactional Outbox e Fallback Governado  
**Versão:** 1.0.0 (Revisão 10)  
**Data:** 2026-11-09  
**Fase:** 4C.2.3 — Orçamento Atômico e Fallback Multi-Modelo Governado  
**Tarefa ai-coordinator:** `1a52600ee41f4ccd`  
**Status:** Implementação Concluída e Validada Offline  
**Repositório:** `D:\ai-coordinator`  
**Branch de Trabalho:** `feature/phase4c-2-ed25519-truststore`  
**Commit de Base:** `126f272` (Publicado no GitHub remoto)  
**Branch Estável (main):** Preservada em `a2f8f65` (`v0.5.1`)  

---

## 1. Sumário Executivo

A Etapa **4C.2.3** integrou a infraestrutura criptográfica desenvolvida na Fase 4C.2.2 ao mecanismo relacional de persistência ACID, auditoria durável e máquina de estados de despacho at-most-once.

### Entregas Técnicas:
1. **Armazenamento Atômico de Orçamento SQLite (`src/ai_coordinator/openrouter/atomic_quota.py`)**:
   - `AtomicDispatchQuotaStore`: DDL relacional em SQLite com `journal_mode = WAL`, `synchronous = FULL`, `busy_timeout = 5000` e `foreign_keys = ON`;
   - Transação atômica serializada via `BEGIN IMMEDIATE` para aquisição de reservas, eliminando concorrência destrutiva entre múltiplos processos.
2. **Garantia Local At-Most-Once e Ponto de Compromisso Irreversível (PIC)**:
   - Estado `SEND_COMMITTED` gravado em disco com `fsync` antes de qualquer emissão física de pacotes na rede;
   - Transição estritamente unidirecional: uma vez em `SEND_COMMITTED`, a reserva é irreversivelmente consumida.
3. **Transactional Outbox Durável e Projetor de Auditoria**:
   - Tabela `outbox_events` como fonte única da verdade para eventos de auditoria;
   - `TransactionalOutboxProjector`: Projeta eventos pendentes para `pilot_audit.jsonl` com `os.fsync()`, garantindo idempotência e desacoplamento do transporte de rede.
4. **Proteção Anti-Replay Persistente**:
   - Tabela `consumed_authorizations` com chave primária em `authorization_id`;
   - Tentativas de reutilização de manifestos já consumidos são rejeitadas em nível de banco de dados (`PersistentReplayAttackError`), persistindo entre diferentes processos e reinícios de sistema.
5. **Recuperação Conservadora de Quedas (P0.3)**:
   - `recover_interrupted_reservations()`: Sanitizador de inicialização que identifica registros em `SEND_COMMITTED` sem resposta e os transita para `UNKNOWN`;
   - Reservas em `UNKNOWN` permanecem computadas como consumidas, proibindo retransmissões automáticas.
6. **Motor de Fallback Multi-Modelo Governado (`GovernedFallbackEngine`)**:
   - **Nível 1 (`max_dispatches = 1`)**: Diante de HTTP 429, emite evento `FALLBACK_PROPOSED_BLOCKED` detalhando o modelo recomendado, encerrando a execução com zero novos despachos;
   - **Nível 2 (`max_dispatches > 1`)**: Diante de HTTP 429, aloca atomicamente nova reserva para modelo alternativo homologado no `FreeCostGuard` e autoriza o envio sob o teto.
7. **Suíte de Testes Automatizada (`tests/test_phase4c2_atomic_quota_fallback.py`)**:
   - **12 testes unitários e de estresse**, todos aprovados com 100% de sucesso.
   - Total acumulado da Fase 4C.2: **40 testes específicos (28 de criptografia + 12 de quota/outbox)** e **56 testes totais de integração** passando 100% offline.

---

## 2. Matriz de Cobertura dos Testes da Etapa 4C.2.3

| ID | Cenário / Invariante Testado | Requisito Protegido | Resultado |
| :---: | :--- | :---: | :---: |
| `test_01` | Prevenção persistente de replay attack cross-restart | Anti-Replay SQLite | **PASS** |
| `test_02` | Rejeição de orçamento sem consentimento humano | Tier 3 (Consentimento) | **PASS** |
| `test_03` | Rejeição por divergência entre manifesto e commit atual | Tier 3 (Vínculo de Código) | **PASS** |
| `test_04` | Garantia At-Most-Once com teto unitário (`max_dispatches=1`) | P0.1 (Teto Inviolável) | **PASS** |
| `test_05` | Ponto de Compromisso Irreversível (`SEND_COMMITTED`) | P0.1 (PIC em Disco) | **PASS** |
| `test_06` | Transições de máquina de estados para `COMPLETED` e `FAILED` | P0.3 (FSM Linear) | **PASS** |
| `test_07` | Transactional Outbox com projeção durável `fsync` para JSONL | P0.2 (Atomicidade Outbox) | **PASS** |
| `test_08` | Recuperação conservadora de crash drenando para `UNKNOWN` | P0.3 (Recuperação Fail-Closed) | **PASS** |
| `test_09` | Transição direta para `UNKNOWN` em timeout de socket de rede | P0.3 (Anti-Loop) | **PASS** |
| `test_10` | Fallback Nível 1 (`max_dispatches=1`): recomendação sem envio | P0.1 (Tentativa Única Estrita) | **PASS** |
| `test_11` | Fallback Nível 2 (`max_dispatches=2`): despacho de contingência | Fallback Multi-Modelo | **PASS** |
| `test_12` | Bloqueio fail-closed quando quota multi-despacho esgota | Teto Orçamentário | **PASS** |

---

## 3. Conformidade com as Recomendações da Auditoria

1. **Publicação no GitHub Remoto**:
   - A branch `feature/phase4c-2-ed25519-truststore` contendo o commit `126f272` foi enviada com sucesso para o GitHub remoto:  
     `https://github.com/MaxxArtes/ai-coordinator/tree/feature/phase4c-2-ed25519-truststore`
   - O auditor pode inspecionar e validar independentemente todos os arquivos e testes.
2. **Persistência da Proteção Anti-Replay**:
   - Validada tanto no nível criptográfico (hash canônico) quanto no nível relacional SQLite (`consumed_authorizations` com chave primária). Testada com reinício simulado de instância.
3. **Validade da Canonicalização**:
   - O hash SHA-256 do manifesto canônico é calculado e gravado na tabela `global_dispatch_budgets`, garantindo que apenas manifestos estritamente conformes sejam indexados.
4. **Diferença Rigorosa entre Assinatura Válida e Consentimento Humano**:
   - O método `register_budget_from_signed_manifest` exige simultaneamente a assinatura válida (Tier 1), a autoridade da chave (Tier 2) e a flag explícita `human_operator_consent=True` (Tier 3). Se o consentimento operacional for `False`, a operação aborta antes de qualquer inserção relacional.
5. **Zero Tráfego de Rede e Congelamento da Main**:
   - Zero sockets abertos; branch `main` permanece rigorosamente congelada em `a2f8f65`.
