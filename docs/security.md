# Limites de segurança e isolamento dos workers

A Fase 4B.5 do AI Coordinator introduz isolamento de processos e governança a nível de sistema operacional (OS-level sandboxing) para execução supervisionada de workers.

## Camadas de Isolamento e Contenção (Fase 4B.5)

### 1. Contenção de Processos e Limites de Recursos (4B.5.1)
- **Windows**: Criação de subprocessos suspensos (`CREATE_SUSPENDED`), inserção atômica em Win32 Job Object antes de iniciar qualquer código (`AssignProcessToJobObject`) e resumo seguro via `ntdll.NtResumeProcess`. Limites de memória (`JobMemoryLimit`), limite de processos ativos (`ActiveProcessLimit`) e terminação atômica da árvore de descendentes com `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
- **POSIX (Linux / macOS)**: Sessões isoladas (`start_new_session=True`), terminação atômica de grupos de processos (`os.killpg`) com verificação rigorosa de tempos de criação de kernel (`/proc/stat` e `ps`) para evitar reciclagem acidental de PIDs. Limites de recursos aplicados via `resource.setrlimit` (RLIMIT_AS, RLIMIT_CPU, RLIMIT_NPROC).
- **Fail-Closed**: Caso a criação ou vinculação da sandbox de processos falhe, o processo é abortado imediatamente (`TerminateProcess` no Windows ou `_exit` no POSIX) sem executar nenhuma instrução desprotegida.

### 2. Isolamento do Sistema de Arquivos e Permissões (4B.5.2)
- **Windows**: Isolamento via perfil AppContainer (`CreateAppContainerProfile` gerando SID restrito `S-1-15-2-*`). Permissões NTFS (DACLS) concedidas exclusivamente ao diretório temporário da sandbox da tarefa. O kernel NT bloqueia incondicionalmente (`Access Denied / PermissionError`) tentativas do worker de ler ou gravar em arquivos do coordenador (`coordination.sqlite`, arquivos WAL/SHM, `auth/supervisor.secret`, `auth/supervisor.key`, backups e diretórios de tarefas irmãs). Suporte a `stdin` nativo via atributo `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, restringindo a tabela de handles herdados estritamente aos descritores do canal de entrada sem vazamento de handles privilegiados.
- **Linux**: Isolamento nativo via Landlock LSM (Linux 5.13+) com `prctl(PR_SET_NO_NEW_PRIVS)`. O kernel restringe operações de filesystem exclusivamente à raiz da sandbox e aos caminhos essenciais de runtime (`/usr`, `/lib`, `/opt`), bloqueando tentativas de ler arquivos privados do supervisor ou do perfil do usuário. Falhas na configuração do Landlock interrompem a execução imediatamente (fail-closed).
- **macOS**: Isolamento via Apple Seatbelt Sandbox (`/usr/bin/sandbox-exec` / `libsystem_sandbox`) com perfis SBPL que restringem leitura/escrita à sandbox e bloqueiam caminhos confidenciais do coordenador.
- **Defesa contra Traversal e Reparse Points**: Canonicalização estrita (`Path.resolve()`), detecção e bloqueio de tentativas de escape com `..`, caminhos absolutos fora da raiz autorizada e links simbólicos / junctions (`FILE_ATTRIBUTE_REPARSE_POINT`).

### 3. Broker de Ferramentas, Autorização e Mediação (4B.5.3)
- **Negação por Padrão (Deny-by-default)**: Nenhuma ferramenta pode ser executada por um worker sem estar expressamente registrada no `ToolRegistry` e autorizada no escopo do token de capacidade emitido para a tarefa.
- **Tokens de Capacidade Criptográficos (`CapabilityToken`)**:
  - Assinados com HMAC-SHA256 usando a credencial secreta do supervisor (`supervisor_key`).
  - Vinculados a identificadores contextuais imutáveis: `task_id`, `run_id`, `project_root`, `allowed_tools` e `worker_pid`.
  - Proteção contra reciclagem de PIDs (PID recycling / TOCTOU) por validação de `worker_create_time_ns` obtido diretamente do kernel do sistema operacional.
  - Validade temporal estrita (`ttl_seconds`) com expiração automática (`CapabilityTokenExpiredError`).
  - Proteção contra replay com nonces criptográficos e mecanismo de revogação imediata (`revoke_token`).
