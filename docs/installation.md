# Guia de Instalação e Integração — AI Coordinator

Este guia reúne todas as formas disponíveis para instalar e conectar o **AI Coordinator**: experiência completa (com os componentes homologados atuais), somente **Desktop** (Windows Pre-Release), somente **CLI/Core + MCP** (multiplataforma estável) e integração modular com agentes que você já utiliza (como **Claude Code**, **OpenCode**, **Codex** e **Antigravity / AGY**).

> [!NOTE]
> **Transparência sobre o estado atual:** o instalador gráfico unificado (um único arquivo que instala e configura tudo automaticamente, inclusive com modo offline completo) é uma entrega planejada para o **Marco 4D.6**. Hoje você instala o Desktop (com Core `v0.6.0` e Python `3.11.9` privados já embutidos) e, se desejar usar o Coordinator fora do app pelos seus agentes de terminal, instala o pacote Python `ai-coordinator[mcp]==0.6.0` e a integração do agente escolhido.

---

## 1. Requisitos e compatibilidade por componente

| Componente | Versão atual | Situação | Sistemas suportados | Requisitos |
| --- | --- | --- | --- | --- |
| **Core Python (`ai-coordinator`)** | `v0.6.0` | Estável (PyPI e GitHub) | Windows, macOS, Linux | Python `>= 3.11` |
| **Servidor MCP (`ai-coordinator-mcp`)** | `v0.6.0` | Estável (extra `[mcp]`) | Windows, macOS, Linux | `mcp>=2,<3` instalado via extra `[mcp]` |
| **AI Coordinator Desktop** | `0.6.1-alpha.52` | Pre-release (Alpha) | Windows 10/11 x64 | WebView2 (nativo no Windows 11; runtime Microsoft Edge WebView2 no Windows 10). Não exige Python/Node globais |
| **Agent Skill (`SKILL.md`)** | `v0.6.0` | Suportada | Windows, macOS, Linux | Qualquer cliente compatível com Agent Skills |
| **Plugin Claude Code** | `v0.6.0` | Empacotado | Windows, macOS, Linux | Claude Code CLI + `ai-coordinator[mcp]==0.6.0` no `PATH` |
| **Plugin OpenCode V2** | `v0.6.0` | Empacotado | Windows, macOS, Linux | OpenCode V2 + Node.js/npm + `ai-coordinator==0.6.0` no `PATH` |
| **Codex / Antigravity (AGY)** | `v0.6.0` | Documentado (receita genérica) | Windows, macOS, Linux | `ai-coordinator[mcp]==0.6.0` + configuração MCP stdio + `SKILL.md` |
| **Desktop Linux / macOS + Instalador Unificado** | — | Planejado (`4D.6`) | — | Previsto após o Marco `4D.5` (Mission Control) |
| **Patch de identidade do launcher (`7fae072`) / AI Memory lab** | — | Em laboratório (não distribuídos) | — | Não fazem parte das releases públicas atuais |

Consulte a [Matriz de Suporte](support-matrix.md) para detalhes do que é validado em CI e do que exige conferência manual no cliente nativo.

---

## 2. Já tenho Claude Code (ou outro agente) instalado — início rápido

Se você já usa **Claude Code** no seu computador e quer testar o AI Coordinator sem mexer nas suas credenciais ou provedores existentes:

1. **Instale o Core + servidor MCP (Python `>= 3.11`):**
   ```console
   python -m pip install "ai-coordinator[mcp]==0.6.0"
   ```
2. **Adicione o marketplace e instale o plugin no Claude Code:**
   ```console
   claude plugin marketplace add MaxxArtes/ai-coordinator
   claude plugin install ai-coordinator@ai-coordinator-marketplace
   ```
3. **Inicialize o projeto onde você está trabalhando:**
   ```console
   ai-coordinator bootstrap --project .
   ```

O que acontece ao fazer isso:
- Suas credenciais e configurações existentes do Claude Code **não são alteradas** e nenhum provedor externo é instalado ou habilitado automaticamente.
- O estado das tarefas fica no diretório de dados do usuário (`%USERPROFILE%\.ai-coordinator` no Windows ou `~/.ai-coordinator` no Linux/macOS; personalizável com `AI_COORDINATOR_HOME`).
- O arquivo `routes.example.json` copiado na primeira execução começa em modo seguro (`allowlist` para ambiente de workers); revise `routes.json` apenas se e quando quiser delegar chamadas a CLIs locais configuradas por você.

Se desejar também a interface gráfica (abas **Conversas** com terminais ConPTY, **Configurações** e **Monitoramento** no Windows), basta instalar o **Desktop `0.6.1-alpha.52`** descrito na seção seguinte — ambos compartilham os mesmos conceitos e preservam suas configurações separadas.

