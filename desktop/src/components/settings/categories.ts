/**
 * The nine settings categories of the canonical roadmap (docs/desktop/phase4d-roadmap.md, §2.1 and Marco 4D.3).
 *
 * `state` follows decision D4: nothing is presented as operational before it is really integrated.
 * - "available": implemented and backed by real data.
 * - "planned": structure only; nothing in this category is saved or applied yet.
 * Items marked `unsupported` would require changing the Core (out of scope) or a secure store that
 * does not exist yet on that platform (decision D8).
 */
export type SettingsCategoryId =
  | "perfil"
  | "geral"
  | "aparencia"
  | "terminal"
  | "coordinator"
  | "agentes"
  | "provedores"
  | "seguranca"
  | "sobre";

export type CategoryState = "available" | "planned";

export interface CategoryItem {
  label: string;
  unsupported?: string;
}

export interface SettingsCategory {
  id: SettingsCategoryId;
  label: string;
  summary: string;
  state: CategoryState;
  stage?: string;
  items: CategoryItem[];
}

export const SETTINGS_CATEGORIES: SettingsCategory[] = [
  {
    id: "perfil",
    label: "Perfil",
    summary: "Identidade local, chaves públicas e preferências do operador.",
    state: "planned",
    stage: "Etapa 4",
    items: [{ label: "Nome de exibição do perfil local" }, { label: "Chaves públicas confiáveis (somente leitura)" }, { label: "Preferências do operador" }],
  },
  {
    id: "geral",
    label: "Geral",
    summary: "Inicialização, persistência de layout e atalhos de teclado.",
    state: "planned",
    stage: "Etapas 4 e 5",
    items: [
      { label: "Comportamento ao iniciar (projeto autorizado a abrir)" },
      { label: "Persistência das preferências de layout" },
      { label: "Restauração de sessões de terminal", unsupported: "O servidor IPC ainda não oferece restauração; hoje existe apenas exportação." },
      { label: "Mapa de atalhos de teclado" },
    ],
  },
  {
    id: "aparencia",
    label: "Aparência",
    summary: "Modo de baixa estimulação visual, contraste, tipografia e densidade.",
    state: "planned",
    stage: "Etapa 4",
    items: [
      { label: "Modo de baixa estimulação (paleta suave, sem animações)" },
      { label: "Respeito à preferência do sistema por movimento reduzido (já ativo)" },
      { label: "Tipografia e densidade visual" },
    ],
  },
  {
    id: "terminal",
    label: "Terminal",
    summary: "Opções do xterm.js: cursor, fontes e buffers.",
    state: "planned",
    stage: "Etapa 4",
    items: [
      { label: "Fonte, tamanho e cursor" },
      { label: "Histórico limitado a 1.000 linhas e 512 KB por terminal" },
      { label: "Shell padrão (exige corrigir o contrato do parâmetro command)" },
    ],
  },
  {
    id: "coordinator",
    label: "Coordinator",
    summary: "Parâmetros de supervisão, timeouts e orçamentos máximos de missão.",
    state: "planned",
    stage: "Etapa 4",
    items: [
      { label: "Timeouts e limites de supervisão (validados pelos esquemas do Core)" },
      { label: "Orçamentos máximos de missão" },
      { label: "Aplicação efetiva pelo Core", unsupported: "O desktop ainda não executa missões; a aplicação chega com a 4D.5." },
    ],
  },
  {
    id: "agentes",
    label: "Agentes",
    summary: "Descoberta de CLIs locais de agentes e servidores MCP.",
    state: "planned",
    stage: "Etapa 4",
    items: [
      { label: "CLIs conhecidas pelo Core (Codex, Claude Code, OpenCode, AGY)" },
      { label: "Gemini CLI", unsupported: "Exige um novo contrato de cliente no Core, fora do escopo da 4D.3." },
      { label: "Descoberta de servidores MCP" },
    ],
  },
  {
    id: "provedores",
    label: "Provedores",
    summary: "Credenciais seguras, limites de cota e endpoints locais.",
    state: "planned",
    stage: "Etapa 4",
    items: [
      { label: "Credenciais no cofre do perfil (DPAPI no Windows)" },
      {
        label: "Credenciais persistentes no Linux e no macOS",
        unsupported: "Desabilitado até existir cofre seguro do sistema (decisão D8, planejado na 4D.6).",
      },
      { label: "Limites de cota e rotas de fallback" },
      { label: "Endpoints configuráveis", unsupported: "Exige alteração no Core, fora do escopo da 4D.3." },
    ],
  },
  {
    id: "seguranca",
    label: "Segurança",
    summary: "Permissões de sandbox, diretórios autorizados e políticas de acesso.",
    state: "planned",
    stage: "Etapa 4",
    items: [{ label: "Diretórios autorizados (listar e revogar)" }, { label: "Permissões de sandbox" }, { label: "Políticas de acesso" }],
  },
  {
    id: "sobre",
    label: "Sobre",
    summary: "Versões, licença, diagnóstico e atualizações.",
    state: "available",
    items: [],
  },
];
