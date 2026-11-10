# Changelog

Todas as alterações notáveis deste projeto serão documentadas neste arquivo.

O formato é baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

---

## [0.6.0] - Proposta / Homologação Fase 4C.2 (Aguardando Liberação Operacional)

Versão proposta consolidando a implementação e homologação offline da **Fase 4C.2 (Fallback Multi-Modelo Governado e Especificação Técnica Offline)**.

> [!NOTE]
> Versão desenvolvida e testada 100% offline ($0.00 USD incorridos). A publicação oficial da tag e release no PyPI permanece pendente de autorização do operador e teste de staging com provedor real.

### Adicionado

#### Governança Criptográfica e Verificação Tripartite (Fase 4C.2.2)
- **Assinatura Ed25519 RFC 8032**: `CanonicalAuthorizationManifest` e `Ed25519Signer` para assinatura determinística de manifestos de despacho contendo commit HEAD, modelos autorizados, cotas e validade temporal.
- **Trust Store Local com Rotação**: `TrustStore` para gestão segura de chaves públicas autorizadas, status (`ACTIVE`, `REVOKED`, `EXPIRED`) e permissões estritas (`PERMISSION_DISPATCH_OPENROUTER_FREE`, `PERMISSION_AUDIT_VERIFY`).
- **Autoridade Operacional Humana Autenticada**: `HumanOperationalAuthority` exigindo autenticação do supervisor via `supervisor_key` e emissão de tokens HMAC-SHA256 vinculados ao `authorization_id` e ao propósito estrito da execução.
- **Verificador Tripartite**: `TripartiteAuthorizationVerifier` inspecionando simultaneamente a chave criptográfica, o commit HEAD de código e a autorização humana autenticada.

#### Orçamento Atômico e Ponto de Compromisso Irreversível (Fase 4C.2.3)
- **Store SQLite com BEGIN IMMEDIATE**: `AtomicDispatchQuotaStore` com isolamento estrito WAL, garantindo semântica local *at-most-once* e teto global inviolável (`max_dispatches`).
- **Anti-Replay Persistente**: Tabela `consumed_authorizations` com restrição de unicidade em banco, impedindo reuso de manifestos através de reinicializações e falhas.
- **Ponto de Compromisso Irreversível (PIC `SEND_COMMITTED`)**: Transição transacional atômica antes de qualquer transporte TLS com revalidação de todas as invariantes e persistência durável via `fsync`.
- **Proibição de Retransmissão após UNKNOWN**: Estados indeterminados retêm o saldo consumido de forma fail-closed, impedindo dupla cobrança remota em caso de timeout de transporte.

#### Transactional Outbox e Sincronização Nativa sem TOCTOU (Fase 4C.2.4)
- **Projeção Atômica de Eventos**: `TransactionalOutboxProjector` projetando eventos do SQLite para JSONL canônico de auditoria de forma idempotente e recuperável.
- **Travas Nativas de Kernel**: `_InterProcessFileLock` utilizando `msvcrt.locking` no Windows e `fcntl.flock` no POSIX sobre arquivo de trava permanente. Eliminação completa de `os.unlink()`, prevenindo corridas TOCTOU e garantindo liberação automática pelo kernel em caso de término abrupto de processos.

#### Fallback Multi-Modelo e Benchmarks Comparativos (Fase 4C.2.5 / 4C.2.6)
- **Mecanismo de Fallback em Dois Níveis**: `GovernedFallbackEngine` avaliando candidatos gratuitos sob `FreeCostGuard`:
  - Nível 1 (`max_dispatches=1`): Emite recomendação governada sem despacho secundário.
  - Nível 2 (`max_dispatches=2`): Autoriza e transiciona a cota secundária sob novo PIC.
- **Campanha Quantitativa de Benchmarks**: Suíte comparativa em `benchmark_4c2.py` avaliando 6 cenários empíricos com separação de Heap Python e RSS do processo, proveniência de commits segregada e exportação de amostras brutas.

---

## [0.5.1] - 2026-11-03