- **Políticas por Ferramenta (`ToolPolicyMode`)**:
  - `ALLOW`: ferramentas seguras de leitura restrita ou utilitários puros (`workspace_read_file`, `workspace_list_dir`, `calculate_hash`, `echo_payload`), contidas estritamente à raiz do projeto/sandbox.
  - `DENY`: ferramentas estritamente proibidas para workers (`read_supervisor_secret`, `raw_sqlite_query`), com bloqueio incondicional.
  - `REQUIRES_APPROVAL`: operações de alta relevância ou mutações (como modificação de checkpoints cognitivos), gerando um `approval_id` pendente e exigindo aprovação humana explícita para prosseguir.
- **Proteção Estrita de Mutações**: Os workers têm bloqueio estrito de escrita em diretórios privados do coordenador, no banco SQLite `coordination.sqlite` e em checkpoints persistidos.
- **Limites Operacionais**: Controle de volume (`max_calls` por token) e contenção de tráfego (`max_payload_bytes` por requisição).
- **Trilha de Auditoria em SQLite (`tool_broker_audit`)**: Registro persistente e rastreável de todas as invocações, contendo correlation IDs, timestamps ISO 8601, identificadores de tarefa e worker, decisões tomadas (`executed`, `denied`, `invalid_token`, `rate_limited`, `requires_approval`), tempo de execução em milissegundos e sumário de parâmetros/resultados.
- **Mailbox na Sandbox de Workers (`.broker/requests` e `.broker/responses`)**: Suporte a mediação assíncrona por arquivos para workers totalmente isolados que não possuem acesso direto a sockets ou IPC direto com o supervisor.

### 4. Auditoria de Execução de Workers e Endurecimento do Broker (4B.5.4)
- **Persistência SQLite de Tokens e Revogações (`tool_broker_tokens`)**: Tokens emitidos, contadores atômicos de chamadas (`calls_made`) e revogações persistem no banco de dados, impedindo revalidação indevida de tokens revogados após reinício do Store ou estouro de limites sob concorrência.
- **Identidade e Anti-Spoofing na Mailbox**: O supervisor valida estritamente a identidade do processo worker da sandbox (`expected_worker_pid`). O PID não é inferido de campos declarativos não confiáveis no JSON da requisição.
- **Proteção Anti-Symlink na Mailbox**: O diretório `.broker/requests` e `.broker/responses` é protegido contra reparse points e symlinks (`is_reparse_point_or_symlink` / `validate_contained_path`), impedindo escapes de leitura ou escrita fora da sandbox.
- **Normalização de Timestamp de Criação de Processos**: Comparação precisa em nanossegundos Unix (convertendo FILETIME de 100ns do Windows e jiffies do Linux), garantindo tolerância uniforme de 2.0 segundos contra reciclagem de PIDs em todas as plataformas.
- **Imutabilidade SQL com Triggers**: As tabelas `worker_audit_log` e `tool_broker_audit` possuem triggers SQLite (`BEFORE UPDATE` e `BEFORE DELETE` acionando `RAISE(ABORT, ...)`), garantindo natureza estritamente append-only contra adulterações.
- **Política de Falha Fechada (Fail-Closed)**: Operações de workers e ferramentas não são executadas se a persistência da trilha de auditoria falhar (`ToolAuditPersistenceError`, `WorkerAuditPersistenceError`).

