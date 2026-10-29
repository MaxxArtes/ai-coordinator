# Matriz de Testes Multiplataforma e Justificativa de Testes Ignorados (Skips)

Este documento detalha a matriz de execução de testes do **AI Coordinator** (Fase 4B), descrevendo os mecanismos de isolamento validados em cada sistema operacional e justificando formalmente a distribuição exata dos testes ignorados nos relatórios da suíte de testes (313 testes no total): **4 ignorados no Ubuntu Linux**, **3 ignorados no macOS** e **1 ignorado no Windows**.

---

## 1. Matriz de Suporte e Ambientes de CI

A suíte de testes automatizados é executada em 7 ambientes paralelos no GitHub Actions:

| Ambiente | Runtime Python | Total de Testes | Executados | Skips | Isolamento de Processo | Isolamento de Filesystem |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Windows Latest** | Python 3.11 | 313 | 312 | **1** | Win32 Job Objects (`CREATE_SUSPENDED`) | Windows AppContainer Isolation |
| **Windows Latest** | Python 3.14 | 313 | 312 | **1** | Win32 Job Objects (`CREATE_SUSPENDED`) | Windows AppContainer Isolation |
| **Ubuntu Latest** | Python 3.11 | 313 | 309 | **4** | Process Groups + Pipe Trampoline (`-I -S`) | Linux Landlock ABI v1-v4 / Fallback |
| **Ubuntu Latest** | Python 3.14 | 313 | 309 | **4** | Process Groups + Pipe Trampoline (`-I -S`) | Linux Landlock ABI v1-v4 / Fallback |
| **macOS Latest** | Python 3.11 | 313 | 310 | **3** | Process Groups + Pipe Trampoline (`-I -S`) | macOS Seatbelt (`sandbox-exec`) |
| **macOS Latest** | Python 3.14 | 313 | 310 | **3** | Process Groups + Pipe Trampoline (`-I -S`) | macOS Seatbelt (`sandbox-exec`) |
| **OpenCode V2** | Node / Linux | N/A | N/A | N/A | Process Groups | Delimitação de CLI headless |

---

## 2. Justificativa Técnica e Distribuição dos Testes Ignorados

A suíte possui exatamente 4 testes com condições explícitas de skip baseadas na plataforma de execução do sistema operacional.

### Resumo por Plataforma:
- **Ubuntu Linux (`skipped=4`)**: Os 3 testes específicos do kernel Windows NT + 1 teste operacional exclusivo do macOS Seatbelt.
- **macOS (`skipped=3`)**: Os 3 testes específicos do kernel Windows NT. O teste operacional do macOS Seatbelt executa e é validado com sucesso.
- **Windows (`skipped=1`)**: O teste operacional exclusivo do macOS Seatbelt. Todos os 3 testes nativos de Win32 Job Objects executam e são validados com sucesso.

---

### Detalhamento dos 4 Testes Específicos:

#### 1. `tests/test_process_lifecycle.py:68` — `test_job_object_kill_on_close_fail_closed`
* **Condição de Skip**: `if os.name != "nt": self.skipTest("Job Objects are specific to Windows NT")`
* **Plataformas que ignoram**: Ubuntu Linux e macOS (2 ambientes).
* **Justificativa**: Valida o flag nativo do kernel NT `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, garantindo que quando o handle do Job Object for fechado pelo supervisor (mesmo por encerramento anormal ou `sys.exit`), o kernel do Windows encerra atomicamente todos os processos membros.
* **Cobertura POSIX Equivalente**: Em Linux e macOS, a contenção do ciclo de vida e término em cascata é exercida e validada por:
  - `tests/test_process_lifecycle.py:test_safe_terminate_tree_with_descendants` (valida terminação de árvore via process group e `os.killpg`);
  - `tests/test_phase4b_process_sandbox.py:test_04_descendant_processes_contained_and_killed_on_close` (executado universalmente em Windows, Linux e macOS);
  - `tests/test_phase4b_adversarial_suite.py:test_01_adversarial_process_spawning_bomb_contained` (bomba de forks contida em todas as plataformas).

#### 2. `tests/test_phase4b_process_sandbox.py:74` — `test_02_windows_job_object_containment_before_execution`
* **Condição de Skip**: `if os.name != "nt": self.skipTest("Teste aplicavel exclusivamente ao Windows NT")`
* **Plataformas que ignoram**: Ubuntu Linux e macOS (2 ambientes).
* **Justificativa**: Testa a atribuição da primitiva `WindowsProcessController.assign_to_job` ao processo worker suspenso antes da liberação da thread primária.
* **Cobertura POSIX Equivalente**: No POSIX, a inicialização em duas etapas suspensa é realizada pelo Trampoline seguro (`ProcessSupervisor.spawn(..., start_suspended=True)`) com canal de sincronização via pipe unifilar e interpretador intermediário isolado (`sys.executable -I -S -c ...`). Esse mecanismo é universalmente validado em:
  - `tests/test_phase4b_multiplatform_regression.py:test_04_posix_isolated_trampoline_command_construction`
  - `tests/test_phase4b_adversarial_suite.py:test_02_adversarial_pre_audit_execution_blocked_by_two_phase_start`

#### 3. `tests/test_phase4b_process_sandbox.py:91` — `test_03_fail_closed_when_job_object_or_sandbox_fails`
* **Condição de Skip**: `if os.name != "nt": self.skipTest("Teste aplicavel exclusivamente ao Windows NT")`
* **Plataformas que ignoram**: Ubuntu Linux e macOS (2 ambientes).
* **Justificativa**: Simula falha do kernel Win32 ao instanciar o Job Object (`create_job_object` retornando `None`), comprovando que o supervisor segue a política *fail-closed* e aborta a inicialização do worker com erro imediato.
* **Cobertura POSIX Equivalente**: Em POSIX, a política *fail-closed* para falhas de sandbox no Linux e macOS é validada universalmente por:
  - `tests/test_phase4b_filesystem_sandbox.py:test_09_fail_closed_when_os_boundary_unavailable_in_enforced_mode` (assegura que se Landlock ou Seatbelt falharem em modo obrigatório, o supervisor aborta a inicialização antes de executar qualquer código não-confiável).

#### 4. `tests/test_phase4b_multiplatform_regression.py:467` — `test_08_macos_seatbelt_operational_containment_and_rejection`
* **Condição de Skip**: `if sys.platform != "darwin": self.skipTest("Operational macOS Seatbelt enforcement test only runs on darwin")`
* **Plataformas que ignoram**: Ubuntu Linux e Windows (2 ambientes).
* **Executado e Aprovado**: macOS Latest (Python 3.11 e 3.14).
* **Justificativa**: Executa `/usr/bin/sandbox-exec` diretamente com o perfil SBPL gerado por `MacOsSeatbeltController.build_profile()`. Comprova que:
  1. A leitura de arquivos autorizados em `allowed_read_roots` tem sucesso.
  2. A leitura e escrita na `sandbox_root` têm sucesso.
  3. A leitura de segredos confidenciais do supervisor (`auth/supervisor.secret`) é categoricamente bloqueada com `PermissionError` / `OSError`.
  4. A leitura de arquivo arbitrário fora de `system_roots` (posicionado sob `/Users/Shared` / `Path.home()`) é rejeitada por `(deny default)`, comprovando a eficácia prática da contenção de menor privilégio no macOS.
* **Cobertura Equivalente em Outros SOs**:
  - No Windows: testado operacionalmente via AppContainer em `tests/test_phase4b_filesystem_sandbox.py` e `tests/test_phase4b_adversarial_suite.py:test_06_adversarial_coordinator_private_directory_inaccessible`.
  - No Linux: testado operacionalmente via Landlock LSM em `tests/test_phase4b_filesystem_sandbox.py:test_03_linux_landlock_rule_generation_and_denial`.

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
