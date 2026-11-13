/**
 * The nine settings categories of the canonical roadmap (docs/desktop/phase4d-roadmap.md, §2.1 and Marco 4D.3).
 *
 * `state` follows decision D4: nothing is presented as operational before it is really integrated.
 * - "available": backed by real data — `backend` says which ("settings" via the Core IPC schema, "vault" via
 *   the profile vault) — or read-only (Sobre).
 * - "planned": structure only; nothing in this category is saved or applied yet.
 * Items marked `unsupported` would require changing the Core (out of scope) or a secure store that does not
 * exist yet on that platform (decision D8).
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
  backend?: "settings" | "vault";
  stage?: string;
  items: CategoryItem[];
}

export const SETTINGS_CATEGORIES: SettingsCategory[] = [
  {
    id: "perfil",
    label: "Perfil",
    summary: "Identidade local e preferências do operador.",
    state: "available",
    backend: "settings",
    items: [],
  },
  {
    id: "geral",
    label: "Geral",
    summary: "Comportamento ao iniciar e preferências gerais.",
    state: "available",
    backend: "settings",
    items: [],
  },
  {
    id: "aparencia",
    label: "Aparência",
    summary: "Modo de baixa estimulação visual e densidade. A preferência do sistema por movimento reduzido é sempre respeitada.",
    state: "available",
    backend: "settings",
    items: [],
  },
  {
    id: "terminal",
    label: "Terminal",
    summary: "Fonte, cursor e histórico dos terminais (limite do buffer: 1.000 linhas e 512 KB).",
    state: "available",
    backend: "settings",
    items: [],
  },
  {
    id: "coordinator",
    label: "Coordinator",
    summary: "Orçamentos máximos de missão, validados pelos esquemas do Core.",
    state: "available",
    backend: "settings",
    items: [],
  },
  {
    id: "agentes",
    label: "Agentes",
    summary: "Descoberta de CLIs locais de agentes e servidores MCP.",
    state: "planned",
    stage: "etapa futura",
    items: [
      { label: "CLIs conhecidas pelo Core (Codex, Claude Code, OpenCode, AGY)" },
      { label: "Gemini CLI", unsupported: "Exige um novo contrato de cliente no Core, fora do escopo da 4D.3." },
      { label: "Descoberta de servidores MCP" },
    ],
  },
  {
    id: "provedores",
    label: "Provedores",
    summary: "Credenciais no cofre do perfil. Limites de cota e rotas de fallback serão integrados depois.",
    state: "available",
    backend: "vault",
    items: [],
  },
  {
    id: "seguranca",
    label: "Segurança",
    summary: "Permissões de sandbox, diretórios autorizados e políticas de acesso.",
    state: "planned",
    stage: "etapa futura",
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
