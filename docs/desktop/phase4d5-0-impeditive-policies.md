# Fase 4D.5.0 — Políticas Impeditivas e Progressão Incremental Governada

> [!NOTE]
> **Status:** especificação normativa de arquitetura e segurança da Fase `4D.5.0`. Este documento não implementa código das subetapas `4D.5.1`–`4D.5.8`.
> **Documentos relacionados:** [`phase4d5-0-architecture.md`](phase4d5-0-architecture.md), [`phase4d-roadmap.md`](phase4d-roadmap.md) (Apêndices H–L) e [`phase4d5-0-case-study-tri-front-4494783d8b3248ce.md`](../reports/phase4d5-0-case-study-tri-front-4494783d8b3248ce.md).
> **Referência de código:** `main` `87509121170f548be58f20f90abd3397b9f94ed5` (Core `v0.6.0`, Desktop `0.6.1-alpha.52`) e achados experimentais da missão `4494783d8b3248ce`.
> **Data:** 26/11/2026.

---

## 1. Propósito e regra *fail-closed*

A visão completa do AI Coordinator — Mission Control com DAG, unidades lógicas heterogêneas (`P/E/D/S/H/V`), execução paralela fora de ordem, Gateway local e aprendizado contínuo na `4E` — permanece integralmente preservada. Contudo, a auditoria do Core `v0.6.0` e a revisão independente (`ae9ec9679ffa49cd`) demonstraram que a ativação imediata de despacho autônomo ou escrita governada em arquivos antes do fechamento dos pré-requisitos de isolamento, autoridade humana e arbitragem de recursos violaria os invariantes `I1`–`I12`.

Este documento define normativamente:

1. A **progressão incremental governada em 6 estágios e 5 níveis** de autonomia (seção 2);
2. A **Política `B3-03`** — Rede em AppContainer (seção 3);
3. A **Política `B3-04`** — Autenticação e autoridade humana na camada `Store` (seção 4);
4. A **Política `B3-08`** — Arbitragem de CPU, RAM e I/O com folga garantida e filas justas (seção 5);
5. As **políticas transversais** de fencing monotônico, reconciliação pós-spawn, idempotência estrita e recuperação após crash (seção 6).

**Regra normativa geral (*fail-closed*):** nenhum estágio ou capacidade pode contornar os pré-requisitos do estágio anterior. Enquanto qualquer item impeditivo aplicável (`B3-01`, `B3-02`, `B3-03`, `B3-04`, `B3-08`, `B3-09`, `P8`, `P9`, `P10`) permanecer aberto, o sistema permanece travado no estágio seguro correspondente e recusa qualquer promoção silenciosa para execução mutável com arquivos.

---

## 2. Progressão incremental governada (6 estágios / 5 níveis)

A evolução da `4D.5` separa **capacidade de leitura/planejamento** de **capacidade de despacho e mutação**, mapeando **6 estágios de entrega** sobre **5 níveis progressivos de autoridade operacional**:

