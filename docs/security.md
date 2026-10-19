# Limites de segurança dos workers

As chamadas que o coordenador delega são propostas textuais. O contrato desabilita ferramentas nos clientes compatíveis e inicia o processo no diretório isolado da execução. Isso reduz o acesso acidental ao projeto, mas não cria uma sandbox do sistema operacional.

## Ambiente do processo

`worker_environment.mode` controla as variáveis herdadas:

- `allowlist` é o padrão. Passa variáveis básicas de sistema, caminho do usuário e diretórios de configuração. Nenhum outro valor do ambiente é copiado automaticamente.
- `allow` acrescenta nomes globais de variáveis.
- `allow_by_client` acrescenta nomes somente para o cliente indicado. Use esta opção para uma chave de API que o cliente realmente leia do ambiente.
- `inherit` repassa o ambiente inteiro e deve ser uma escolha explícita.

Exemplo no `routes.json` do usuário:

```json
{
  "worker_environment": {
    "mode": "allowlist",
    "allow": [],
    "allow_by_client": {
      "opencode": ["OPENROUTER_API_KEY"]
    }
  }
}
```

O coordenador registra apenas o modo aplicado, nunca os valores das variáveis. Se o cliente usa autenticação salva em seu diretório de configuração, o processo ainda consegue acessar esse diretório por meio de `HOME` ou `USERPROFILE`; o filtro de ambiente não oculta esses arquivos.

## O que este projeto não promete

O projeto não executa `ai-jail` nem oferece isolamento forte contra código hostil. O `ai-jail` upstream documenta suporte para Linux e macOS, não para Windows, e descreve a ferramenta como prevenção de acidentes, não como uma fronteira contra um atacante determinado. Por isso, o coordenador usa limites portáveis do próprio processo e não declara uma integração automática com o `ai-jail`.

Para código ou prompts não confiáveis, use uma máquina virtual descartável e credenciais sem acesso a dados de produção. Não trate `--tools ""`, modo de planejamento ou o filtro de ambiente como uma sandbox inviolável.
