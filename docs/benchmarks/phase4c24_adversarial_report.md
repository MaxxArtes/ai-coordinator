# AI Coordinator — Relatório de Auditoria Adversarial e Recuperação (Fase 4C.2.4 — Revisão 11)

**Documento:** Relatório Técnico de Resolução de Apontamentos P0/P1 e Validação da Suíte Adversarial  
**Versão:** 1.0.0 (Revisão 11)  
**Data:** 2026-11-09  
**Fase:** 4C.2.4 — Auditoria Adversarial, Correções P0 e Recuperação Conservadora  
**Tarefa ai-coordinator:** `1a52600ee41f4ccd` (Revisão 11)  
**Status:** Implementação Concluída, 100% Validada Offline  
**Repositório:** `D:\ai-coordinator`  
**Branch de Trabalho:** `feature/phase4c-2-ed25519-truststore`  
**Branch Estável (main):** Preservada em `a2f8f65` (`v0.5.1`)  

---

## 1. Resumo Executivo e Atendimento aos Apontamentos do Auditor

Na Revisão 10 da auditoria independente, foram identificados 4 apontamentos de segurança e integridade (3 P0 e 1 P1), além de uma recomendação de invariante crítico sobre a procedência de falhas no fallback. Todos foram integralmente corrigidos e validados por testes dedicados na Fase 4C.2.4:

| Item | Gravidade | Descrição do Apontamento do Auditor | Resolução Implementada | Evidência de Teste |
| :---: | :---: | :--- | :--- | :---: |
| **1** | **P0** | `human_operator_consent=True` era valor padrão em `register_budget_from_signed_manifest`, permitindo omissão sem consentimento explícito. | Parâmetro tornado **estritamente obrigatório (sem default)**. Exige validação booleana estrita `human_operator_consent is True`. Adicionada validação de identidade do operador humano contra `authorizer_identity` do manifesto. | `test_09` (4C.2.4) e `test_02` (4C.2.3) |
| **2** | **P0** | `commit_send()` não revalidava expiração do manifesto e revogação da chave no Ponto de Compromisso Irreversível (PIC). | `commit_send()` agora revalida no PIC: expiração temporal (`valid_until_utc`), commit HEAD do código, status/revogação da chave pública na TrustStore e pertencimento do modelo aos `allowed_models`. Qualquer não conformidade aborta em fail-closed, marca reserva como `FAILED` e registra auditoria. | `test_07` e `test_08` (4C.2.4) |
| **3** | **P0** | Fallback consultava `catalog_registry.is_model_free()`, mas não chamava `FreeCostGuard.authorize_model_execution()`. | O motor `GovernedFallbackEngine.evaluate_fallback()` agora invoca mandatoriamente `FreeCostGuard.authorize_model_execution(candidate, verify_freshness=True)`. Modelos não auditados, sem sufixo `:free` ou com catálogo defasado são sumariamente descartados. | `test_10` (4C.2.4) |
| **4** | **P1** | Projetor JSONL podia duplicar linhas se ocorresse queda (*crash*) após `fsync()` no arquivo e antes do commit de marcação no SQLite. | O `TransactionalOutboxProjector` agora inspeciona os `event_id`s já existentes em `pilot_audit.jsonl` antes de gravar novas linhas, assegurando **100% de idempotência** com zero linhas duplicadas em recuperação pós-queda. | `test_12` (4C.2.4) |
| **5** | **P0** | Invariante de Procedência de Falha: fallback não deve ser acionável sem comprovação de HTTP 429/503 na tentativa correspondente. | `evaluate_fallback()` exige que a reserva falha esteja em estado `FAILED` e que o código HTTP registrado pertença exclusivamente ao conjunto `{429, 503}`. Qualquer outro código HTTP (ex: 400, 401, 500) rejeita o fallback. | `test_11` (4C.2.4) |

---

## 2. Detalhamento das Correções Arquiteturais

