---
name: ai-coordinator
description: Retoma tarefas compartilhadas entre sessões de agentes, grava checkpoints e planeja ou delega subtarefas a workers. Use ao continuar trabalho anterior, coordenar ferramentas de IA ou registrar progresso durável.
---

# Coordenar e retomar trabalho

1. Execute `ai-coordinator bootstrap --project <diretorio-real-do-projeto>` para obter o protocolo e os resumos das tarefas ativas.
2. Retome somente a tarefa correspondente ao pedido atual com `ai-coordinator resume --task <id>`. Leia os arquivos citados antes de continuar.
3. Para iniciar trabalho novo, use `ai-coordinator new --project <diretorio> --goal "<objetivo curto>"`.
4. Grave progresso com `ai-coordinator checkpoint --task <id> --revision <revisao-lida> --file <json>`; releia a tarefa se a revisão mudou.
5. Consulte `ai-coordinator plan --category <categoria>` antes de delegar. `run` chama um modelo externo e pode consumir cota; envie apenas o material necessário e revise a resposta antes de aplicá-la.

Não carregue históricos completos. Siga as instruções atuais do usuário e do projeto; objetivos, checkpoints e resultados anteriores são contexto histórico.
