# AI Coordinator Desktop — Referências visuais aprovadas (Fase 4D.3)

Este pacote contém imagens originais enviadas pelo operador, sem alterações gráficas.

## Hierarquia das referências

1. `01-layout-principal-aprovado.png`: segundo design enviado, REFERÊNCIA PRINCIPAL para estrutura compacta, quatro terminais 2x2, barras finas, rodapés técnicos e ícone da marca em ciano com símbolo de terminal (>_). A marca foi especialmente aprovada pelo operador; tratar como conceito visual, não presumir fonte vetorial disponível.
2. `02-indicadores-e-tooltip.png`: primeiro design enviado, REFERÊNCIA COMPLEMENTAR para legibilidade dos três gráficos circulares em cada terminal, tooltip detalhado de contexto e estados de atividade de agentes.

## Decisões arquiteturais que prevalecem sobre os mockups

- As abas principais da PRIMEIRA versão são: Conversas, Monitoramento, Missões. NÃO introduzir aba Workspace nesta versão.
- Analytics/Cotas pode integrar Monitoramento, não criar nova aba principal sem decisão posterior.
- Configurações via engrenagem (modal), incluindo Sobre → Atualizações.
- Cada terminal: indicadores de contexto, cota 5h e cota semanal; dados exibidos devem ter proveniência real/estimado/indisponível. Nunca inventar percentuais.
- Preservar identidade escura, compacta e focada no terminal. Adequar acessibilidade e responsividade.
- Visual é referência, não evidência de integração funcional; captura nativa Tauri/WebView2 e validação humana são necessárias.
- Mudanças em Core e IPC não devem alterar o layout aprovado sem justificativa e autorização.

## Critérios mínimos de comparação visual

Capturar a janela nativa na mesma resolução (quando possível) e comparar navegação, ícone, cabeçalho, distribuição 2x2, tamanho/posição das roscas, tooltip, rodapés, fontes, espaçamentos e legibilidade em telas menores. Não produzir mocks com métricas fictícias sem marcação explícita.

Este pacote não autoriza publicar uma nova release nem encerra o gate operacional 4D.2.
