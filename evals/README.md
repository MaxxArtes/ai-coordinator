# Avaliar candidatos de rota

O benchmark usa o mesmo conjunto de prompts para os candidatos configurados, grava a saída e os dados de uso, e deixa a nota de qualidade para revisão humana. Essa separação ajuda a perceber se uma resposta válida também está correta e útil.

## Executar

Cada par caso/candidato agenda no máximo uma chamada de worker. O limite é obrigatório porque chamadas podem consumir quota ou saldo:

```console
ai-coordinator benchmark --project . --cases evals/cases.example.json --max-calls 12
```

Filtre por categoria ou cliente para reduzir o custo:

```console
ai-coordinator benchmark --project . --cases evals/cases.example.json --category extract --client codex --max-calls 1
```

O comando não chama um juiz de IA e não modifica o projeto informado. As saídas ficam em um JSON no diretório de dados do coordenador. Para cada linha, revise a resposta e preencha `quality_score` com uma nota de 0 a 5 e `review_notes` com o motivo.

Gere um resumo depois de preencher as notas:

```console
ai-coordinator benchmark-report --input caminho/benchmark.json
```

O resumo classifica somente candidatos com notas humanas. Tempo, tokens e custo são medidas auxiliares; custo ou uso ausente aparece como `n/d`, não como zero.

## Cuidados com os casos

- Use prompts sintéticos, sem credenciais, dados pessoais ou informações confidenciais de clientes.
- Mantenha os mesmos casos e critérios ao comparar modelos; registre mudanças de prompt como uma nova versão da bateria.
- A saída é uma proposta de avaliação. Confirme manualmente os critérios e não altere rotas padrão por uma única execução.
- Um candidato bloqueado por cooldown pode ser registrado como ignorado sem gerar chamada. O relatório preserva o estado e as tentativas para essa distinção.