### 5. Consolidação e Testes Adversariais Integrados (4B.5.5)
- **Auditoria Integrada no Ciclo de Vida Real do Worker**: Emissão automática e determinística de eventos no fluxo de execução supervisionada (`cli.py:run_worker`):
  - `sandbox_prepared`: emitida após a montagem e restrição de permissões no filesystem (AppContainer / Landlock / Seatbelt).
  - `sandbox_denied`: emitida se a inicialização de segurança falhar.
  - `spawn`: emitida com o PID real do worker, identidade extraída do kernel, limites operacionais e parâmetros de rota.
  - `timeout`: emitida se o tempo limite de execução for excedido.
  - `termination`: emitida se a árvore de processos for ativamente terminada via `terminate_tree`.
  - `exit`: emitida com o código de retorno, duração da execução e sumário de sucesso ou falha.
- **Assinatura Canônica de Todos os Campos do Registro**:
  - A carga de hash (`chain_hash`) cobre canônica e determinísticamente todos os 15 campos da tabela `worker_audit_log`: `prev_hash`, `sequence_number`, `task_id`, `run_id`, `worker_pid`, `process_identity_json`, `event_type`, `correlation_id`, `operational_revision`, `limits_json`, `resource_usage_json`, `reason`, `error`, `details_json` e `created_at`.
  - Adulterações em qualquer campo (mesmo offline com triggers desativados no SQLite) quebram imediatamente a integridade criptográfica.
- **Encadeamento Sequencial Global Estrito**:
  - Numeração linear e encadeamento contínuo de ponta a ponta para toda a base `worker_audit_log` (`sequence_number = MAX(sequence_number) + 1`), eliminando partições por run ou gaps de verificação.
- **Tabela de Ancoragem contra Truncamento de Cauda (`worker_audit_anchors`)**:
  - Registro de integridade cruzada que armazena o último `chain_hash` assinado com HMAC em tabela separada protegida por triggers de imutabilidade.
  - `verify_integrity()` detecta tentativas de exclusão de registros recentes (tail truncation) comparando a maior sequência do log com a âncora assinada.

### 6. Fechamento de Bloqueadores e Ancoragem Desacoplada (4B.6.0)
- **Eliminação de Fallback Silencioso e Fail-Closed no Ciclo do Worker**:
  - No módulo `cli.py`, todas as chamadas a `store.worker_audit.record_event()` executam sob política estritamente fail-closed:
    - Se o registro de `sandbox_prepared` falhar, o worker é abortado antes da criação do processo (`ProcessSupervisor.spawn`).
    - Se a gravação de `spawn` falhar após a criação do subprocesso, o supervisor invoca imediatamente `terminate_tree(proc)` e levanta `RuntimeError`, impedindo qualquer execução de comandos sem certidão de auditoria persistida.
    - Se a persistência de `exit` falhar, o término é considerado violado e o resultado da execução é descartado.
- **Arquitetura de Dupla Ancoragem Desacoplada do SQLite (`auth/audit.anchor`)**:
  - **Limitação da Ancoragem In-Database**: Caso as âncoras fossem armazenadas exclusivamente no arquivo `coordination.sqlite`, um adversário com privilégio de escrita física no banco de dados e capacidade de remover triggers SQLite poderia simultaneamente truncar o log e a tabela de âncoras.
  - **Mitigação com Âncora Externa Isolada**: A cada inserção de evento, o `WorkerAuditLogger` grava atomicamente o estado da âncora no arquivo externo desacoplado `store.root / "auth" / "audit.anchor"`, protegido por permissões NTFS/POSIX restritas (0700/0600) e inacessível às sandboxes de workers.
  - **Detecção Híbrida**: O método `verify_integrity()` valida a coerência sequencial tanto contra a tabela interna `worker_audit_anchors` quanto contra o arquivo externo desacoplado `auth/audit.anchor`. Se o banco for manipulado offline para remover eventos recentes, o arquivo externo de âncora detecta e reporta imediatamente o truncamento.
