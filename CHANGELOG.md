# Changelog

Todas as alterações notáveis deste projeto serão documentadas neste arquivo.

O formato é baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/),
e este projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

---

## [0.5.0] - Não publicado / Release Candidate (2026-10-28)

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