| Estágio | Nível de autoridade | Escopo funcional | Subetapa / Gate | Pré-requisitos obrigatórios | Capacidade explicitamente proibida |
|---|---|---|---|---|---|
| **1. Mission Control + DAG consultivo** | **Nível 1** (Observação e aconselhamento) | Entidade de missão (`missão → tarefas → nós`), cálculo consultivo de `get_ready_nodes`, caminho crítico consultivo, vista **Missões** somente leitura no Desktop e métodos IPC `mission.*` de leitura. | `4D.5.1` | `P8` (`--root` isolado e compatibilidade `v0.6.0`), aprovação humana da `4D.5.0`. | Despacho automático, alteração de DAG sem aprovação, escrita via IPC/vista Missões. |
| **2. Gateway local somente leitura (`G0`)** | **Nível 1 → 2** (Exposição controlada somente leitura) | Descoberta (`capabilities`, `version`, `contracts`) e leitura de projetos/tarefas/DAG sobre MCP local (`stdio`), com suíte dedicada `P9` validando as 8 tools MCP (contratos, erros e recusa de escrita fora de escopo). | `4D.5.1` (`G0`) | Estágio 1 + suíte dedicada `P9` 100% aprovada. | Mutações não cobertas pelo contrato da tarefa, delegação ilimitada, despacho de executores com escrita. |
| **3. Workers textuais governados** | **Nível 2** (Execução textual isolada sem escrita) | Seleção por `UnitDescriptor` (`P/E/D/S/H/V`), proveniência obrigatória (`measured`/`estimated`/`unknown`), recusa de cota `stale`/`estimated` como disponível (`I7`) e failover multimodelo em workers estritamente textuais (`≤ 8000` chars entrada, `≤ 1600` saída, sem escrita em disco). | `4D.5.2` | Estágios 1–2 + `B3-02`/`D1` corrigido (`v0.6.1`), `B3-07` tratado (candidato sem `worker_headless` não consome tentativa) e `B3-15` (identidade física de projeto). | Execução direta de ferramentas com escrita de arquivos; uso de cota sem proveniência válida. |
| **4. Execução com arquivos após fechamento dos pré-requisitos** | **Nível 3** (Mutação governada e rastreada) | Despacho de executores governados com acesso restrito a `allowed_paths` via `Gateway` `G1`/`G2`/`G3`, `GatewayPrincipal` HMAC sem vínculo a PID, aprovação humana vinculada a hash na camada `Store`, `idempotency_key` e auditoria em cadeia de hash. | `4D.5.5` (`G1`), `4D.5.6` (`G2`), `4D.5.8` (`G3`) | Estágios 1–3 + **fechamento integral** de `B3-01` (`v0.6.1`), `B3-03` (rede AppContainer), `B3-04` (autenticação humana no `Store`), `B3-08` (arbitragem CPU/RAM/I/O), `B3-09` (perfis AppContainer reutilizáveis), fencing até o efeito colateral e aprovação explícita do operador. | Despacho com escrita sem `fencing_token`, sem `allowed_paths` exclusivos ou com aprovação não vinculada ao hash do conteúdo. |
| **5. Escalonamento adaptativo e paralelismo governado** | **Nível 4** (Concorrência arbitrada e recuperação resiliente) | Caminho crítico dinâmico, `ResourceLedger` com `exclusive_window`, `agent_slot` e `quota_budget`, despacho fora de ordem seguro, pipeline sobrepondo execução e verificação (`V-Core`), subestados (`productive`, `waiting_declared`, `waiting_input` → `NEEDS_INPUT`, `idle`, `stalled`), intervenção seletiva e handoff preventivo como nó do DAG. | `4D.5.3`, `4D.5.4`, `4D.5.6`, `4D.5.7` | Estágio 4 (para paralelismo mutável) + `P10` (heartbeat de progresso), `B3-05`/`B3-06` (máquina de estados), `I9` verificado sob contenção/falha do AI Memory e testes de fronteira aprovados. | Preempção destrutiva de runs em curso, execução paralela com `allowed_paths` sobrepostos, cancelamento automático fora do contrato não interativo. |
| **6. Evidências para Meta-Coordinator (`4E`)** | **Nível 5** (Observabilidade fechada para aprendizado offline) | Coleta obrigatória de `ExecutionDecisionRecord` (separando `decided_by`: `coordinator | harness | human`), registros no `ExperienceStore`, métricas com proveniência, benchmarks contra referência sequencial (`J.2`) e consolidação de evidências para a `4E`. | `4D.5.8` → insumo `4E` | Estágios 1–5 + Verify 9/9, `cargo test --locked`, homologação nativa Windows e revisão de segurança independente sem achados altos. | Alteração automática de políticas, rotas ou limites pelo `InsightEngine` na `4D.5` (`proactive` permanece desabilitado — invariante `I12`). |

### 2.1 Escopo reduzido seguro em caso de bloqueio parcial

