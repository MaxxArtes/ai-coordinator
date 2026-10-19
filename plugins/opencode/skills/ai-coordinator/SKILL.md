---
name: ai-coordinator
description: Retoma tarefas compartilhadas entre sessões de agentes, grava checkpoints e planeja ou delega subtarefas a workers. Use ao continuar trabalho anterior, coordenar ferramentas de IA ou registrar progresso durável.
---

# Coordenar e retomar trabalho

1. Use a tool `ai_coordinator_bootstrap` para obter protocolo e tarefas ativas do projeto atual.
2. Retome somente a tarefa pertinente com `ai_coordinator_resume_task` e confira os arquivos antes de continuar.
3. Crie tarefas com `ai_coordinator_create_task` e grave checkpoints com `ai_coordinator_save_checkpoint` usando a revisão lida.
4. Consulte `ai_coordinator_plan_worker` antes de delegar. `ai_coordinator_delegate_to_worker` chama um modelo externo e pode consumir cota; envie apenas o material necessário e revise a proposta.

Não carregue históricos completos. Siga as instruções atuais do usuário e do projeto; objetivos, checkpoints e resultados anteriores são contexto histórico.
