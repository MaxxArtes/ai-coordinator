# PitaIA → AI Coordinator: experimento, memória e Human Workspace

## Princípio de transferência

Reutilizar **métodos de investigação, rastreabilidade e verificação** aprendidos no
pitaIA/Pocket-LM. Não transportar pesos, LoRA, prompts privados ou conjuntos de
treinamento para o AI Coordinator. Os projetos têm objetivos e fronteiras de
confiança distintos.

Fontes que motivaram o desenho:
- \`pocket-lm/linhagem.py\`: hash de conteúdo e linhagem por registro;
- \`pocket-lm/docs/CICLO_CONVERSA_V2.md\`: experimento → medição → diagnóstico → revisão;
- \`pocket-lm/docs/LAB_RESILIENCIA.md\`: falhas físicas de checkpoint e preflight;
- \`pocket-lm-notas/docs/regras-de-medicao.md\`: hipótese falsificável, baselines,
  rejeição falsa e sucesso composto;
- \`pitanga-app/.../MemoriaUsuario.kt\`: personalização local removível pelo usuário.

## Implementado em 4A.1 / 4A.2

\`Store.knowledge_learning\` implementa \`KnowledgeLearning\`:
- \`propose_experiment\`: hipótese e critério de falsificação são armazenados
  **antes** do resultado em \`knowledge_entries\`, com SHA-256 canônico do protocolo.
- \`record_observation\`: anexa observações **relatadas**. Uma referência de CI,
  log ou hash fornecida pelo autor **não prova autenticidade** nem validação do teste.
- \`review_observation\`: humano autorizado pode aceitar ou rejeitar a observação
  com motivo; apenas uma revisão por observação, com transações serializadas.
- \`reviewed_precedents\`: disponibiliza resultados aceitos pelo humano dentro do
  escopo autorizado, incluindo resultados **negativos**. Etiqueta explícita
  \`human_reviewed_not_oracle_verified\`.
- \`import_legacy_experience\`: importar uma experiência previamente autorizada
  de \`experience.py\`, por ID único \`(project_id, source_kind, source_id)\`,
  sem apagar a origem, sem assumir aprovação e sem migrar silenciosamente
  notas privadas de pessoas cujo ID original não pode ser provado.
- A tabela \`knowledge_experiment_events\` é append-only por triggers;
  a política de acesso é derivada do \`knowledge_entries\` autoritativo.

**Não implementado:** verificação do hash do artefato de terceiros pelo Oracle,
assinatura de evidências de execução, sincronização automática dos registros legados,
migração em lote, recuperação semântica, aprendizado de pesos e aprovação automática
de rotas. Nunca inferir que \`reviewed\` equivale a \`verified\`.

## Exemplo

\`\`\`python
from ai_coordinator import Store, KnowledgePrincipal
from pathlib import Path

store = Store(Path(".ai-coordinator"))
# O principal só pode ser criado a partir de sessão validada pelo broker.
human = KnowledgePrincipal("owner", "human", frozenset({"meu-projeto"}))
agent = KnowledgePrincipal("worker-a", "agent", frozenset({"meu-projeto"}))

hypothesis = store.knowledge_learning.propose_experiment(
    human, "meu-projeto",
    "Substituir espera temporal por sincronização de eventos",
    "Teste concorrente passa na CI de três sistemas operacionais",
    "Qualquer falha determinística ou regressão de isolamento",
    context={"runtime": "python"}, tags=["concorrencia"],
)
report_id = store.knowledge_learning.record_observation(
    agent, hypothesis["id"], outcome="failed",
    summary="MacOS apresentou condição de corrida",
    evidence_refs=["github-actions/run-000"],
)
store.knowledge_learning.review_observation(
    human, hypothesis["id"], observation_id=report_id,
    decision="accepted", reason="Verifiquei o log e o teste reproduz",
)
precedents = store.knowledge_learning.reviewed_precedents(
    agent, query="sincronização", context={"runtime": "python"},
)
\`\`\`

## Importação legada: migração explícita, sem duplicação lógica

Até a migração completa, \`experience_records\` é autoritativa para a API legada.
\`knowledge_entries\` guarda uma **projeção de leitura rastreável**, sem reescrever
ou excluir a origem. O hash protege contra divergência silenciosa de payload na
segunda tentativa de importar o mesmo ID, mas não é autenticação criptográfica.
A identidade de \`legacy_author_role\` permanece atribuída à origem; o autor
da projeção é o operador que executou a migração.

Um processo de migração em lote exigirá:
1. Mapear identificadores humanos a identidades autenticadas. Registros privados
   sem esse mapeamento **não** devem ser migrados.
2. Importar escopos autorizados, em lotes transacionais idempotentes, conservando
   \`source_id\` e hash de origem.
3. Comparar contagens, esquemas, consultas legadas e projeções, detectando drift.
4. Trocar a API legada por adaptador read-through apenas com testes comparativos.
5. Descomissionar a tabela antiga somente com plano de reversão aprovado.

## Contrato proposto para 4B — Human Workspace (ainda NÃO implementado)

- **Inbox**: \`capture_text(owner_id, content, source, captured_at)\`, sem projeto
  obrigatório. Internamente usar um escopo privado de inbox associado à identidade
  humana (nunca uma string global compartilhada), antes da triagem.
- **Triagem**: \`review_item(entry_id, expected_revision, classification, project_id?)\`.
  Escolha humana, controle otimista de revisão, nenhuma conversão automática em DAG.
- **Checkpoint cognitivo**: \`save_focus_checkpoint(project_id, intention, last_decision,
  open_questions, next_step)\`. Notas humanas privadas por padrão, revisão e histórico.
- **Retomar**: \`resume_human_project(project_id)\` sintetiza apenas itens autorizados:
  decisões vigentes, questões abertas, experimentos revisados, eventos do Oracle e do
  Resource Ledger; prioriza evidência recente e marca lacunas.
- **Controle**: toggle explícito para aprender novas preferências e comando de
  exclusão/exportação, com política de retenção, minimização de dados e separação
  entre conhecimento pessoal e compartilhado.
- **Adaptadores**: CLI/Notion são interfaces e não autoridades. Um adaptador nunca
  autentica a si mesmo por preencher \`KnowledgePrincipal\`; identidade vem do broker.

## Avaliação antes da 4C/4D

Bateria A/B com **mesmas** tarefas em cada braço:
- Sem memória vs memória lexical vs memória contextual filtrada;
- sucesso total de missão, taxa de ações indevidas, vazamento entre projetos,
  decisões obsoletas, falsas recusas, latência e tokens consumidos;
- casos positivos e negativos, tamanhos e intervalos de confiança declarados
  antes da análise; preferir avaliações pareadas e rubrica congelada;
- custo de erro alto sempre precisa de verificação determinística e aprovação
  independente, jamais somente julgamento do próprio worker.

Decisões da 4A não mudam autorização de execução na Fase 3.