Conforme registrado na revisão independente (`ae9ec9679ffa49cd`), se qualquer impeditivo de **Estágio 4** (`B3-03`, `B3-04`, `B3-08` ou `B3-09`) não for aprovado ou concluído na janela da `4D.5`, os **Estágios 1 a 3** (Mission Control + DAG consultivo + `G0` somente leitura + workers textuais governados com `B3-02` corrigido) podem ser entregues de forma independente sem rebaixar garantias de segurança. Os **Estágios 4 a 6** ficam bloqueados automaticamente até o fechamento integral dos requisitos.

---

## 3. Política `B3-03`: Rede AppContainer

### 3.1 Contexto experimental

Na `main` `8750912` (`fs_sandbox.py:326`), o `WindowsAppContainerController` cria o perfil AppContainer com `CapabilityCount=0`, bloqueando completamente a pilha de rede de saída (verificado pela Frente B: `curl` dentro da AppContainer retornou `HTTP_000` contra `HTTP 200` fora). Embora workers estritamente textuais de provedores externos ou executores governados que precisem consultar metadados de API ou MCP remoto específico possam falhar por timeout de rede, liberar rede irrestrita destruiria a contenção de exfiltração e movimento lateral.

### 3.2 Regras normativas de `B3-03`

1. **Negação por padrão (`deny-by-default`):** todo perfil AppContainer (e equivalentes Landlock/Seatbelt) nasce sem capabilities de rede (`CapabilityCount=0`, equivalente `network: none`). Nenhuma ferramenta, worker ou executor recebe acesso de rede implicitamente.
2. **Autorização explícita por cliente e missão:** acesso de rede só pode ser concedido quando declarado no contrato do executor (`UnitDescriptor` / política do cliente em `routes.json` e `FlightPlan`), com escopo mínimo, justificativa operacional e aprovação explícita quando ampliar a política padrão do projeto.
3. **Mediação preferencial por proxy loopback autenticado por run:**
   - Sempre que o cliente/ferramenta suportar proxy HTTP/HTTPS ou endpoint configurável, o acesso **deve** passar por um **proxy loopback local mediado pelo Core**, autenticado por credencial efêmera vinculada ao `run_id` e revogada ao fim da tentativa.
   - O proxy impõe *allowlist* estrita de domínios/endpoints, métodos permitidos, limite de payload/taxa e registro completo na auditoria (`worker_audit`), mantendo segredos e chaves reais fora do processo filho sempre que aplicável.
   - A concessão de capability nativa Windows `internetClient` é exceção estrita para clientes cujo binário não opere via proxy loopback, devendo ser restrita a perfil dedicado por cliente (nunca compartilhada com executores genéricos de código) e acompanhada de regras de firewall/endpoint quando disponível.
4. **Proibição de rede irrestrita como atalho:** é proibido conceder `internetClient` amplo de forma global, conceder `privateNetworkClientServer` (acesso a rede local/LAN) por padrão, ou desativar `security.enforce_filesystem_sandbox` para contornar falhas de rede. Em consonância com `B3-10`, falhas de inicialização de perfil mantêm comportamento *fail-closed*.

---

## 4. Política `B3-04`: Autenticação Humana na Camada `Store`

### 4.1 Lacuna identificada na `main` `8750912`

Na `main` `8750912`, `cli.py:973-974` chama `store.resolve_trajectory_proposal(args.proposal, args.resolve)` sem verificar identidade do chamador, e `Store.activate_trajectory_proposal` (`storage.py:2056`) verifica apenas `proposal.status == "approved"` (exigindo `admin_token` somente quando parâmetros tentam desativar `require_approval`, `enforce_invariants` ou `enforce_clean_resources`). Como resultado, qualquer processo local com acesso ao CLI, MCP ou objeto `Store` pode aprovar a própria proposta de DAG e ativá-la sem intervenção humana verificável.

### 4.2 Regras normativas de `B3-04`

