# ai-coordinator

Estado de tarefas compartilhado entre agentes de programação e operadores humanos, com retomada por checkpoints cognitivos, rotas configuráveis, avaliação comparável de candidatos, governança de ferramentas via Tool Broker com tokens HMAC e contenção física de workers em sandboxes nativas do sistema operacional. O núcleo funciona em Windows, macOS e Linux com Python `3.11` ou superior, e o **AI Coordinator Desktop** oferece interface gráfica nativa em Windows (Pre-release Alpha `0.6.1-alpha.52`).

---

## Instalação Rápida (Escolha sua modalidade)

> [!NOTE]
> A versão estável oficial do **Core / CLI / MCP** é a **[`v0.6.0`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.0)**. O **Desktop** está disponível como **Pre-release Alpha ([`v0.6.1-alpha.52`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.1-alpha.52))** para Windows x64. Um instalador gráfico unificado único (com seleção completa/desktop/CLI/personalizada e opção offline) está planejado para o **Marco 4D.6**.

| Modalidade | O que inclui hoje | Status | Como começar |
| --- | --- | --- | --- |
| **Experiência Completa** | Desktop `0.6.1-alpha.52` (Core `0.6.0` e Python `3.11.9` embarcados) + CLI/MCP `v0.6.0` no terminal + plugin do seu agente | Disponível em etapas separadas (instalador único planejado na `4D.6`) | [Guia completo](docs/installation.md#3-instalação-completa-com-os-componentes-homologados-hoje) |
| **Somente Desktop** | Aplicativo gráfico Windows x64 (Conversas ConPTY, Configurações e Monitoramento), sem precisar de Python/Node globais | Pre-release Alpha (`0.6.1-alpha.52`, Windows x64) | [Release `v0.6.1-alpha.52`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.1-alpha.52) · [Guia Desktop](docs/installation.md#4-instalação-individual--ai-coordinator-desktop-windows-x64-pre-release) |
| **Somente Terminal / CLI + MCP** | Core Python `v0.6.0`, CLI e servidor MCP `stdio` | Estável (`v0.6.0`, Windows / macOS / Linux) | `python -m pip install "ai-coordinator[mcp]==0.6.0"` |
| **Personalizada por Agente** | Plugin Claude Code, Plugin OpenCode V2, `SKILL.md` ou receita MCP para Codex/AGY | Empacotado / Documentado (`v0.6.0`) | [Guia de integrações](docs/installation.md#6-integração-com-agentes-já-instalados) |

### Já tenho o Claude Code instalado — como testar em 3 comandos?

Se você já utiliza o **Claude Code**, não precisa reconfigurar suas chaves nem instalar outros provedores de IA:

```console
python -m pip install "ai-coordinator[mcp]==0.6.0"
claude plugin marketplace add MaxxArtes/ai-coordinator
claude plugin install ai-coordinator@ai-coordinator-marketplace
```

Depois, dentro da pasta do seu projeto:

```console
ai-coordinator bootstrap --project .
```

Suas credenciais e configurações existentes permanecem intocadas, e nenhum provedor externo é instalado ou ativado sem autorização explícita.

### Downloads Diretos

- **Desktop Windows x64 (`0.6.1-alpha.52`, Pre-release):**
  - [Instalador NSIS (`AI-Coordinator-Desktop_0.6.1-alpha.52_x64-setup.exe`)](https://github.com/MaxxArtes/ai-coordinator/releases/download/v0.6.1-alpha.52/AI-Coordinator-Desktop_0.6.1-alpha.52_x64-setup.exe) · [Assinatura `.sig`](https://github.com/MaxxArtes/ai-coordinator/releases/download/v0.6.1-alpha.52/AI-Coordinator-Desktop_0.6.1-alpha.52_x64-setup.exe.sig)
  - [Pacote Portátil (`ai-coordinator-desktop-alpha-portable.zip`)](https://github.com/MaxxArtes/ai-coordinator/releases/download/v0.6.1-alpha.52/ai-coordinator-desktop-alpha-portable.zip) · [Checksums SHA-256](https://github.com/MaxxArtes/ai-coordinator/releases/download/v0.6.1-alpha.52/checksums-desktop-0.6.1-alpha.52.sha256)
  - *Aviso de assinatura:* o instalador possui assinatura criptográfica **Minisign/Ed25519** (`499FF8912D665FE8`), porém **não** possui assinatura Microsoft Authenticode (`NotSigned`), podendo acionar o aviso do SmartScreen no Windows. Confira o SHA-256 antes de executar.
- **Core Python, CLI, MCP, Skills e Plugins (`v0.6.0`, Estável):**
  - [Release `v0.6.0` no GitHub](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.0) · [Pacote no PyPI](https://pypi.org/project/ai-coordinator/)
- **Documentação:** [Guia Completo de Instalação](docs/installation.md) · [Matriz de Suporte](docs/support-matrix.md) · [Arquitetura](docs/architecture.md) · [Roadmap Desktop](docs/desktop/phase4d-roadmap.md) · [Notas da Alpha.52](docs/releases/v0.6.1-alpha.52.md)

### Situação real dos componentes (Disponível × Experimental × Planejado)

| Situação | O que está incluído |
| --- | --- |
| **Disponível e Estável (`v0.6.0`)** | Biblioteca Python `ai-coordinator`, CLI, servidor MCP `ai-coordinator-mcp` e Agent Skill `SKILL.md` (Windows, macOS e Linux). |
| **Pre-release Alpha (`0.6.1-alpha.52`)** | AI Coordinator Desktop no Windows x64 (4D.1 Fundação, 4D.2 Conversas/Terminais ConPTY, 4D.3 Configurações/Cofre DPAPI/Acessibilidade e 4D.4 Monitoramento com proveniência). |
| **Empacotado / Documentado** | Plugin Claude Code, Plugin OpenCode V2 e receitas MCP+Skill para Codex e AGY (veja [Matriz de Suporte](docs/support-matrix.md)). |
| **Em Laboratório (Não distribuído)** | Patch de identidade de diretório do launcher Windows (`7fae072`, candidato futuro para patch do Core) e versões experimentais do `ai-memory`. |
| **Planejado** | **4D.5:** Mission Control (DAG mista) e Gateway MCP local · **4D.6:** Instalador unificado/modular, gerenciador de componentes no Desktop e suporte Linux/macOS · **4E:** Meta-Coordinator · **4F:** Ecossistema/acesso remoto opcional. |

---

## O que vem no repositório

- **AI Coordinator Desktop (`desktop/`)**: aplicativo gráfico em Tauri 2 (Rust + React/TypeScript + `xterm.js`) com runtime Python `3.11.9` isolado (`-I`), múltiplas abas e até 4 terminais ConPTY por aba, modal unificado de Configurações em 9 categorias com cofre DPAPI no Windows, modo de baixa estimulação sensorial e painel de Monitoramento da árvore de processos e telemetria local multi-perfil.
- **Biblioteca Python (`src/ai_coordinator/`)**: `Coordinator` para criar tarefas, ler contexto, gravar checkpoints cognitivos, consultar rotas e delegar execuções a workers isolados.
- **CLI**: bootstrap de projetos, retomada, checkpoints, consulta de disponibilidade, planejamento, execução e monitoramento de workers.
- **Human-AI Cognitive Workspace**: sessões supervisionadas (`HumanPrincipal`, `HumanSession`), controle de acesso por papéis (RBAC), checkpoints cognitivos com hashes SHA-256 de árvores de arquivos e integração opcional com `ai-memory`.
- **Sandboxes Nativas de SO**: isolamento físico de processos e sistema de arquivos em nível de kernel:
  - **Windows NT**: Win32 Job Objects suspensos (`CREATE_SUSPENDED`), cotas de recursos e perfis AppContainer com restrição NTFS.
  - **Linux**: Landlock LSM (Linux 5.13+) com `PR_SET_NO_NEW_PRIVS` e Trampoline seguro (`sys.executable -I -S -c`).
  - **macOS**: Apple Seatbelt Sandbox (`/usr/bin/sandbox-exec`) operando sob `(deny default)` e menor privilégio estrito.
- **Tool Broker e Capability Tokens**: governança com negação por padrão (`deny-by-default`), tokens criptográficos HMAC-SHA256, cotas atômicas por ferramenta, expiração temporal (TTL), aprovações humanas pendentes e mediação assíncrona via Mailbox de arquivos (`.broker`).
- **Trilha de Auditoria Imutável**: tabela SQLite append-only protegida por triggers contra `UPDATE`/`DELETE`, hash canônico de 15 campos e arquitetura de dupla ancoragem (tabela interna e arquivo externo desacoplado `auth/audit.anchor` com `fsync()`).
- **Servidor MCP**: tools para estado de tarefas, checkpoints e delegação explicitamente chamada pelo agente via stdio.
- **Agent Skill**: instruções para encontrar e retomar a tarefa adequada sem carregar históricos extensos.
- **Plugin Claude Code**: skill e configuração integrada para conectar o servidor MCP.
- **Plugin OpenCode V2**: tools para acessar o mesmo estado local pela CLI de forma determinística.
- **Governança de Despacho Multi-Modelo e Fallback Governado**: Verificação tripartite com assinatura Ed25519 (RFC 8032), Trust Store com rotação e expiração de chaves, aprovação operacional humana autenticada (HMAC-SHA256 vinculada a propósito), cotas atômicas e anti-replay persistente em SQLite WAL (`AtomicDispatchQuotaStore`), Ponto de Compromisso Irreversível (PIC `SEND_COMMITTED`) e Transactional Outbox durável sincronizado sob trava nativa do kernel do SO (`msvcrt` / `fcntl`) sem vulnerabilidade TOCTOU.
- **Avaliação de rotas**: executa casos fixos contra candidatos selecionados e registra saída, tempo e métricas para nota humana.

O estado e a configuração ficam no diretório de dados do usuário. Na primeira execução, o programa copia `routes.example.json` e o protocolo compartilhado para esse diretório, sem substituir arquivos existentes. Defina `AI_COORDINATOR_HOME` para usar outro local. Credenciais e dados de execução não fazem parte do pacote.

---

## Instalação do código-fonte

Clone o repositório e instale o pacote. Para habilitar o servidor MCP:

```console
python -m pip install ".[mcp]"
```

Com o pacote instalado, os comandos disponíveis são:

```console
ai-coordinator --help
ai-coordinator routes
ai-coordinator bootstrap --project .
```

Na primeira execução, revise `routes.json` no diretório de dados do usuário e configure os executáveis locais dos agentes que pretende usar. Os nomes e modelos do arquivo de exemplo são configurações iniciais; catálogo ou rota configurada não confirma saldo, cota ou acesso. Veja todas as instruções detalhadas em [`docs/installation.md`](docs/installation.md).

---

## Isolamento e Segurança

O AI Coordinator executa workers sob política de menor privilégio e isolamento de processo no sistema operacional:

- **Protocolo de Inicialização em Duas Fases**: O worker é criado suspenso no SO, seu PID e identidade estável são registrados na trilha de auditoria e a execução só é liberada (`resume()`) após o commit durável da auditoria. Falhas na auditoria abortam o processo antes da execução de qualquer comando (*fail-closed*).
- **Sandboxes de Filesystem**: Workers têm permissão de escrita restrita exclusivamente à sua própria pasta temporária de sandbox. Tentativas de ler chaves do coordenador (`auth/`), banco SQLite, credenciais de usuário (`.ssh`, `.aws`) ou tarefas irmãs são bloqueadas pelo kernel do SO.
- **Tool Broker e Mailbox**: Workers não possuem acesso a rede ou IPC direto. Ferramentas são solicitadas via arquivos JSON na pasta `.broker/requests` assinados com token HMAC e mediadas pelo supervisor durante o streaming. Tentativas de spoofing de PID ou adulteração de token encerram imediatamente a árvore de processos do worker.
- **Governança de Despacho e Custo Zero**: Despachos a modelos externos requerem autorização tripartite Ed25519 + HMAC de operador, validação de commit, cota atômica e avanço obrigatório ao PIC (`SEND_COMMITTED`) em SQLite WAL antes de qualquer transporte de rede, com auditoria durável em Transactional Outbox sob trava nativa de SO. Falhas indeterminadas (`UNKNOWN`) proíbem retransmissão para prevenir dupla cobrança.

Consulte [`docs/security.md`](docs/security.md), [`docs/security/governance_security_and_limitations.md`](docs/security/governance_security_and_limitations.md) e [`docs/architecture.md`](docs/architecture.md) para detalhes completos sobre limites, ameaças e garantias.

---

## Biblioteca Python

```python
from ai_coordinator import Coordinator

with Coordinator() as coordinator:
    task = coordinator.new_task(".", "Revisar o tratamento de erros")
    print(task["id"])
    print(task["ai_memory_workstream_id"])
    print(coordinator.active_tasks("."))
```

Use `Coordinator(root=".../dados")` para escolher um diretório de dados próprio. A API exportada está em `ai_coordinator.Coordinator` e `ai_coordinator.open_coordinator`.

Quando `AI_MEMORY_WORKSTREAM_ID` estiver definido, uma tarefa nova guarda esse UUID como referência ao workstream atual do ai-memory. Também é possível passar `ai_memory_workstream_id` a `new_task` ou `--ai-memory-workstream-id` à CLI. O coordenador armazena o identificador e integra o contexto de forma resiliente, tratando memórias como dados não-confiáveis.

---

## Avaliar candidatos de rota

`benchmark` executa cada caso contra os candidatos habilitados da categoria. Cada par caso/candidato agenda no máximo uma chamada; `--max-calls` é obrigatório porque a chamada pode consumir quota ou saldo. O comando grava as saídas e as métricas, mas não escolhe um vencedor automaticamente.

```console
ai-coordinator benchmark --project . --cases evals/cases.example.json --max-calls 12
```

Depois de revisar as respostas, preencha `quality_score` de 0 a 5 e `review_notes` no JSON de resultados. Gere o resumo:

```console
ai-coordinator benchmark-report --input caminho/benchmark.json
```

Veja [`evals/README.md`](evals/README.md) para os limites da comparação e os exemplos. As respostas não são pontuadas por outro modelo: o ranking usa notas humanas e mostra separadamente tempo, tokens e custo informado.

---

## Servidor MCP

Instale a dependência opcional e configure o host para iniciar `ai-coordinator-mcp` em transporte stdio:

```json
{
  "mcpServers": {
    "ai-coordinator": {
      "command": "ai-coordinator-mcp",
      "args": []
    }
  }
}
```

O servidor oferece tools para bootstrap do projeto, listar/criar tarefas, retomar, gravar checkpoint, consultar rotas e planejar workers. `delegate_to_worker` inicia uma chamada externa aos clientes locais configurados e pode consumir cota ou saldo; a resposta é uma proposta que precisa de revisão.

---

## Agent Skill

Use `skills/ai-coordinator/SKILL.md` em ferramentas compatíveis com Agent Skills. Ele orienta o agente a consultar o estado compartilhado sem carregar históricos completos.

---

## Claude Code

O plugin está em `plugins/claude-code`. Depois de instalar `ai-coordinator[mcp]`, adicione o marketplace e instale o plugin:

```console
claude plugin marketplace add MaxxArtes/ai-coordinator
claude plugin install ai-coordinator@ai-coordinator-marketplace
```

O marketplace contém um plugin com skill e configuração MCP. Se instalar apenas o ZIP de release, carregue-o com `claude --plugin-url <URL-do-asset>`.

---

## OpenCode V2

O plugin está em `plugins/opencode`. Instale o pacote Python e a dependência do plugin (via pasta clonada ou tarball `.tgz` da Release `v0.6.0`), depois adicione o caminho do plugin à configuração OpenCode:

```json
{
  "plugins": ["./plugins/opencode"]
}
```

O plugin invoca a CLI por argumentos separados, sem passar comandos por um shell. Seu agente OpenCode terá tools para bootstrap, contexto, criação e retomada de tarefas, checkpoints, plano de rota e delegação.

---

## Releases

Ao publicar uma tag estável `v*` (excluindo sufixos `-alpha*`, `-beta*` e `desktop-*`), o workflow gera e anexa ao GitHub Release:

- wheel e source distribution Python;
- ZIP da skill;
- ZIP do plugin Claude Code;
- tarball npm do plugin OpenCode.

Pre-releases do Desktop (`v*-alpha*`) publicam separadamente o instalador NSIS, sua assinatura Minisign `.sig`, o pacote portátil `.zip`, o `BUILD-MANIFEST.json` e os checksums SHA-256 sem disparar nova publicação do Core no PyPI ou npm. Cada formato inclui `LICENSE` e `NOTICE`. O servidor MCP acompanha o pacote Python como extra opcional. Pull requests e releases executam verificações automatizadas; veja a [matriz de suporte](docs/support-matrix.md) para saber o que é validado no CI e o que ainda exige conferência no cliente nativo.

---

## Licença

O projeto usa a [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). Ela permite uso e distribuição para fins não comerciais; uso comercial exige autorização separada do titular dos direitos. Essa restrição também pode se aplicar ao uso dentro de uma empresa. Consulte `LICENSE` e `NOTICE` antes de distribuir ou incorporar o pacote.
