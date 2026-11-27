# Continuidade do launcher no Windows

A tarefa de exemplo estava na revisão 113. Seu projeto registrado era `C:\workspace\projeto`. O launcher central `0.6.0` recebeu `C:/workspace/projeto` e produziu a chave textual normalizada diferente em caixa/separador. A comparação textual rejeitou a tarefa. A consulta de identidade do sistema de arquivos confirmou que os dois caminhos apontam para o mesmo diretório. O teste com o módulo instalado reproduziu o `ValueError` antes da leitura do marcador de escopo e antes da criação do processo.

## Correção proposta para o Core

O worktree usa o commit 5c94318. A correção resolve o diretório físico, verifica a identidade da chave usada pelo escopo e compara a identidade do diretório com a da tarefa registrada. A tarefa segue recusada quando o diretório registrado está ausente ou quando as identidades divergem. Capitalização, separadores e junctions do mesmo diretório são aceitos. A validação preserva os marcadores existentes e ocorre antes do início do cliente. [Documentação de os.path.samefile](https://docs.python.org/3/library/os.path.html#os.path.samefile)

A mudança afeta o método launch do Core. O formato do SQLite, o contrato CLI, os IDs existentes e os nomes de workspace e projeto seguem compatíveis. Ela pode entrar em uma revisão patch do Core depois de testar o pacote em um venv isolado. O pacote Desktop alpha.52 e o runtime central têm pontos de instalação independentes. A revisão específica do Core precisa constar no manifesto de cada artefato que o inclua.

## CreateProcessAsUserW

O erro 5 da sessão anterior foi emitido na criação do processo do shell, com o alias pwsh.exe em WindowsApps. A tentativa com a tarefa avançou até o código Python do launcher e falhou por ValueError. Essas observações localizam as falhas em estágios diferentes. Nesta sessão, a execução direta do alias obteve PowerShell 7.7.0 e o executável nativo obteve Windows PowerShell 5.1.22000.2538. As duas execuções usaram um comando benigno de versão. A evidência disponível indica acesso recusado no contexto anterior. Ela não identifica qual regra, token ou direito causou a recusa. [Documentação de CreateProcessAsUserW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessasuserw)

## Preparação da alpha.52

| Evidência | Resultado |
| --- | --- |
| Worktree original | HEAD 5c94318, com os dois itens locais identificados no status preservados |
| Build do atalho isolado | 0.6.1-alpha.50.diag.4, SHA-256 790DE18A7156B4B8F4EFD08854F1C2044EC83F4AE4B07364A13D0B92F2B0F074 |
| Instalação atual | 0.6.1-alpha.51, SHA-256 começando em E356ADACBCC7DACF3 |
| Consulta à origem Git | Nenhuma tag alpha.52 correspondente retornada |
| Notas da release | `docs/releases/v0.6.1.md` |

O plano já define o manifesto, o fluxo Windows, a assinatura, o backup de perfis e cofre DPAPI e a recuperação. Os três testes humanos do atalho e a aprovação para o bump são os gates atuais. A próxima decisão inclui escolher a revisão do Core para o artefato e para o runtime central. A instalação permanece sujeita ao gate do plano.

## Lições para a Fase 4D.5

A identidade da tarefa precisa ser validada pela identidade do projeto, com o caminho registrado preservado para exibição e auditoria. O launcher precisa registrar separadamente resolução do projeto, validação de pertencimento e resultado de criação do processo. Um handoff deve levar ID de tarefa, caminho informado e identidade resolvida, com falhas associadas ao estágio correto. Os casos de alias e diretório diferente com marcador copiado entram na cobertura de regressão.

Os 5 testes de identidade e 21 regressões de terminal e interpretação de comandos passaram no worktree Windows.