1. **Identidade e autoridade verificáveis:** toda operação sensível que exige autoridade humana — aprovação/rejeição de `TrajectoryProposal` (`dag-proposal`), resolução de nó `H-Core` / intervenção seletiva, concessão/revogação de `GatewayPrincipal`, autorização de escrita fora do escopo padrão e despacho `G3` — deve apresentar prova criptográfica emitida por uma sessão humana autenticada (`human_workspace.py`, token de sessão humana + assinatura HMAC/Ed25519 conforme `manifest_auth.py`). Flags autodeclaradas (`caller_role="human"`, `--approved` sem prova) são proibidas e ignoradas.
2. **Vínculo estrito de conteúdo (*content-bound approval*):** o envelope de aprovação (`HumanOperationalApproval` estendido) deve vincular atomicamente:
   - `operation`: identificador canônico da ação (ex.: `trajectory.approve`, `dag.node.resolve`, `gateway.dispatch.g3`);
   - `mission_id`, `task_id`, `node_id` (quando aplicável);
   - `project_id`: identidade física verificada do projeto (`.ai-memory.toml` + `samefile` com `st_ino != 0`, `B3-15`);
   - `purpose`: finalidade declarada pelo operador;
   - `content_hash`: `SHA-256` canônico (JSON ordenado sem ambiguidade) do objeto exato aprovado (nós e arestas da proposta de DAG, `allowed_paths`, comando/executor e parâmetros do despacho). Qualquer alteração pós-emissão invalida a aprovação.
3. **Expiração, revogação e anti-replay:**
   - Toda aprovação carrega `issued_at`, `expires_at` curto e `nonce` único;
   - O `nonce` é consumido atomicamente na mesma transação SQLite da operação aprovada; reenvio do mesmo envelope após consumo ou revogação gera falha de replay (`ReplayError` / recusa auditada);
   - A revogação explícita pelo operador invalida imediatamente aprovações pendentes e tokens derivados.
4. **Aplicação obrigatória na camada `Store` (impossível contornar):**
   - A validação do envelope e do `content_hash` reside **dentro dos métodos mutáveis do `Store`** (`storage.py`, ex.: `resolve_trajectory_proposal`, `activate_trajectory_proposal`, `resolve_human_intervention`, `issue_gateway_principal`), e não apenas na interface CLI, MCP, IPC ou Gateway;
   - CLI, MCP, IPC do Desktop e Gateway atuam apenas como transportadores da prova fornecida pelo operador autenticado; nenhum caminho direto via `Store` em Python pode marcar uma proposta como `approved` ou executar operação sensível sem prova válida (exceto testes unitários que explicitamente utilizem a infraestrutura de chave/sessão humana de teste, nunca com flag booleana de bypass em produção).

---

## 5. Política `B3-08`: Arbitragem de CPU, RAM e I/O

### 5.1 Motivação empírica

Na missão `4494783d8b3248ce` (máquina Windows de 4 núcleos lógicos com disco HDD `D:` compartilhado), a ausência de arbitragem nativa no Core (`ResourceLimits` em `process.py:199` sem limites de taxa de CPU, afinidade, prioridade ou I/O) exigiu contenção manual externa (`0xC` + `BelowNormal`, `cargo -j 2`, `pytest` sem `xdist`) e o script improvisado `heavy_lock.py`. Mesmo assim, ocorreu *starvation* severa (testes leves levaram `110 s` em vez de `20 s` — 5× mais — e um lote concorrente invalidou porque o launcher demorou `> 15 s` para iniciar sob `BelowNormal`), além de reaquisição imediata no mesmo segundo (`12:28:45`) antes da criação da fila FIFO.

### 5.2 Regras normativas de `B3-08`

1. **Folga garantida para o usuário e o SO:**
   - O escalonador e os limites de processo devem reservar uma fração explícita de CPU e memória para o sistema operacional e para a sessão interativa do operador (ex.: em host de 4 núcleos lógicos, teto máximo agregado de trabalhadores pesados equivalente a `≤ 50%` da CPU ou máscara dedicada configurável como `0xC`, prioridade padrão `BelowNormal` para cargas pesadas de fundo e reserva mínima de RAM livre configurável).