Release de segurança prioritária e endurecimento de isolamento (*Security Hardening*), corrigindo os achados de auditoria identificados na revisão do PR #2 do Codex.

### Segurança e Correções

#### P1 — Prevenção de Fuga da Sandbox e Sobrescrita Arbitrária via Mailbox (.broker)
- **Validação Estrita da Hierarquia da Mailbox**: `ToolBroker.process_mailbox` agora inspeciona recursivamente toda a hierarquia `.broker`, `.broker/requests` e `.broker/responses` contra a raiz autorizada da sandbox (`sbox`), garantindo confinamento estrito.
- **Bloqueio de Symlinks, Junções e Reparse Points**: Rejeição imediata (*fail-closed*) caso qualquer diretório ou componente intermediário da hierarquia seja link simbólico, junção NTFS ou reparse point, impedindo que workers não confiáveis redirecionem o diretório de respostas para fora da sandbox.
- **Fixação de Diretórios e Prevenção de TOCTOU (Directory Swap)**:
  - No Windows, introduzido o mecanismo de fixação `_PinnedDirectory`, mantendo handles abertos nos diretórios com `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT` sem o compartilhamento `FILE_SHARE_DELETE`. Qualquer tentativa por processos concorrentes de renomear, excluir ou substituir `.broker` ou `.broker/responses` por junções é rejeitada diretamente pelo kernel do Windows NT (`WinError 32 / sharing violation`).
  - Em ambientes POSIX, os diretórios são abertos por descida estritamente relativa a partir da raiz confiável (`sbox_fd` -> `.broker` via `dir_fd=sbox_fd` -> `requests`/`responses` via `dir_fd=broker_fd`) com flags `O_DIRECTORY | O_NOFOLLOW`, impedindo desvios por links simbólicos em qualquer diretório intermediário ou ancestral. Todas as operações de leitura, stat, remoção e gravação de requisições e respostas operam exclusivamente através de descritores fixados (`dir_fd=req_fd`, `dir_fd=res_fd`), com substituição atômica via `src_dir_fd`/`dst_dir_fd` e política estrita de falha fechada (*fail-closed*), eliminando janelas de corrida TOCTOU e swaps de diretórios ancestrais.

#### P1 — Serialização de Concorrência e Resiliência na Trilha de Auditoria
- **Lock Cross-Process Exclusivo (`auth/audit.lock`)**: Introduzido o context manager `_acquire_audit_lock` utilizando locks de arquivo exclusivos no nível do sistema operacional (`msvcrt.locking` no Windows e `fcntl.flock` no POSIX), serializando a transação do SQLite (`BEGIN IMMEDIATE` até `commit()`) e a escrita atômica no arquivo desacoplado `audit.anchor` sob o mesmo lock mútuo entre processos e instâncias de `Store`.
- **Serialização Estrita e Prevenção de Regressão na Âncora Externa**: A ordem de gravação garante que a transação SQLite seja consolidada primeiro (`commit()`) para que eventuais falhas deixem a âncora externa apenas atrasada (recuperável via `reconcile_anchor()`), nunca adiantada. Adicionada conferência de número de sequência que impede que processos atrasados sobrescrevam o arquivo com sequências inferiores.
- **Índice UNIQUE em Sequência de Auditoria**: Adicionada restrição `UNIQUE` ao índice `idx_worker_audit_seq`, garantindo integridade estrita e impossibilidade de sequências duplicadas no nível do banco.
- **Eliminação de Colisões em Âncoras Desacopladas**: O arquivo temporário da âncora externa agora utiliza identificador exclusivo contendo PID e UUID (`audit.anchor.tmp.{pid}.{uuid}`), eliminando conflitos de acesso no Windows e em sistemas POSIX.
- **Recuperação Atômica de Quedas**: Em caso de falha transitória ou descontinuidade da âncora externa, o método `reconcile_anchor` restaura o arquivo a partir do registro assinado e auditado na tabela `worker_audit_anchors` sob o lock exclusivo cross-process.