---

## 3. Instalação completa (com os componentes homologados hoje)

Para ter o ambiente completo disponível hoje no Windows 10/11 x64:

1. **Instale o AI Coordinator Desktop (`0.6.1-alpha.52`, Pre-release):** baixe e execute `AI-Coordinator-Desktop_0.6.1-alpha.52_x64-setup.exe` na [Release `v0.6.1-alpha.52`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.1-alpha.52). Ele já traz um runtime Python `3.11.9` privado e o Core `v0.6.0` embarcado para uso interno da interface gráfica.
2. **Instale a CLI e o servidor MCP no seu ambiente Python (`>= 3.11`):**
   ```console
   python -m pip install "ai-coordinator[mcp]==0.6.0"
   ```
   > **Nota:** o Python privado dentro do Desktop roda em modo isolado (`-I`) exclusivamente para a janela gráfica. Instalar `ai-coordinator[mcp]==0.6.0` no seu Python de terminal disponibiliza `ai-coordinator` e `ai-coordinator-mcp` no `PATH` para o Claude Code, OpenCode, Codex, AGY e uso direto no shell.
3. **Conecte os agentes que você já usa** seguindo a [Seção 6](#6-integração-com-agentes-já-instalados).
4. **Confira a instalação:**
   ```console
   ai-coordinator --version
   ai-coordinator routes
   ai-coordinator bootstrap --project .
   ```

---

## 4. Instalação individual — AI Coordinator Desktop (Windows x64 Pre-release)

O Desktop `0.6.1-alpha.52` está disponível em dois formatos na [Release `v0.6.1-alpha.52`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.1-alpha.52):

| Formato | Arquivo | Quando usar |
| --- | --- | --- |
| **Instalador NSIS** | `AI-Coordinator-Desktop_0.6.1-alpha.52_x64-setup.exe` | Instalação padrão por usuário em `%LOCALAPPDATA%\AI Coordinator Desktop` com atalhos e desinstalador |
| **Pacote Portátil** | `ai-coordinator-desktop-alpha-portable.zip` | Execução direta a partir de uma pasta extraída, sem instalador (inclui `BUILD-MANIFEST.json`, `BUILD-COMMIT.txt` e `BUILD-PIP-FREEZE.txt`) |
| **Assinatura do Atualizador** | `AI-Coordinator-Desktop_0.6.1-alpha.52_x64-setup.exe.sig` | Verificação criptográfica Ed25519 / Minisign (chave pública `499FF8912D665FE8`) |
| **Manifesto de Hashes** | `checksums-desktop-0.6.1-alpha.52.sha256` | Conferência SHA-256 dos artefatos baixados |

### 4.1. Verificação de integridade antes de executar

No PowerShell, compare o hash SHA-256 do arquivo baixado com `checksums-desktop-0.6.1-alpha.52.sha256`:

```powershell
Get-FileHash .\AI-Coordinator-Desktop_0.6.1-alpha.52_x64-setup.exe -Algorithm SHA256
```

### 4.2. Assinatura Minisign vs. Windows Authenticode (SmartScreen)

- **Assinatura Minisign (Ed25519, presente):** os artefatos de atualização e o instalador NSIS possuem assinatura verificável pela chave pública fixada no aplicativo (`499FF8912D665FE8`).
- **Assinatura Authenticode do Windows (`NotSigned`):** o executável e o instalador **não** possuem certificado comercial Authenticode da Microsoft nesta fase Alpha. Por isso, o Windows Defender SmartScreen pode exibir um aviso ao abrir o instalador pela primeira vez. Confira sempre o `SHA-256` publicado na release antes de prosseguir.

### 4.3. Onde ficam seus dados e como recuperar ou desinstalar

- **Executável instalado (NSIS):** `%LOCALAPPDATA%\AI Coordinator Desktop\`
- **Configurações e interface do Desktop:** `%APPDATA%\ai-coordinator-desktop\`
- **Perfis, projetos autorizados, cofre DPAPI (`vault.enc`) e tarefas:** `%USERPROFILE%\.ai-coordinator\`

As atualizações e a desinstalação preservam seus dados de perfil e credenciais. Caso precise reverter ou fazer backup manual antes de testar uma nova pre-release, copie `%USERPROFILE%\.ai-coordinator\` e `%APPDATA%\ai-coordinator-desktop\`.

---

## 5. Instalação individual — Core, CLI e Servidor MCP (`v0.6.0`)

### 5.1. Via PyPI (recomendado para uso via terminal)

```console
python -m pip install "ai-coordinator[mcp]==0.6.0"
```

Se quiser apenas a CLI e a biblioteca Python sem o servidor MCP:

```console
python -m pip install "ai-coordinator==0.6.0"
```

### 5.2. A partir dos artefatos da Release `v0.6.0` no GitHub

Baixe `ai_coordinator-0.6.0-py3-none-any.whl` (ou `.tar.gz`) e `checksums.sha256` na [Release `v0.6.0`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.0), valide o SHA-256 e instale em um ambiente virtual limpo:

```console
python -m venv .venv
python -m pip install "ai_coordinator-0.6.0-py3-none-any.whl[mcp]"
```

### 5.3. A partir do código-fonte

```console
git clone https://github.com/MaxxArtes/ai-coordinator.git
cd ai-coordinator
python -m pip install ".[mcp]"
```

### 5.4. Configuração manual do servidor MCP (stdio)

Qualquer cliente compatível com MCP por `stdio` pode iniciar `ai-coordinator-mcp`:

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

---

## 6. Integração com agentes já instalados

> [!IMPORTANT]
> Nenhuma integração altera seus arquivos ou provedores sem que você execute o comando ou edite a configuração do respectivo cliente.

### 6.1. Claude Code

**Opção A — Marketplace oficial do repositório (requer `ai-coordinator[mcp]` no `PATH`):**
```console
claude plugin marketplace add MaxxArtes/ai-coordinator
claude plugin install ai-coordinator@ai-coordinator-marketplace
```

**Opção B — ZIP empacotado da Release `v0.6.0`:**
```console
claude --plugin-url https://github.com/MaxxArtes/ai-coordinator/releases/download/v0.6.0/ai-coordinator-claude-plugin-0.6.0.zip
```

**Opção C — Apenas o servidor MCP manualmente:** adicione o bloco `mcpServers` da [Seção 5.4](#54-configuração-manual-do-servidor-mcp-stdio) ao seu `.mcp.json`.

### 6.2. OpenCode V2

O arquivo `plugins/opencode/package.json` declara `publishConfig.registry` apontando para o GitHub Packages (`https://npm.pkg.github.com`), que exige token do GitHub ao instalar pelo nome `@maxxartes/ai-coordinator-opencode`. Para instalar **sem autenticação no npm do GitHub**, use o pacote local do repositório clonado ou o tarball `.tgz` da Release `v0.6.0`:

**Opção A — Via tarball da Release `v0.6.0`:**
1. Baixe `maxxartes-ai-coordinator-opencode-0.6.0.tgz` da [Release `v0.6.0`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.0) e extraia em um diretório local (por exemplo `./plugins/opencode`), ou instale no projeto com `npm install ./maxxartes-ai-coordinator-opencode-0.6.0.tgz`.
2. Aponte sua configuração OpenCode para a pasta do plugin:
   ```json
   {
     "plugins": ["./plugins/opencode"]
   }
   ```

**Opção B — Via repositório clonado:** aponte `"plugins": ["./plugins/opencode"]` diretamente após instalar `ai-coordinator==0.6.0` no Python.

### 6.3. Agent Skill avulsa (`SKILL.md`)

Para clientes que suportam o padrão Agent Skills sem plugin próprio:
- Copie [`skills/ai-coordinator/SKILL.md`](../skills/ai-coordinator/SKILL.md) (ou baixe `ai-coordinator-skill-0.6.0.zip` da [Release `v0.6.0`](https://github.com/MaxxArtes/ai-coordinator/releases/tag/v0.6.0)) para o diretório de skills do seu cliente.

### 6.4. Codex e Antigravity (AGY)

Atualmente não há plugin dedicado nem teste end-to-end automatizado dentro dos clientes Codex ou AGY (veja [Matriz de Suporte](support-matrix.md)). Para utilizá-los:
1. Instale `ai-coordinator[mcp]==0.6.0` no Python.
2. Registre `ai-coordinator-mcp` como servidor MCP `stdio` na configuração do seu cliente.
3. Adicione [`skills/ai-coordinator/SKILL.md`](../skills/ai-coordinator/SKILL.md) como skill disponível.

### 6.5. Uso opcional com `ai-memory`

O AI Coordinator funciona **de forma autônoma** sem depender de `ai-memory` (usando seu próprio banco SQLite `coordination.sqlite`, espelhos JSON e checkpoints). Caso você também utilize uma versão estável do `ai-memory`, o Coordinator preserva opcionalmente a variável `AI_MEMORY_WORKSTREAM_ID` nas tarefas criadas sem replicar o conteúdo da wiki. Candidatos experimentais/laboratoriais do `ai-memory` não fazem parte desta distribuição.

---

## 7. Licença

Todo o repositório e seus artefatos distribuídos usam a licença [**PolyForm Noncommercial 1.0.0**](../LICENSE) (acompanhada do arquivo [`NOTICE`](../NOTICE)). Consulte os termos antes de redistribuir, empacotar ou utilizar em ambiente comercial.