2. **Limites configuráveis por unidade e executor:**
   - Estender `ResourceLimits` (`process.py:199`) e `UnitDescriptor` para declarar e aplicar no Job Object (Windows) / cgroups ou `nice`/`ionice`/`RLIMIT` (POSIX):
     - taxa máxima de CPU (`JOBOBJECT_CPU_RATE_CONTROL_INFORMATION` no Windows);
     - máscara de afinidade opcional por classe de carga;
     - classe de prioridade de processo e prioridade de I/O (`PROCESS_MODE_BACKGROUND_BEGIN` ou prioridade baixa de I/O quando aplicável, observando o limite de memória associada);
     - teto de RAM/commit e número máximo de processos filhos (`ActiveProcessLimit`), mantendo `KILL_ON_JOB_CLOSE`.
3. **Prioridade adaptativa com precedência de segurança:**
   - As filas de despacho e aquisição de recursos obedecem a níveis de prioridade explícitos:
     - **Prioridade `0` (Máxima — Segurança e Contenção):** correções críticas de segurança, mitigação de vulnerabilidades, validação de integridade e pouso seguro;
     - **Prioridade `1` (Alta — Homologação e Caminho Crítico):** preparação de release, verificação `V-Core` e nós no caminho crítico sem risco pendente;
     - **Prioridade `2` (Normal/Fundo — Diagnóstico, Benchmarks e Lotes):** benchmarks, matrizes comparativas e tarefas fora do caminho crítico.
   - **Anti-starvation:** espera prolongada além de janela configurada promove progressivamente o item em espera dentro dos limites de folga, sem violar a precedência de segurança imediata nem as restrições de exclusividade.
4. **Controle de concorrência:** limites simultâneos configuráveis por `agent_slot` (cliente/perfil), por `unit_class` e por teto global de trabalhadores pesados ativos no host.
5. **Monitoramento de pressão com *backpressure*:**
   - Antes de despachar novos nós e periodicamente durante a supervisão, o Core consulta telemetria do host (CPU total e carga externa não gerenciada — análogo ao `v_lista.py` observado na missão anterior — RAM livre e fila/latência de disco no volume alvo);
   - Quando qualquer indicador ultrapassa o limiar configurado, o escalonador aplica ***backpressure***: retém novos despachos pesados (`defer` registrado), registra a proveniência `measured` da pressão e só retoma após retorno abaixo da histerese, sem cancelar trabalho saudável já em andamento.
6. **Filas FIFO priorizadas e janelas exclusivas:**
   - Atividades incompatíveis em CPU, RAM ou disco (`pytest` completo, testes de corrida sensíveis a carga, `cargo build --release`, benchmarks de disco, `npm run build`/`test`) exigem aquisição do recurso `exclusive_window` no `ResourceLedger`;
   - A fila é ordenada deterministicamente por `(priority, first_seen_seq)` com TTL de espera renovável; quem acabou de liberar uma janela não pode readquiri-la na frente de outro solicitante ativo na fila (eliminando a reaquisição no mesmo segundo observada às `12:28:45`).

---

## 6. Políticas transversais de execução e recuperação

As quatro políticas abaixo aplicam-se de ponta a ponta a todo despacho governado, reserva de recurso e operação mutável do Gateway:

### 6.1 Token de fencing monotônico até o efeito colateral

- Toda concessão de `exclusive_window`, lease de nó ou reserva de execução emite um **`fencing_token` inteiro estritamente monotônico** (persistido em sequência atômica no `Store` / `ResourceLedger`, generalizando `locks/epoch.json` do `heavy_lock_v2.py`).
- O token acompanha toda a cadeia: reserva → despacho → contexto do executor / capability do Tool Broker → chamada de efeito colateral (`ResourceLedger.confirm`, escrita de mailbox/sandbox, commit de resultado, `release` do recurso e checkpoint de nó).
- Qualquer camada de efeito compara o `fencing_token` apresentado contra a época vigente do recurso/nó; se for menor ou revogado, **recusa o efeito colateral** e registra violação de época na auditoria. Isso elimina a janela em que um titular cujo lease expirou ainda consiga gravar efeitos ou liberar o lock de um sucessor legítimo.

### 6.2 Reconciliação pós-spawn (`PID + create_time_ns` + revogação antes de retry)

