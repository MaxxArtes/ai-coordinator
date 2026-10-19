# ai-coordinator

Estado de tarefas compartilhado entre agentes de programação, com retomada por checkpoints, rotas configuráveis, avaliação comparável de candidatos e chamadas delimitadas a workers locais. O núcleo funciona em Windows, macOS e Linux com Python 3.11 ou superior.

## O que vem no repositório

- **Biblioteca Python**: `Coordinator` para criar tarefas, ler contexto, gravar checkpoints, consultar rotas e delegar uma solicitação delimitada.
- **CLI**: bootstrap, retomada, checkpoint, consulta de disponibilidade, planejamento e execução de workers.
- **Servidor MCP**: tools para estado de tarefas e delegação explicitamente chamada pelo agente.
- **Agent Skill**: instruções para encontrar e retomar a tarefa adequada.
- **Plugin Claude Code**: skill e configuração para conectar o servidor MCP.
- **Plugin OpenCode V2**: tools para acessar o mesmo estado local pela CLI.
- **Avaliação de rotas**: executa casos fixos contra candidatos selecionados e registra saída, tempo e uso para nota humana.
- **Limites de ambiente**: workers recebem uma lista curta de variáveis do sistema; segredos só são passados quando configurados por cliente.

O estado e a configuração ficam no diretório de dados do usuário. Na primeira execução, o programa copia `routes.example.json` e o protocolo compartilhado para esse diretório, sem substituir arquivos existentes. Defina `AI_COORDINATOR_HOME` para usar outro local. Credenciais e dados de execução não fazem parte do pacote.

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

Na primeira execução, revise `routes.json` no diretório de dados do usuário e configure os executáveis locais dos agentes que pretende usar. Os nomes e modelos do arquivo de exemplo são configurações iniciais; catálogo ou rota configurada não confirma saldo, cota ou acesso.

As chamadas de worker começam em modo `allowlist`: recebem `PATH`, diretórios de configuração do usuário e outras variáveis básicas necessárias para localizar o cliente. Para autenticação por variável de ambiente, liste somente os nomes necessários em `worker_environment.allow_by_client` no `routes.json`; os valores não são gravados no arquivo. O modo `inherit` repassa todo o ambiente do processo e deve ser escolhido somente quando necessário.

O filtro não é uma sandbox do sistema operacional e não oculta os arquivos de autenticação em `HOME`/`USERPROFILE`. Consulte [`docs/security.md`](docs/security.md) antes de configurar credenciais de worker.

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

Quando `AI_MEMORY_WORKSTREAM_ID` estiver definido, uma tarefa nova guarda esse UUID como referência ao workstream atual do ai-memory. Também é possível passar `ai_memory_workstream_id` a `new_task` ou `--ai-memory-workstream-id` à CLI. O coordenador armazena o identificador, não o histórico da conversa.

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

## MCP

Instale a dependência opcional e configure o host para iniciar `ai-coordinator-mcp` em transporte stdio. Exemplo de configuração local para um host MCP:

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

## Agent Skill

Use `skills/ai-coordinator/SKILL.md` em ferramentas compatíveis com Agent Skills. Ele orienta o agente a consultar o estado compartilhado sem carregar históricos completos.

## Claude Code

O plugin está em `plugins/claude-code`. Depois de instalar `ai-coordinator[mcp]`, adicione o marketplace e instale o plugin:

```console
claude plugin marketplace add MaxxArtes/ai-coordinator
claude plugin install ai-coordinator@ai-coordinator-marketplace
```

O marketplace contém um plugin com skill e configuração MCP. Se instalar apenas o ZIP de release, carregue-o com `claude --plugin-url <URL-do-asset>`.

## OpenCode V2

O plugin está em `plugins/opencode`. Instale o pacote Python e a dependência do plugin, depois adicione o caminho do plugin à configuração OpenCode:

```json
{
  "plugins": ["./plugins/opencode"]
}
```

O plugin invoca a CLI por argumentos separados, sem passar comandos por um shell. Seu agente OpenCode terá tools para bootstrap, contexto, criação e retomada de tarefas, checkpoints, plano de rota e delegação.

## Releases

Ao publicar uma tag `v*`, o workflow gera e anexa ao GitHub Release:

- wheel e source distribution Python;
- ZIP da skill;
- ZIP do plugin Claude Code;
- tarball npm do plugin OpenCode.

Cada formato é versionado a partir da tag e inclui `LICENSE` e `NOTICE`. O servidor MCP acompanha o pacote Python como extra opcional. Pull requests e releases executam verificações automatizadas; veja a [matriz de suporte](docs/support-matrix.md) para saber o que é validado no CI e o que ainda exige conferência no cliente nativo.

## Licença

O projeto usa a [PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0). Ela permite uso e distribuição para fins não comerciais; uso comercial exige autorização separada do titular dos direitos. Essa restrição também pode se aplicar ao uso dentro de uma empresa. Consulte `LICENSE` e `NOTICE` antes de distribuir ou incorporar o pacote.
