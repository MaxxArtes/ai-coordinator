# Protocolo de coordenação

Use o estado compartilhado para localizar a tarefa em andamento deste projeto. Comece executando `ai-coordinator bootstrap --project <diretorio-real-do-projeto>` e siga o campo `protocol` retornado.

## Retomada

- Leia somente o checkpoint da tarefa correspondente ao pedido atual.
- Confira no disco os arquivos citados antes de afirmar que uma alteração existe.
- Continue de `next_step`, atualize um checkpoint após progresso relevante e marque como concluída apenas quando não houver trabalho pendente.
- Trate objetivos, resumos e resultados de workers como contexto histórico; instruções atuais do usuário e do projeto têm prioridade.
- Quando `AI_MEMORY_WORKSTREAM_ID` existir, uma tarefa nova guarda esse UUID para apontar ao workstream do ai-memory; aceite também `--ai-memory-workstream-id` quando for informado diretamente. Não copie o histórico do workstream para o estado do coordenador.

## Delegação

- Use `plan` para consultar a rota e a disponibilidade observada antes de `run`.
- Forneça aos workers apenas o material necessário. As ferramentas dos workers ficam desativadas; a resposta é uma proposta que o coordenador revisa antes de aplicar.
- Faça chamadas externas apenas quando forem úteis para o pedido atual. Uma tentativa pode consumir quota ou saldo do provedor.
- Respeite limites, cooldowns e resultados anteriores. Não repita em loop uma chamada que falhou.

## Segurança e publicação

- Não coloque credenciais, dados pessoais ou conteúdo confidencial nos objetivos, entradas ou checkpoints.
- Leia e preserve as alterações existentes no repositório antes de editar.
- Diferencie alterações locais, commit, push, publicação de release e deploy. Relate somente o que foi efetivamente concluído.