- Imediatamente após o `spawn`, o Core persiste atomicamente `(holder_pid, process_create_time_ns)` (análogo a `holder_pid` + `holder_create_time_100ns` via `GetProcessTimes` no `heavy_lock_v2.py` e `process_create_time` no `storage.py`).
- **Vivacidade antes de takeover:** a expiração de relógio (`max_minutes` / lease TTL) **nunca** autoriza takeover automático por si só. Antes de declarar um lease abandonado, o Core verifica se o processo real (`PID` + `create_time_ns`) continua ativo no SO; se estiver vivo, o takeover é recusado (equivalente ao código `4` em `heavy_lock_v2.py`) e convertido em inspeção/escalonamento de estagnação (`4D.5.7`).
- **Revogação e reconciliação antes de retry:** nenhum retry pós-spawn (`begin_worker_attempt` ou reenvio de nó) pode ocorrer enquanto:
  1. o processo anterior não estiver comprovadamente encerrado (`safe_terminate_tree` + verificação de saída) ou isolado;
  2. o `fencing_token` e os tokens de capacidade/Gateway da tentativa anterior não tiverem sido explicitamente revogados; e
  3. o `ResourceLedger` não tiver reconciliado intenções pendentes da tentativa anterior (`record_intent` sem `confirm`), aplicando compensação ou reaproveitamento idempotente quando aplicável.

### 6.3 Idempotência estrita

- Toda operação mutável no Gateway e no ciclo de despacho exige `idempotency_key` determinística (ex.: derivada de `(mission_id, node_id, attempt, operation_digest)`).
- Repetições com a mesma `idempotency_key` e mesmo hash de payload retornam o resultado original consolidado sem reexecutar o efeito.
- Repetições com a mesma `idempotency_key` e payload divergente lançam `IdempotencyError` (`resource_ledger.py:39`) e são auditadas como anomalia.

### 6.4 Recuperação segura após crash

- Todos os arquivos de estado e espelhos locais (`state.json`, metadados de lock/epoch, snapshots auxiliares) devem usar **escrita em arquivo temporário + `fsync` + `os.replace` atômico** (endereçando `B3-13` e o risco de arquivos zerados ou truncados).
- Checkpoints críticos de missão/nó utilizam transação SQLite durável (`synchronous=FULL` configurável para commits críticos) com revisão otimista (`storage.py:1194`).
- Na retomada pós-crash:
  1. Reconstrói-se `L1`/`L2` a partir de `coordination.sqlite` sem depender da disponibilidade do AI Memory (`I9`);
  2. Reconciliam-se runs `running`/`starting` por `(PID, create_time_ns)` e tokens pendentes;
  3. Nós `completed` e verificados por `V-Core` nunca são reexecutados (`I8`);
  4. Intenções parciais no `ResourceLedger` são reconciliadas ou compensadas antes de recalcular `get_ready_nodes`.

---

## 7. Rastreabilidade para implementação (`4D.5.1`–`4D.5.8`)

| Política / Mecanismo | Subetapa de destino | Critério de aceite vinculado (`phase4d5-0-architecture.md` §5.6) |
|---|---|---|
| Progressão em 6 estágios / 5 níveis | `4D.5.1` → `4D.5.8` | `4D.5.1(d)`, `4D.5.5(a)`, `4D.5.8` |
| `B3-03` (Rede AppContainer) | `4D.5.3` / `4D.5.5` / `G3` | Testes de bloqueio padrão e proxy loopback autenticado por run |
| `B3-04` (Autenticação humana no `Store`) | `4D.5.5` (`G1`) | `4D.5.5(a)` — recusa 100% de contorno via CLI/MCP/Store e aprovação alterada |
| `B3-08` (Arbitragem CPU/RAM/I/O) | `4D.5.3` | `4D.5.3(c)` — folga garantida, fila FIFO priorizada e backpressure sob pressão |
| Fencing monotônico + reconciliação pós-spawn + idempotência + crash recovery | `4D.5.3`, `4D.5.5`, `4D.5.6` | `4D.5.3(a)/(b)`, `4D.5.6(a)` — zero efeitos duplicados e zero desvios por lease vencido |