#### P2 — Correspondência Exata de Identidade e Tolerância Zero (PID Recycling)
- **Correspondência Estrita Sem Tolerância (`diff == 0`)**: A função `match_process_creation_time` opera com tolerância temporal zero, eliminando janelas heurísticas (`tolerance_ns`) para impedir que a reutilização rápida de PID pelo sistema operacional engane o supervisor.
- **Normalização Segura de Identidades de Processo**:
  - Suporte ao formato legado da v0.4.0 no Windows (Win32 `FILETIME` bruto de 100ns desde 1601, na faixa `1.16e17` a `2.0e17`), validado estritamente em ambiente Windows via igualdade determinística exata.
  - Suporte ao formato legado da v0.4.0 no Linux (clock ticks / jiffies desde boot, `< 10^11`), validado estritamente em POSIX via igualdade exata com base no `SC_CLK_TCK`.
  - Rejeição cruzada fail-closed entre plataformas (FILETIME em POSIX e jiffies em Windows são rejeitados imediatamente).
- **Política Fail-Closed Estrita**: Identidades com timestamps negativos, nulos, ambíguos ou fora de faixas válidas conhecidas são rejeitadas de forma conservadora, prevenindo falsos positivos e acidentes por reciclagem de PID.
- **Preservação de Runs em Andamento e Escalonamento Seguro**: O reconciliador de tarefas (`Store.reconcile`) e a finalização de processos (`safe_terminate_tree`, `WindowsProcessController.is_running` e `PosixProcessController.terminate_process`) continuam reconhecendo processos ativos legados sem interromper indevidamente runs em progresso, garantindo que a validação de identidades legadas seja mantida durante a etapa de escalonamento destrutivo para `SIGKILL`.

---

## [0.5.0] - 2026-10-29

Esta versão consolida a conclusão da **Fase 4B (Human-AI Cognitive Workspace)**, introduzindo isolamento de processos e filesystem em nível de sistema operacional (OS-level sandboxing), governança e autorização segura de ferramentas via Tool Broker com tokens HMAC, trilha de auditoria criptográfica imutável com dupla ancoragem e integração ao ecossistema `ai-memory`.

### Adicionado

#### Human-AI Cognitive Workspace & Sessões Supervisionadas (Fases 4A e 4B.1–4B.4)
- **Modelo de Entidades e Papéis**:
  - `HumanPrincipal` e `HumanSession` com controle de acesso baseado em papéis (RBAC).
  - Autenticação de sessões humanas via tokens HMAC assinados e expiração temporal.
- **Checkpoints Cognitivos**:
  - `CognitiveCheckpoint` com amarração a revisões operacionais, estado de DAG e árvore de arquivos via hash SHA-256.
  - Histórico de revisões cognitivas preservado de forma idempotente e imutável.
- **Integração com o Ecossistema `ai-memory`**:
  - `AIMemoryAdapter` para sincronização bidirecional de memórias de projeto e contexto durável.
  - Validação estrita de formato UUID para identificadores de workstream (`ai_memory_workstream_id`).
  - Tratamento resiliente e degradado (*fail-safe*) quando a CLI ou base do `ai-memory` não estiver disponível.
  - Política de segurança: dados recuperados do `ai-memory` são tratados como evidências não-confiáveis, impedindo execução direta de instruções contidas em memórias.

