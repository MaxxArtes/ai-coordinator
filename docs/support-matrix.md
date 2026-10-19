# Matriz de suporte

Esta tabela distingue integração que o pacote implementa de integração que foi executada dentro do cliente nativo. Um arquivo de configuração válido, sozinho, não prova que uma versão específica do cliente funciona.

| Plataforma ou cliente | Situação | Verificação |
|---|---|---|
| Windows, macOS e Linux | Suportados para a biblioteca Python e a CLI | CI executa a suíte em cada sistema. |
| Servidor MCP por stdio | Suportado como extra opcional | CI instala o extra e cria o servidor. O CI não abre uma conversa em cada host. |
| Agent Skill | Suportada | CI verifica que as cópias distribuídas são idênticas e estão nos pacotes. |
| Plugin Claude Code | Empacotado | CI valida os manifestos e os arquivos incluídos. A execução no cliente precisa de confirmação manual. |
| Plugin OpenCode V2 | Empacotado | CI valida o manifesto e o tarball. A execução no cliente precisa de confirmação manual. |
| Codex | Receita genérica de MCP e skill | Não há plugin próprio nem teste end-to-end no Codex. |
| AGY | Receita genérica de MCP e skill | Não há plugin próprio nem teste end-to-end no AGY. |
| Plugin OpenCode V1 | Não declarado como suportado | O plugin atual usa o contrato V2. |

O arquivo [`support-matrix.json`](support-matrix.json) é a fonte legível por máquina. `scripts/check_support_matrix.py` confere se as evidências citadas existem, se as versões dos plugins acompanham o pacote e se os arquivos de skill não divergiram.

## Significado dos estados

- **Suportado**: o repositório implementa a integração e há verificações automatizadas compatíveis com o que a linha afirma.
- **Empacotado**: há um artefato distribuível, mas o uso no cliente nativo ainda precisa de confirmação manual.
- **Documentado**: há uma receita de configuração, mas o cliente nativo não é executado no CI.
- **Experimental**: a integração existe, mas tem cobertura ou estabilidade limitada.
- **Não declarado**: não prometemos compatibilidade para essa combinação.

O CI valida artefatos e interfaces locais. Ele não testa saldo, login, quota ou acesso a modelos externos.
