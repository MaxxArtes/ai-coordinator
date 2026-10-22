# PitaIA → AI Coordinator: experimento, memória e Human Workspace

## Princípio de transferência

Reutilizar **métodos de investigação, rastreabilidade e verificação** aprendidos no
pitaIA/Pocket-LM. Não transportar pesos, LoRA, prompts privados ou conjuntos de
treinamento para o AI Coordinator. Os projetos têm objetivos e fronteiras de
confiança distintos.

Fontes que motivaram o desenho:
- `pocket-lm/linhagem.py`: hash de conteúdo e linhagem por registro;
- `pocket-lm/docs/CICLO_CONVERSA_V2.md`: experimento → medição → diagnóstico → revisão;
- `pocket-lm/docs/LAB_RESILIENCIA.md`: falhas físicas de checkpoint e preflight;
- `pocket-lm-notas/docs/regras-de-medicao.md`: hipótese falsificável, baselines,
  rejeição falsa e sucesso composto;
- `pitanga-app/.../MemoriaUsuario.kt`: personalização local removível pelo usuário.

## Implementado em 4A.1 / 4A.2 / 4A.3

`Store.knowledge_core` e `Store.knowledge_learning` implementam o núcleo de conhecimento:
- **Três dimensões independentes**:
  - `state` (Ciclo de vida): `captured`, `proposed`, `accepted`, `rejected`, `superseded`, `archived`.
  - `nature` (Natureza epistemológica): `speculative_draft`, `observation`, `validated_finding`, `decision_record`. Distingue decisões normativas de projeto de descobertas factuais universais.
  - `evidence_level` (Grau de evidência): `unverified`, `human_reviewed`, `oracle_verified`.
- **Proteção do Oráculo**:
  - `oracle_verified` e `validated_finding` só podem ser atribuídos por `principal.role == "supervisor"` através de `verify_with_oracle` com token autenticado e evidências auditáveis.
  - Workers e humanos não conseguem forjar `oracle_verified` via JSON ou transições diretas.
- **Protocolo de experimentos falsificáveis**:
  - `propose_experiment`: hipótese e critério de falsificação são armazenados **antes** do resultado em `knowledge_entries`, com SHA-256 canônico do protocolo e `evidence_level="unverified"`.
- **Observações e precedentes negativos**:
  - `record_observation`: registra observações relatadas sem promoção automática.
  - `review_observation`: revisão humana serializada; resultados negativos (`outcome="failed"`) são preservados como precedentes fundamentais para evitar repetir erros passados.
- **Migração retrocompatível do legado**:
  - Esquema SQLite com migração automática via `_init_schema` adicionando `nature` e `evidence_level` caso ausentes.
  - `import_legacy_experience`: importa dados históricos de `experience.py` com `evidence_level="unverified"` e bloqueia notas privadas legadas sem autor único autenticado (*fail-closed*).
- A tabela `knowledge_experiment_events` é append-only por triggers; a política de acesso é derivada do `knowledge_entries` autoritativo.

**Não implementado:** verificação do hash do artefato de terceiros pelo Oracle,
assinatura de evidências de execução, sincronização automática dos registros legados,
migração em lote, recuperação semântica, aprendizado de pesos e aprovação automática
de rotas. Nunca inferir que `reviewed` equivale a `verified`.

## Exemplo

```python
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
```

## Importação legada: migração explícita, sem duplicação lógica

Até a migração completa, `experience_records` é autoritativa para a API legada.
`knowledge_entries` guarda uma **projeção de leitura rastreável**, sem reescrever
ou excluir a origem. O hash protege contra divergência silenciosa de payload na
segunda tentativa de importar o mesmo ID, mas não é autenticação criptográfica.
A identidade de `legacy_author_role` permanece atribuída à origem; o autor
da projeção é o operador que executou a migração.

Um processo de migração em lote exigirá:
1. Mapear identificadores humanos a identidades autenticadas. Registros privados
   sem esse mapeamento **não** devem ser migrados.
2. Importar escopos autorizados, em lotes transacionais idempotentes, conservando
   `source_id` e hash de origem.
3. Comparar contagens, esquemas, consultas legadas e projeções, detectando drift.
4. Trocar a API legada por adaptador read-through apenas com testes comparativos.
5. Descomissionar a tabela antiga somente com plano de reversão aprovado.

## Contrato proposto para 4B — Human Workspace (a implementar na Fase 4B)

- **Tabela dedicada `human_inbox`**:
  - `id TEXT PRIMARY KEY`, `owner_id TEXT NOT NULL`, `project_id TEXT NULLABLE`,
    `content TEXT NOT NULL`, `status TEXT NOT NULL DEFAULT 'unread'`,
    `captured_at TEXT NOT NULL`, `updated_at TEXT NOT NULL`.
  - Isola anotações pessoais da tabela `knowledge_entries` (que possui `project_id TEXT NOT NULL`),
    permitindo captura sem atrito sem exigir projetos globais genéricos ou gambiarras de esquema.
  - O `owner_id` deve ser autenticado por broker/supervisor confiável; workers subordinados não podem
    forjar identidades humanas.
- **Triagem**: `review_item(entry_id, expected_revision, classification, project_id?)`.
  Escolha humana, controle otimista de revisão, nenhuma conversão automática em nós da DAG.
- **Checkpoint cognitivo**: `save_focus_checkpoint(project_id, intention, last_decision,
  open_questions, next_step)`. Preserva o foco mental antes de interrupções ou trocas de contexto.
- **Retomar**: `resume_human_project(project_id)` sintetiza itens autorizados: decisões vigentes,
  questões em aberto, experimentos revisados, eventos do Oracle e do Resource Ledger.
- **Insight Engine híbrido e econômico**:
  - Proatividade por eventos contextuais locais (retomada, término de missão, bloqueio) com detecção determinística no SQLite sem consumo contínuo de tokens.
  - Ideação e reflexão profunda com modelos externos apenas sob demanda ou autorização explícita com orçamento e cotas.
  - Três regras inegociáveis: repetição de hipótese por múltiplos modelos não é validação; semântica entre projetos não quebra privacidade; autonomia determinística total mesmo offline.
- **Adaptadores**: CLI e visualizadores externos (ex: Notion na 4F) são interfaces secundárias de leitura e ingestão, sem autoridade operacional sobre a fonte única da verdade (SQLite).

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