- **Modelo de Ameaças e Limites Teóricos**:
  - Contra processos workers em sandbox (AppContainer / Landlock / Seatbelt): o isolamento do kernel impede acesso de leitura e escrita ao banco e ao diretório `auth/`.
  - Contra adulteração offline em disco: a assinatura HMAC com segredo mestre (`supervisor_secret`) impede forja de hashes ou assinaturas válidas sem conhecimento da chave.
  - Limite inerente de SO: Se um adversário obtiver credenciais de `root` ou `Administrador` no host do coordenador e comprometer a chave em memória ou `auth/supervisor.secret`, o host é considerado comprometido além das fronteiras de governança em nível de aplicação.

### 7. Validação E2E Completa e Protocolo em Duas Fases (4B.6.1)
- **Protocolo de Inicialização em Duas Fases (Gate G1: Inicialização Auditada)**:
  - Eliminação da janela de execução não auditada: O `ProcessSupervisor.spawn()` suporta o parâmetro `start_suspended=True` (obrigatório para execuções de workers em produção via `cli.py`).
  - No Windows, o processo é criado com a flag de kernel `CREATE_SUSPENDED`, imediatamente vinculado ao Job Object com limites de cotas e mantido suspenso.
  - No POSIX (Linux/macOS), a contenção inicial utiliza sincronização unidirecional antes da chamada `execvp` do código do worker.
  - Sequência rigorosa:
    1. Preparação da sandbox e persistência de `sandbox_prepared`.
    2. Criação do processo bloqueado no SO pelo supervisor.
    3. Persistência de PID, identidade estável do kernel e evento `spawn` na trilha criptográfica com HMAC e sincronização com `fsync()` no arquivo desacoplado `auth/audit.anchor`.
    4. Liberação da execução (`proc.resume()`) **estritamente após** a confirmação do commit de auditoria.
    5. Se a persistência falhar, `terminate_tree(proc)` encerra o processo ainda suspenso sem que **nenhuma instrução** do worker seja executada (garantia validada experimentalmente via teste adversarial de arquivo sentinela).
- **Âncora Externa Obrigatória e Durabilidade (Gate G6: Integridade)**:
  - O arquivo externo `auth/audit.anchor` é de presença obrigatória para a verificação de integridade caso existam registros na trilha de auditoria. A sua remoção arbitrária invalida imediatamente o status de integridade.
  - Gravação durável atômica utilizando buffer temporário, `flush()`, `os.fsync()` e substituição atômica via `replace`, com permissões restritas `0700` no diretório e `0600` no arquivo em POSIX.
  - Método de recuperação e reconciliação durável (`reconcile_anchor()`): caso ocorra queda ou encerramento inesperado entre o commit SQLite e a atualização do arquivo, o coordenador pode reconstruir e sincronizar a âncora a partir do registro autenticado na tabela `worker_audit_anchors`.
- **Garantia dos 8 Gates de Segurança (G1 a G8)**:
  - **G1 — Inicialização auditada**: Nenhum worker executa instruções antes da confirmação e persistência do evento de spawn.
  - **G2 — Sandbox efetiva**: Isolamento do filesystem (AppContainer / Landlock / Seatbelt) e contenção de Job Object mantêm segredos e arquivos do coordenador estritamente inacessíveis.
  - **G3 — Broker integrado**: Solicitações via mailbox de arquivos `.broker` passam por validação obrigatória de caller PID, token de capacidade HMAC, cotas por ferramenta e auditoria pré e pós execução.
  - **G4 — Memória real**: Recuperação e isolamento do ai-memory com escopo comprovado na origem e tratamento de dados recuperados como não-confiáveis.
  - **G5 — Ciclo de vida**: Eventos de `sandbox_prepared`, `spawn`, `exit`, `timeout` e `termination` preservados em sequência cronológica linear auditável.
  - **G6 — Integridade**: Encadeamento criptográfico SHA-256 e HMAC-SHA256 à prova de truncamento, remoção e adulteração por dupla ancoragem.
  - **G7 — Compatibilidade**: Roteamento consistente e launchers testados em matriz multiplataforma (Windows, Linux, macOS).
  - **G8 — Recuperação**: Reinício e re-instanciação do supervisor preservam histórico, autorizações de tokens e integridade.
