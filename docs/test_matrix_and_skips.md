# Matriz de Testes Multiplataforma e Justificativa de Testes Ignorados (Skips)

Este documento detalha a matriz de execução de testes do **AI Coordinator** (Fase 4B), descrevendo os mecanismos de isolamento validados em cada sistema operacional e justificando formalmente os três testes ignorados (`skipped=3`) nas esteiras POSIX (Ubuntu Linux e macOS).

---

## 1. Matriz de Suporte e Ambientes de CI

A suíte de testes automatizados é executada em 7 ambientes paralelos no GitHub Actions:

| Ambiente | Runtime Python | Escopo Principal | Isolamento de Processo | Isolamento de Filesystem |
| :--- | :--- | :--- | :--- | :--- |
| **Windows Latest** | Python 3.11 | Suíte completa + Win32 | Win32 Job Objects (`CREATE_SUSPENDED`) | Windows AppContainer Isolation |
| **Windows Latest** | Python 3.14 | Suíte completa + Win32 | Win32 Job Objects (`CREATE_SUSPENDED`) | Windows AppContainer Isolation |
| **Ubuntu Latest** | Python 3.11 | Suíte completa + POSIX | Process Groups + Pipe Trampoline (`-I -S`) | Linux Landlock ABI v1-v4 / Fallback |
| **Ubuntu Latest** | Python 3.14 | Suíte completa + POSIX | Process Groups + Pipe Trampoline (`-I -S`) | Linux Landlock ABI v1-v4 / Fallback |
| **macOS Latest** | Python 3.11 | Suíte completa + POSIX | Process Groups + Pipe Trampoline (`-I -S`) | macOS Seatbelt (`sandbox-exec`) |
| **macOS Latest** | Python 3.14 | Suíte completa + POSIX | Process Groups + Pipe Trampoline (`-I -S`) | macOS Seatbelt (`sandbox-exec`) |
| **OpenCode V2** | Node / Linux | Integração de launcher | Process Groups | Delimitação de CLI headless |

---

## 2. Justificativa Técnica dos Testes Ignorados (`skipped=3`)

Em plataformas POSIX (Ubuntu e macOS), o relatório de execução do `unittest` registra exatamente `OK (skipped=3)`.
Esses 3 skips correspondem exclusivamente a recursos nativos do kernel **Windows NT (Win32 Job Objects)** que não possuem representação na API POSIX, e cujos requisitos de contenção e fail-closed são rigorosamente cobertos por implementações POSIX equivalentes em testes universais.

### Detalhamento dos 3 Testes Específicos:

### 1. `tests/test_process_lifecycle.py:68` — `test_job_object_kill_on_close_fail_closed`
* **Condição de Skip**: `if os.name != "nt": self.skipTest("Job Objects are specific to Windows NT")`
* **Justificativa**: Valida o flag nativo do kernel NT `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, garantindo que quando o handle do Job Object for fechado pelo supervisor (mesmo por encerramento anormal ou `sys.exit`), o kernel do Windows encerra atomicamente todos os processos membros.
* **Cobertura POSIX Equivalente**: Em Linux e macOS, a contenção do ciclo de vida e término em cascata é exercida e validada por:
  - `tests/test_process_lifecycle.py:test_safe_terminate_tree_with_descendants` (valida terminação de árvore via process group e `os.killpg`);
  - `tests/test_phase4b_process_sandbox.py:test_04_descendant_processes_contained_and_killed_on_close` (executado universalmente em Windows, Linux e macOS);
  - `tests/test_phase4b_adversarial_suite.py:test_01_adversarial_process_spawning_bomb_contained` (bomba de forks contida em todas as plataformas).

### 2. `tests/test_phase4b_process_sandbox.py:74` — `test_02_windows_job_object_containment_before_execution`
* **Condição de Skip**: `if os.name != "nt": self.skipTest("Teste aplicavel exclusivamente ao Windows NT")`
* **Justificativa**: Testa a atribuição da primitiva `WindowsProcessController.assign_to_job` ao processo worker suspenso antes da liberação da thread primária.
* **Cobertura POSIX Equivalente**: No POSIX, a inicialização em duas etapas suspensa é realizada pelo Trampoline seguro (`ProcessSupervisor.spawn(..., start_suspended=True)`) com canal de sincronização via pipe unifilar e interpretador intermediário isolado (`sys.executable -I -S -c ...`). Esse mecanismo é universalmente validado em:
  - `tests/test_phase4b_multiplatform_regression.py:test_04_posix_isolated_trampoline_command_construction`
  - `tests/test_phase4b_adversarial_suite.py:test_02_adversarial_pre_audit_execution_blocked_by_two_phase_start`

### 3. `tests/test_phase4b_process_sandbox.py:91` — `test_03_fail_closed_when_job_object_or_sandbox_fails`
* **Condição de Skip**: `if os.name != "nt": self.skipTest("Teste aplicavel exclusivamente ao Windows NT")`
* **Justificativa**: Simula falha do kernel Win32 ao instanciar o Job Object (`create_job_object` retornando `None`), comprovando que o supervisor segue a política *fail-closed* e aborta a inicialização do worker com erro imediato.
* **Cobertura POSIX Equivalente**: Em POSIX, a política *fail-closed* para falhas de sandbox no Linux e macOS é validada universalmente por:
  - `tests/test_phase4b_filesystem_sandbox.py:test_09_fail_closed_when_os_boundary_unavailable_in_enforced_mode` (assegura que se Landlock ou Seatbelt falharem em modo obrigatório, o supervisor aborta a inicialização antes de executar qualquer código não-confiável).

---

## 3. Matriz de Equivalência de Segurança

| Requisito de Segurança (Gate) | Windows NT Implementation | POSIX (Linux/macOS) Implementation |
| :--- | :--- | :--- |
| **G1 — Inicialização Auditada** | `CreateProcessW` com `CREATE_SUSPENDED`, persistência e HMAC de auditoria, e `NtResumeProcess` | Sincronização via Pipe unifilar + Trampoline `-I -S`, persistência e HMAC de auditoria, e liberação com token `b'G'` |
| **G2 — Contenção de Processos** | Win32 Job Object com `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` e verificação de árvore | Process Groups (`setpgid`), `killpg(SIGTERM/SIGKILL)` e árvore de criação com timestamp |
| **G3 — Broker e Mailbox** | Polling não-bloqueante (`SafeStreamConsumer.on_poll`) e troca de mensagens em diretório `.broker` | Idêntico em todas as plataformas |
| **G4 — Memória Externa (ai-memory)** | CLI/SQLite com validação estrita de UUID e semântica resiliente | Idêntico em todas as plataformas |
| **G5 — Isolamento de Filesystem** | Windows AppContainer Security Capabilities (`PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`) | Linux Landlock (regras de path read/write) / macOS Seatbelt sandbox |
| **G6 — Chave HMAC e Âncora** | Proteção de arquivo externo `auth/audit.anchor` com permissões restritas e lockfile atômico | Idêntico em todas as plataformas |
| **G7 — Integridade da Auditoria** | Tabela SQLite `worker_audit_log` com triggers `BEFORE UPDATE` e `BEFORE DELETE` preventivos | Idêntico em todas as plataformas |
| **G8 — Recuperação e Reconciliação** | Reconciliação pós-queda com `reconcile_anchor()` e persistência de revogação de tokens | Idêntico em todas as plataformas |