#### Contenção e Sandboxing de Processos no SO (Fases 4B.5.1, 4B.6.1 e 4B.6.2)
- **Windows NT**:
  - Subprocessos de workers criados suspensos com `CREATE_SUSPENDED`.
  - Inserção atômica em Win32 Job Object com limites de cotas de memória e processos ativos antes da liberação de execução.
  - Terminação de árvore garantida pelo kernel NT via `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- **POSIX (Linux e macOS)**:
  - Criação de novas sessões de processo (`start_new_session=True`).
  - Terminação atômica do grupo de processos com `os.killpg` (SIGTERM seguido de SIGKILL) e verificação contra reciclagem de PIDs pelo tempo de criação de processo (`/proc` e `ps`).
  - **Trampoline POSIX Isolado**: Execução intermediária através de `sys.executable -I -S -c` com sincronização unidirecional via pipe, impedindo execução prematura de variáveis de ambiente (`PYTHONPATH`) ou hooks (`sitecustomize.py`).
- **Protocolo de Inicialização em Duas Fases (Gate G1)**:
  - `ProcessSupervisor.spawn(..., start_suspended=True)` mantém o processo do worker bloqueado no kernel.
  - Persistência e confirmação de durabilidade do evento `spawn` na trilha de auditoria antes da emissão de `resume()`.
  - Se a persistência falhar, `safe_terminate_tree` encerra o processo sem que nenhuma instrução do worker seja executada.

#### Isolamento de Sistema de Arquivos (Fases 4B.5.2 e 4B.6.3)
- **Windows**:
  - Isolamento por perfis AppContainer nativos (`CreateAppContainerProfile`) gerando SID restrito (`S-1-15-2-*`).
  - Permissões NTFS (DACLs) concedidas estritamente à pasta da sandbox da tarefa, com herança bloqueada.
  - Tabela restrita de handles herdados via `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, isolando completamente `stdin`/`stdout`/`stderr` sem vazamento de descritores privilegiados.
- **Linux**:
  - Landlock LSM (Linux 5.13+) com `prctl(PR_SET_NO_NEW_PRIVS)`.
  - Concessão de acesso de leitura/escrita delimitado estritamente à raiz da sandbox e leitura aos runtimes essenciais do sistema.
- **macOS**:
  - Apple Seatbelt Sandbox (`/usr/bin/sandbox-exec`) com perfil SBPL restritivo.
  - Política baseada em `(deny default)`, eliminando permissões globais e importando `system.sb` para primitivas do sistema.
  - Concessão de leitura restrita a runtimes essenciais (`/System`, `/usr`, `/Library`, `/opt`, `sys.prefix`, `sys.path`), à sandbox e a projetos declarados em `allowed_read_roots`.
  - Negação explícita para credenciais (`.ssh`, `.aws`, `.gnupg`), tarefas irmãs, banco SQLite e ativos confidenciais do supervisor (`auth/`, `backups/`, `checkpoints/`).
- **Proteção contra Escape e Reparse Points**:
  - Canonicalização rigorosa com `Path.resolve()`, detecção e bloqueio de symlinks maliciosos e junctions Win32 (`FILE_ATTRIBUTE_REPARSE_POINT`).

#### Tool Broker, Capability Tokens e Mailbox (Fases 4B.5.3, 4B.6.2 e 4B.6.3)
- **Negação por Padrão (Deny-by-default)**: Ferramentas não registradas ou não atribuídas explicitamente são recusadas preventivamente (`ToolAccessDeniedError`).
- **Capability Tokens Criptográficos (`CapabilityToken`)**:
  - Assinatura com HMAC-SHA256 vinculada a `task_id`, `run_id`, `project_root`, `allowed_tools` e `worker_pid`.
  - Verificação do timestamp de criação de kernel (`worker_create_time_ns`) com tolerância de 2.0s contra ataques de reciclagem de PID.
  - Controle atômico de cotas (`max_calls`), expiração temporal (`ttl_seconds`), limitação de tamanho de payload e revogação durável (`revoke_token`).
- **Políticas de Acesso (`ToolPolicyMode`)**:
  - `ALLOW`: ferramentas seguras de leitura de workspace ou cálculo de hash contidas à sandbox.
  - `DENY`: ferramentas proibidas para workers (leitura de segredos, consultas diretas a SQLite).
  - `REQUIRES_APPROVAL`: mutações de checkpoints ou rotas geram pendência (`approval_id`) exigindo consentimento humano.
- **Mailbox Assíncrona na Sandbox (`.broker/requests` e `.broker/responses`)**:
  - Troca de mensagens baseada em arquivos JSON para workers isolados sem acesso a rede ou IPC.
  - Inclusão do token em `.broker/token` e inspeção periódica via `SafeStreamConsumer.on_poll` a cada 50ms durante o streaming.