- **Auditoria Pré-Execução Fail-Closed no Tool Broker**:
  - Antes de invocar qualquer handler de ferramenta mutante ou de leitura, o broker persiste e comita o registro de autorização (`decision="allowed"`).
  - Caso ocorra falha de I/O, bloqueio ou persistência SQLite, o handler **nunca** é executado, impedindo categoricamente qualquer efeito colateral desprotegido.
  - Após a execução do handler, o broker grava o evento de conclusão (`completed`) ou erro (`error`) com sumário e duração.
- **Identidade Obrigatória na Mailbox de Arquivos**:
  - `process_mailbox` exige obrigatoriamente `expected_worker_pid: int` positivo, eliminando qualquer fallback permissivo para PIDs declarados em payloads JSON. Divergências entre o PID esperado da sandbox e o PID alegado são bloqueadas como ataques de spoofing (`CallerIdentityMismatchError`).
- **Concorrência Segura e Proteção Multithread no Banco de Dados**:
  - Conexão SQLite compartilhada no Store configurada com `check_same_thread=False` e sincronizada via `threading.RLock()` reentrante em `Store`, `ToolBroker` e `CapabilityTokenManager`.
  - Controle de cotas (`max_calls`) atômico com serialização thread-safe, impedindo race conditions sob alta carga simultânea.
- **Suíte de Testes Adversariais Integrados (`tests/test_phase4b_adversarial_suite.py`)**:
  - Bateria automatizada cobrindo testes ponta a ponta de delegação real, fail-closed pré-efeito, adulteração de campos canônicos, truncamento de log, spoofing de mailbox, contenção concorrente e isolamento dos arquivos privados do coordenador.

## Ambiente do processo

`worker_environment.mode` controla as variáveis herdadas:

- `allowlist` é o padrão. Passa variáveis básicas de sistema, caminho do usuário e diretórios de configuração. Nenhum outro valor do ambiente é copiado automaticamente.
- `allow` acrescenta nomes globais de variáveis.
- `allow_by_client` acrescenta nomes somente para o cliente indicado. Use esta opção para uma chave de API que o cliente realmente leia do ambiente.
- `inherit` repassa o ambiente do host, mas **expurga incondicionalmente** credenciais do coordenador e do supervisor (`AI_COORDINATOR_SUPERVISOR_*`, `AI_MEMORY_API_TOKEN`, etc.).
- No Windows com AppContainer, o bloco de ambiente é passado explicitamente em formato Unicode (`CREATE_UNICODE_ENVIRONMENT`), impedindo vazamento de variáveis não sanitizadas do processo pai.

## Garantias e Limitações por Plataforma

- **Ambientes com Suporte a Sandbox de SO**: Windows 8+/Server 2016+ (AppContainer), Linux 5.13+ (Landlock LSM habilitado no kernel) e macOS (Seatbelt Sandbox). Nestes ambientes, a sandbox é imposta pelo kernel do sistema operacional, bloqueando leituras mesmo que o worker ignore regras Python.
- **Ambientes Legados ou Containers Restritivos**: Em ambientes onde o kernel não oferece suporte ou onde o host bloqueia syscalls de segurança (por exemplo, containers Docker antigos sem privilégios ou sem Landlock compilado), o modo seguro com `enforce_filesystem_sandbox=True` falha de forma fechada. No modo compatibilidade, a governança recai sobre a validação de caminhos (`allowed_roots`), sanitização estrita de variáveis e filtros de contrato do launcher.

