# AI Coordinator — Guia de Segurança e Limitações Operacionais (Fase 4C.2)

**Versão:** 1.0.0 (Fase 4C.2.6)  
**Data:** 2026-11-10  
**Status:** Auditado e Consolidado Offline  
**Repositório:** [D:\ai-coordinator](file:///D:/ai-coordinator)  
**Branch de Trabalho:** `feature/phase4c-2-ed25519-truststore`  

---

## 1. Princípios de Segurança e Modelo de Ameaça

A arquitetura de governança da Fase 4C.2 do AI Coordinator implementa defesa em profundidade para proteger despachos a provedores externos de IA (com foco na integração OpenRouter), garantindo que:
1. **Nenhum centavo ($0.00 USD) seja gasto acidentalmente ou fraudulentamente:** Apenas modelos estritamente gratuitos constantes de catálogo verificado e com precificação zero em todas as dimensões são autorizados (`FreeCostGuard`).
2. **Nenhuma requisição seja despachada sem autorização criptográfica e humana:** Exige validação tripartite combinando chave Ed25519 confiável na Trust Store, identidade verificada do supervisor humano (`HumanOperationalApproval`) e commit HEAD de código auditado.
3. **Nenhum manifesto possa ser reutilizado (Anti-Replay Persistente):** O identificador de autorização é gravado de forma irreversível em tabela SQLite com restrição de chave primária antes da liberação do despacho.
4. **Nenhum despacho exceda o teto global concedido:** O número de reservas ativas, concluídas ou indeterminadas é estritamente limitado a `max_dispatches` sob transações atômicas `BEGIN IMMEDIATE`.
5. **Nenhuma requisição de rede seja iniciada antes da persistência durável:** O Ponto de Compromisso Irreversível (PIC `SEND_COMMITTED`) drena a cota e persiste eventos de auditoria com `fsync` antes da abertura de soquetes TLS.
6. **Nenhuma retransmissão ocorra após falha indeterminada (`UNKNOWN`):** Se a conexão cair após o envio ou ocorrer timeout, o despacho é marcado como `UNKNOWN` e seu saldo é permanentemente deduzido para evitar cobrança duplicada remota.

---

## 2. Pilares de Controle e Invariantes Fundamentais

### 2.1 Verificação Tripartite (Criptografia, Código e Humano)
O `TripartiteAuthorizationVerifier` inspeciona conjuntamente três dimensões ortogonais:
- **Autoridade de Chave (Ed25519):** A assinatura do manifesto canônico é validada contra chaves ativas registradas na `TrustStore`. Chaves revogadas ou expiradas provocam rejeição imediata (*fail-closed*).
- **Integridade do Código (Code Commit):** O commit registrado no manifesto deve corresponder rigorosamente ao commit HEAD do ambiente de runtime.
- **Autoridade Operacional Humana:** O consentimento humano não pode ser estabelecido por booleanos isolados. Requer uma sessão autenticada por chave mestra de supervisor (`supervisor_key`) e um token HMAC-SHA256 vinculado ao `authorization_id` e ao propósito estrito da chamada (`PERMISSION_DISPATCH_OPENROUTER_FREE`).

### 2.2 Ponto de Compromisso Irreversível (PIC `SEND_COMMITTED`)
Antes de invocar o transporte HTTP ou abrir conexões de rede:
1. A reserva transiciona de `RESERVED` para `SEND_COMMITTED` no SQLite WAL.
2. Todas as condições de autorização (Trust Store, validade temporal, permissões, commit) são revalidadas no banco.
3. O evento `DISPATCH_COMMITTED` é gravado no Transactional Outbox.
4. Se o envio falhar posteriormente por motivos de rede ou timeout, a cota não é devolvida: o estado é mantido em `UNKNOWN` para evitar duplicidade no provedor remoto.

### 2.3 Trava Nativa de Sistema Operacional contra TOCTOU
A sincronização cross-process do Transactional Outbox e a serialização de concorrência utilizam primitivas nativas do kernel do sistema operacional:
- **Windows:** `msvcrt.locking` bloqueando o primeiro byte do descritor de arquivo aberto.
- **POSIX:** `fcntl.flock` com bloqueio exclusivo (`LOCK_EX`).
- **Eliminação de TOCTOU:** O arquivo de lock é permanente e mantido aberto. Não há chamadas de exclusão (`os.unlink`), eliminando qualquer janela de corrida onde dois processos concorrentes recriem arquivos de trava distintos. Em caso de terminação abrupta do processo (`kill -9` ou crash), o kernel do sistema operacional libera automaticamente o lock ao fechar os descritores.

---

## 3. Limitações Conhecidas e Recomendações de Otimização

A auditoria quantitativa da Fase 4C.2.5 identificou aspectos operacionais e de desempenho que devem ser compreendidos pelos operadores:

### 3.1 Contenção de I/O em Concorrência Intensa
- **Comportamento Observado:** Sob carga simultânea de 8 threads executando 120 despachos concorrentes em um único arquivo SQLite, a latência mediana foi de **632.1 ms**, com P95 de **9.34 segundos** e pico de **22.8 segundos**.
- **Causa Arquitetural:** O SQLite opera sob o modelo de escritor único com `BEGIN IMMEDIATE`, e o Transactional Outbox executa `fsync` durável sob trava exclusiva de sistema operacional a cada projeção.
- **Recomendação para Produção:** 
  1. Em ambientes com alta taxa de concorrência, recomenda-se habilitar agrupamento em lote (*batching*) para projeção outbox em processo de background, reduzindo a frequência de `fsync`.
  2. Implementar pool de conexões persistentes para evitar custos repetidos de abertura de banco.

### 3.2 Escopo dos Testes Offline vs Ambiente Real
- **Ambiente Offline:** Todos os testes das Fases 4C.2.1 a 4C.2.6 foram executados sem tráfego de rede externo ($0.00 USD incorridos).
- **Limitação de Garantia:** A ausência de violações no ambiente local comprova a correção lógica das regras do software, mas **NÃO** garante a disponibilidade, tempos de resposta ou estabilidade dos servidores remotos da OpenRouter.
- **Staging Operacional Recomendado:** Antes da liberação para produção com credenciais reais de API, o operador deve realizar um teste de homologação operacional em ambiente controlado (staging) com um token real da OpenRouter, validando o tráfego TLS em rede pública.

### 3.3 Suporte Multiplataforma
- **Validado Efetivamente:** Windows 11 (AMD64) com Python 3.14 (151/151 testes específicos e 498/501 testes globais aprovados).
- **Pendente de Execução Nativa:** Linux e macOS possuem suporte de código consolidado (primitivas `fcntl.flock`, `resource.getrusage`, `os.killpg`), mas requerem execução independente da suíte completa em runners dedicados dessas plataformas antes de homologação oficial.