### 2.1 Consentimento Humano Explícito e Identidade do Operador (P0.1)
- Em [`src/ai_coordinator/openrouter/atomic_quota.py`](file:///D:/ai-coordinator/src/ai_coordinator/openrouter/atomic_quota.py#L254), a assinatura de `register_budget_from_signed_manifest` foi alterada para:
  ```python
  def register_budget_from_signed_manifest(
      self,
      signed_manifest: SignedAuthorizationManifest,
      trust_store: TrustStore,
      expected_commit: str,
      human_operator_consent: bool,
      human_operator_identity: Optional[str] = None,
      check_time_utc: Optional[dt.datetime] = None,
  ) -> str:
  ```
- O parâmetro `human_operator_consent` não possui mais valor padrão (`default`).
- Uma verificação explícita `if human_operator_consent is not True` lança `HumanOperationalAuthorizationError`.
- Quando `human_operator_identity` é fornecido, ele é comparado contra `signed_manifest.manifest.authorizer_identity`; divergências barram o registro em nível de autorização operacional (Tier 3).

### 2.2 Revalidação Abrangente no PIC (`commit_send`) (P0.2)
- Antes de transitar uma reserva para o estado irreversível `SEND_COMMITTED` e antes de liberar a chamada de rede, o método `commit_send()` consulta a tabela `global_dispatch_budgets` e revalida:
  1. **Validade Temporal:** `at_time_utc <= valid_until_utc`. Se expirado, marca `FAILED` com `REASON_EXPIRED` e lança `ManifestExpiredError`.
  2. **Alinhamento de Commit:** `runtime_commit == budget_code_commit`. Se divergente, marca `FAILED` e lança `CommitDivergenceError`.
  3. **Revogação na TrustStore:** Verifica se a chave pública do autorizador na Trust Store foi revogada ou expirou entre a reserva e o PIC. Se revogada, marca `FAILED` e lança `KeyRevokedError`.
  4. **Autorização de Modelo:** Verifica se o `target_model` permanece na lista `allowed_models` do orçamento.

### 2.3 Certificação Obrigatória com `FreeCostGuard` no Fallback (P0.3)
- Na avaliação de modelos de contingência em `GovernedFallbackEngine.evaluate_fallback()`, cada candidato é submetido a:
  ```python
  entry = self.cost_guard.authorize_model_execution(candidate, verify_freshness=True)
  ```
- Qualquer modelo com preço superior a US$ 0.00, sem sufixo `:free` ou com timestamp de catálogo expirado falha na autorização do `FreeCostGuard` e é descartado silenciosamente da lista de candidatos elegíveis.

### 2.4 Projeção de Outbox Idempotente à Prova de Crash (P1)
- Em `TransactionalOutboxProjector.project_pending_events()`, antes de abrir o arquivo para append:
  1. O arquivo `pilot_audit.jsonl` é lido (se existente) e todos os `event_id`s persistidos são indexados em um conjunto `existing_ids`.
  2. Apenas eventos com `event_id not in existing_ids` são adicionados ao buffer de escrita e submetidos a `fsync()`.
  3. Em seguida, os eventos são marcados como projetados no SQLite. Se um processo sofrer queda entre o `fsync()` e o commit no SQLite, a próxima execução ignora os eventos já em disco e atualiza o SQLite com idempotência exata.

---

## 3. Matriz de Cobertura da Suíte Adversarial 4C.2.4 (13 Cenários)

A suíte [`tests/test_phase4c24_adversarial_recovery.py`](file:///D:/ai-coordinator/tests/test_phase4c24_adversarial_recovery.py) implementou 13 cenários adversariais avançados executados 100% offline:

| # | Método de Teste | Invariante / Cenário Adversarial Testado | Resultado |
| :-: | :--- | :--- | :---: |
| 1 | `test_01_real_concurrency_race_condition_max_dispatches_one` | Concorrência real via `ThreadPoolExecutor` (10 threads simultâneas) disputando a aquisição de reserva única (`max_dispatches=1`). Exatamente 1 thread obtém reserva; 9 falham com `BudgetExhaustedError`. | **APROVADO** |
| 2 | `test_02_contention_for_final_unit_of_max_dispatches` | Disputa concorrente (8 threads) pela última unidade de despacho em orçamento de 2 unidades. Apenas 1 vence a disputa; integridade do saldo mantida em 2 consumidos. | **APROVADO** |
| 3 | `test_03_crash_before_and_after_send_committed` | Simulação de crash abrupto antes e depois do PIC (`commit_send`). Reservas em `RESERVED` expiram para `EXPIRED_UNCOMMITTED` e restauram o saldo; reservas em `SEND_COMMITTED` são drenadas irreversivelmente para `UNKNOWN` sem reutilização de saldo. | **APROVADO** |
| 4 | `test_04_socket_timeout_transitions_to_unknown_fail_closed` | Queda de rede por timeout de socket após `SEND_COMMITTED`. Transita a reserva para `UNKNOWN` fail-closed, preservando consumo do orçamento. | **APROVADO** |
| 5 | `test_05_corrupted_ledger_recovery_skips_bad_lines_without_crash` | Injeção de linhas JSON corrompidas e bytes truncados em `pilot_audit.jsonl`. O leitor de auditoria salta linhas defeituosas de forma segura sem quebrar o processo. | **APROVADO** |
| 6 | `test_06_manifest_replay_and_reservation_reuse_blocked` | Tentativa deliberada de reexecutar manifesto idêntico em nova instância de banco, ou reutilizar `reservation_id` concluído. Bloqueado por `PersistentReplayAttackError` e `ReservationStateError`. | **APROVADO** |
| 7 | `test_07_key_revocation_between_reservation_and_send_pic_blocks_send` | Chave Ed25519 revogada na Trust Store após `acquire_reservation()` e antes de `commit_send()`. O PIC detecta a revogação, aborta com `KeyRevokedError` e transita a reserva para `FAILED`. | **APROVADO** |
| 8 | `test_08_manifest_expiration_between_reservation_and_send_pic_blocks_send` | Janela temporal do manifesto expira entre `acquire_reservation()` e `commit_send()`. O PIC bloqueia o envio com `ManifestExpiredError` e transita para `FAILED`. | **APROVADO** |
| 9 | `test_09_consent_mandatory_no_default_and_imposter_rejection` | Tentativa de chamada sem `human_operator_consent` (erro sintático), com `False` (erro semântico) ou com identidade de operador divergente do manifesto. Todas rejeitadas com `HumanOperationalAuthorizationError`. | **APROVADO** |
| 10 | `test_10_paid_model_and_stale_catalog_rejected_by_free_cost_guard` | Candidato de fallback pago (custo > 0) ou sem atualização recente de catálogo. `FreeCostGuard` rejeita e fallback bloqueia emissão de requisição. | **APROVADO** |
| 11 | `test_11_fallback_rejected_without_proven_http_429` | Tentativa de acionar fallback com reserva não falha ou com status HTTP que não seja 429/503 (ex: HTTP 400 ou 500). Rejeitado com `QuotaSecurityError`. | **APROVADO** |
| 12 | `test_12_idempotent_outbox_recovery_zero_duplicate_lines` | Queda simulada entre `fsync()` no JSONL e commit no SQLite. Segunda projeção deduplica eventos e produz **zero linhas duplicadas** no ledger. | **APROVADO** |
| 13 | `test_13_recovery_scanner_does_not_corrupt_active_completed_reservations` | Scanner conservador de recuperação (`recover_interrupted_reservations`) executado em banco com reservas ativas recentes (`RESERVED`), concluídas (`COMPLETED`) e abandonadas. Apenas reservas realmente estagnadas são drenadas; ativas e concluídas permanecem intactas. | **APROVADO** |

---

## 4. Consolidação Geral de Testes (Fase 4C.2)

Todos os testes das três etapas de desenvolvimento e auditoria da Fase 4C.2 foram executados em conjunto, totalizando **53 testes com 100% de aprovação offline**:

| Arquivo de Suíte | Foco da Suíte | Cenários | Status |
| :--- | :--- | :---: | :---: |
| [`test_phase4c2_ed25519_truststore.py`](file:///D:/ai-coordinator/tests/test_phase4c2_ed25519_truststore.py) | Criptografia Ed25519, TrustStore, Verificação Tripartite | 28 | **28/28 APROVADOS** |
| [`test_phase4c2_atomic_quota_fallback.py`](file:///D:/ai-coordinator/tests/test_phase4c2_atomic_quota_fallback.py) | Orçamento Atômico SQLite, Outbox, PIC, Máquina de Estados | 12 | **12/12 APROVADOS** |
| [`test_phase4c24_adversarial_recovery.py`](file:///D:/ai-coordinator/tests/test_phase4c24_adversarial_recovery.py) | Auditoria Adversarial, Concorrência, Revalidação PIC, Quedas | 13 | **13/13 APROVADOS** |
| **Total Geral Fase 4C.2** | **Garantias Criptográficas, Orçamentárias e Adversariais** | **53** | **53/53 APROVADOS (100%)** |

- **Tempo total de execução:** 34.85s
- **Ambiente:** Python 3.14.5 / pytest 9.1.1
- **Requisições de Rede Reais:** **ZERO** (isolamento estrito offline mantido).