- **Distinção entre Falhas Operacionais e Violações Críticas**:
  - Erros operacionais (rate limits, aprovações pendentes, parâmetros inválidos) retornam resposta JSON na mailbox.
  - Violações críticas de segurança (spoofing de PID `CallerIdentityMismatchError`, token adulterado `InvalidCapabilityTokenError`, mutação não permitida `MutationNotPermittedError`, ataques de symlink `FileSandboxSecurityError` ou falhas de auditoria `ToolAuditPersistenceError`) acionam fail-closed imediato: o supervisor encerra a árvore de processos (`safe_terminate_tree`) e levanta `RuntimeError`.

#### Trilha de Auditoria Criptográfica Imutável (Fases 4B.5.4, 4B.5.5, 4B.6.0 e 4B.6.1)
- **Tabela Append-Only no SQLite (`worker_audit_log`)**:
  - Triggers SQLite `BEFORE UPDATE` e `BEFORE DELETE` abortando incondicionalmente qualquer tentativa de modificação ou exclusão de registros.
- **Carga de Hash Canônica de 15 Campos**:
  - Encadeamento contínuo `chain_hash = SHA256(...)` cobrindo determinísticamente todos os 15 campos da tabela `worker_audit_log`.
- **Arquitetura de Dupla Ancoragem**:
  - Tabela interna `worker_audit_anchors` protegida por triggers de imutabilidade.
  - Arquivo externo desacoplado `auth/audit.anchor` com permissões restritas (0700/0600) e sincronização atômica durável com `fsync()`.
  - Detecção imediata de manipulações offline e truncamento de cauda (*tail truncation*).
  - Reconciliação durável pós-queda (`reconcile_anchor()`) restaurando o arquivo de âncora a partir do SQLite auditado.
- **Ciclo de Vida Totalmente Auditado**:
  - Emissão determinística dos eventos `sandbox_prepared`, `spawn`, `exit`, `timeout` e `termination`.
  - Operação estritamente fail-closed: falha no registro de auditoria aborta a execução do worker sem deixar processos órfãos desauditados.

#### Compatibilidade e Migrações
- **Migração Idempotente da v0.4.0**:
  - Atualização transparente do schema SQLite, criando `worker_audit_log`, `worker_audit_anchors` e `tool_broker_tokens` e preservando intacto todo o histórico de tarefas, runs e backlog anteriores.
- **Suíte de Testes Multiplataforma**:
  - 313 testes automatizados executados na matriz de CI (Windows, Ubuntu Linux e macOS em Python 3.11 e 3.14).
  - Justificativa formal dos 3 testes ignorados em POSIX em `docs/test_matrix_and_skips.md`.

---

## [0.4.0] - 2026-10-15

### Adicionado
- **Fase 3: Controle Adaptativo de Trajetória, DAG Versionada e Fencing Criptográfico**:
  - Histórico de revisões de planos em grafo acíclico direcionado (`PlanRevision`, `PlanNode`, `PlanEdge`).
  - Motor preditivo de riscos de trajetória (`TrajectoryPredictor`) com telemetria antecipada.
  - Oráculo de invariantes (`InvariantOracle`) com evidências criptográficas HMAC-SHA256.
  - Livro-razão de recursos físicos (`ResourceLedger`) com backup prévio automático e compensação reversível.
  - Reconstrução adaptativa de rotas com emenda topológica (`TrajectoryReconstructor`).
  - Motor de insights e incubadora de hipóteses (`InsightEngine`, `InsightIncubator`).
  - Verificação de neutralização física de processos em ativação (`safe_process_alive`).

---

## [0.3.0] - 2026-09-20

### Adicionado
- Orquestração de tarefas compartilhadas, checkpoints periódicos e rotas configuráveis para workers locais.
- Protocolo unificado de colaboração e avaliação de candidatos (`benchmark`).

---

## [0.2.1] - 2026-09-05

### Corrigido
- Ajustes de compatibilidade com transporte stdio do protocolo MCP.
- Correções de codificação UTF-8 em ambientes Windows.

---

## [0.2.0] - 2026-08-15

### Adicionado
- Versão inicial da CLI `ai-coordinator`, biblioteca Python e servidor MCP de coordenação de tarefas.