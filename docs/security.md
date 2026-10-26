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

