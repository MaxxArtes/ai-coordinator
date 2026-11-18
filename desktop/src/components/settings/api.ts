/**
 * Typed access to the settings, vault, profile and inventory commands (Marco 4D.3, Etapas 4 e 7).
 * Validation is authoritative in the Core IPC; the UI only mirrors ranges for input hints.
 * Vault calls never return secret values, and nothing here logs request payloads.
 */

import { TERMINAL_SETTINGS_EVENT, validInt } from "./terminalPreferences";

export type CallTauri = (cmd: string, args?: Record<string, any>) => Promise<any>;

export const SCHEMA_VERSION = 1;

export type BackedCategory = "perfil" | "geral" | "aparencia" | "terminal" | "coordinator";
export type Application = "interface" | "unsupported";

export interface CategorySnapshot {
  values: Record<string, any>;
  saved_fields: string[];
  /** True only without invalid stored fields and without a protected (read-only) schema. */
  validated: boolean;
  applied: boolean;
  application: Application;
  /** Fields whose stored value is invalid: `values` shows the default and the stored value stays untouched. */
  invalid_fields?: string[];
}

export interface StoredIssue {
  reason: "incompatible_version" | "invalid_stored_value";
  /** Fixed, safe text from the host; never a stored value. */
  message: string;
}

export interface SettingsSnapshot {
  schema_version: number;
  stored_schema_version: number | null;
  /** Saved by another (future or incompatible) version, or not an object: nothing can be written. */
  read_only?: boolean;
  stored_issue?: StoredIssue | null;
  migration?: { required: boolean; from_version: number | null; to_version: number };
  categories: Record<BackedCategory, CategorySnapshot>;
  unsupported_categories: Record<string, string>;
}

export interface StructuredError {
  code: number;
  message: string;
  field?: string;
  reason?: string;
}

export interface VaultStatus {
  available: boolean;
  backend: string;
  reason?: "unavailable" | "unreadable" | "busy" | string;
}

export interface AgentInfo {
  name: string;
  client: string;
  available: boolean;
  reason?: string;
  worker_headless?: boolean;
  coordinator_headless?: boolean;
}

/** discovery.agents: names and capabilities only, never a configuration value. */
export interface AgentsSnapshot {
  agents: AgentInfo[];
  mcp_servers: string[];
  /** False when some MCP source could not be read: the list may be incomplete. */
  mcp_complete: boolean;
  source: "configured" | "packaged_defaults" | "unreadable" | string;
  unsupported: { name: string; reason: string }[];
}

export interface RouteCandidate {
  client: string;
  model: string;
  effort?: string;
}

/** providers.routes: routing model identifiers only. */
export interface RoutesSnapshot {
  routes: { category: string; candidates: RouteCandidate[] }[];
  available: boolean;
  source: "configured" | "packaged_defaults" | "unreadable" | string;
  reason?: string;
  quota_status: "unknown" | string;
  quota_milestone: string;
}

export interface SecurityStatus {
  authorized_projects: string[];
  /** Authorized entries that open a whole disk, share or home folder (L5), with their stored text; absent on older hosts. */
  broad_projects?: string[];
  policy: Record<string, boolean | string>;
}

export interface AuthorizeResult {
  success: boolean;
  profile_name?: string;
  /** The directory as the host stored it (canonical). */
  project_path: string;
  authorized_projects: string[];
}

export interface RevokeResult {
  revoked: boolean;
  authorized_projects: string[];
  last_project: string | null;
}

export interface RememberResult {
  remembered: boolean;
  last_project: string;
}

/** The active profile was unreadable: it was quarantined (copied byte for byte) and recreated empty. */
export interface ProfileRecovery {
  reason: "profile_unreadable" | string;
  /** File name only, never a path. */
  backup_file: string;
  message: string;
}

export interface ActiveProfile {
  name: string;
  display_name?: string | null;
  authorized_projects: string[];
  last_project?: string | null;
  recovery?: ProfileRecovery | null;
}

/** Error code of a transport failure between the Tauri host and the Core (L2). */
export const IPC_TRANSPORT_ERROR_CODE = -32007;

/**
 * The commands that still answer with plain text (L2: `call()`) report a transport failure with the same fixed text as
 * the structured ones: it is recognized here, so the reason is known whatever the command's error type.
 */
const DISCONNECTED_MESSAGE = "O Core encerrou a comunicação. Reinicie o aplicativo.";
const TIMEOUT_MESSAGE = /^O Core não respondeu em \d+ s\. /;
function transportReason(message: string): "timeout" | "disconnected" | undefined {
  if (message === DISCONNECTED_MESSAGE) return "disconnected";
  return TIMEOUT_MESSAGE.test(message) ? "timeout" : undefined;
}

/** Normalizes any rejection (Core structured error, transport failure, plain string) without echoing inputs. */
export function toStructuredError(err: unknown): StructuredError {
  const e = err as any;
  if (e && typeof e === "object" && typeof e.message === "string") {
    const reason = e.data?.reason ?? e.reason ?? transportReason(e.message);
    return {
      code: typeof e.code === "number" ? e.code : reason === "timeout" || reason === "disconnected" ? IPC_TRANSPORT_ERROR_CODE : -32603,
      message: e.message,
      field: e.data?.field ?? e.field,
      reason,
    };
  }
  if (typeof err === "string") {
    const reason = transportReason(err);
    return reason ? { code: IPC_TRANSPORT_ERROR_CODE, message: err, field: "ipc", reason } : { code: -32603, message: err };
  }
  return { code: -32603, message: "Falha inesperada na comunicação com o Core." };
}

/** Texts for the reasons the host reports (K3, L2). The host message wins when it says more than a generic failure. */
const REASON_TEXT: Record<string, string> = {
  busy: "Outra janela está usando o perfil ou o cofre. Tente novamente.",
  profile_unreadable: "O perfil não pôde ser lido. Os dados foram preservados.",
  read_only_stored_schema: "As configurações foram gravadas por outra versão e estão protegidas. Nada foi alterado.",
  conflict: "O cofre mudou desde a conferência. Nada foi sobrescrito.",
  unreadable: "Não foi possível ler ou gravar o cofre. Os dados existentes foram preservados.",
  timeout: "O Core não respondeu a tempo. A operação pode ter sido concluída: confira antes de repetir.",
  disconnected: DISCONNECTED_MESSAGE,
  broad_scope: "Este diretório dá acesso amplo (raiz do disco ou pasta pessoal inteira). Confirme explicitamente para autorizar.",
};

/** What to show for an error: the host message, or the reason's fixed text when the message is generic or missing. */
export function errorText(err: unknown): string {
  const e = toStructuredError(err);
  const generic = !e.message.trim() || /^internal error/i.test(e.message);
  return generic && e.reason && REASON_TEXT[e.reason] ? REASON_TEXT[e.reason] : e.message;
}

async function call<T>(callTauri: CallTauri, cmd: string, args?: Record<string, any>): Promise<T> {
  try {
    return (await callTauri(cmd, args)) as T;
  } catch (err) {
    throw toStructuredError(err);
  }
}

export const getSettings = (callTauri: CallTauri) => call<SettingsSnapshot>(callTauri, "settings_get");

export const updateSettings = (callTauri: CallTauri, category: BackedCategory, values: Record<string, any>) =>
  call<SettingsSnapshot>(callTauri, "settings_update", { schemaVersion: SCHEMA_VERSION, category, values });

export const getVaultStatus = (callTauri: CallTauri) => call<VaultStatus>(callTauri, "vault_status");

export const getVaultKeysSnapshot = (callTauri: CallTauri) =>
  call<{ keys: string[]; revision?: string }>(callTauri, "vault_list_keys");
export const listVaultKeys = (callTauri: CallTauri) => getVaultKeysSnapshot(callTauri).then((r) => r.keys);

/** Without a revision: create only if absent. With a revision: replace only that exact revision. */
export const setVaultSecret = (callTauri: CallTauri, key: string, value: string, expectedRevision?: string) =>
  call<{ stored: boolean; key: string }>(callTauri, "vault_set_secret", { key, value, ...(expectedRevision !== undefined ? { expectedRevision } : {}) });

/** With the revision of the operator's own reading, a vault changed since then is a conflict and nothing is removed (L4/L6). */
export const deleteVaultSecret = (callTauri: CallTauri, key: string, expectedRevision?: string) =>
  call<{ deleted: boolean; key: string }>(callTauri, "vault_delete_secret", { key, ...(expectedRevision !== undefined ? { expectedRevision } : {}) });

export const getActiveProfile = (callTauri: CallTauri) => call<ActiveProfile>(callTauri, "profile_get_active");

/**
 * Authorizes a project directory. A broad one (disk or share root, the whole home folder) is refused with reason
 * "broad_scope" unless `confirmBroad` carries the operator's explicit yes (L5/L7); the flag is sent only then.
 */
export const authorizeProject = (callTauri: CallTauri, projectPath: string, confirmBroad = false) =>
  call<AuthorizeResult>(callTauri, "profile_authorize_project", { projectPath, ...(confirmBroad ? { confirmBroad: true } : {}) });
export const getAgents = (callTauri: CallTauri) => call<AgentsSnapshot>(callTauri, "discovery_agents");
export const getRoutes = (callTauri: CallTauri) => call<RoutesSnapshot>(callTauri, "providers_routes");
export const getSecurityStatus = (callTauri: CallTauri) => call<SecurityStatus>(callTauri, "security_status");
export const revokeProject = (callTauri: CallTauri, projectPath: string) =>
  call<RevokeResult>(callTauri, "profile_revoke_project", { projectPath });
export const rememberProject = (callTauri: CallTauri, projectPath: string) =>
  call<RememberResult>(callTauri, "profile_remember_project", { projectPath });

/** Same comparison as the host on Windows (os.path.normcase): separators and letter case do not matter. */
const normPath = (p: string) => p.replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
export const samePath = (a: string | null | undefined, b: string | null | undefined): boolean =>
  !!a && !!b && normPath(a) === normPath(b);
/** `child` is `parent` or lies below it. */
export const pathWithin = (child: string, parent: string): boolean =>
  samePath(child, parent) || normPath(child).startsWith(`${normPath(parent)}\\`);

/** Settings the interface applies itself; everything else is only saved and validated for now (D4). */
export const APPLIED_BY_INTERFACE: ReadonlySet<string> = new Set([
  "perfil.display_name", "aparencia.stimulation", "aparencia.density",
  "terminal.font_size", "terminal.cursor_blink", "terminal.scrollback_lines",
]);

/** Saved now, used only by the next start of the application. */
export const APPLIED_ON_NEXT_START: ReadonlySet<string> = new Set(["geral.open_last_project_on_start"]);

/** Stored fields of a category whose value is invalid (names as the host reports them). */
export const invalidFieldsOf = (snapshot: SettingsSnapshot | null | undefined, category: BackedCategory): string[] =>
  Array.isArray(snapshot?.categories?.[category]?.invalid_fields) ? snapshot!.categories[category].invalid_fields! : [];

/**
 * The settings this version uses: null means its own defaults. Settings written by another version, or a malformed
 * envelope, are protected (read_only) and the host says the defaults are in use; the values it still sends are the
 * other version's and are never applied nor shown as in use.
 */
export const settingsInUse = (snapshot: SettingsSnapshot | null | undefined): SettingsSnapshot | null =>
  snapshot && !snapshot.read_only ? snapshot : null;

/** Only a loaded, validated and writable category can be said to be in effect. */
export const categoryInEffect = (snapshot: SettingsSnapshot | null | undefined, category: BackedCategory): boolean =>
  !!snapshot && !snapshot.read_only && snapshot.categories?.[category]?.validated === true && invalidFieldsOf(snapshot, category).length === 0;

const APPEARANCE_CACHE_KEY = "ai-coordinator.appearance.v1";
const APPEARANCE_KEYS = ["stimulation", "density", "terminalFontSize", "terminalScrollback", "terminalCursorBlink"] as const;

/**
 * Applies interface-level preferences to the document (null or protected settings: the defaults). The applied
 * appearance is also kept in this window's storage, only so the next start paints with it before the host answers
 * (never a source of truth).
 */
export function applyInterfaceSettings(given: SettingsSnapshot | null, root: HTMLElement = document.documentElement): void {
  const snapshot = settingsInUse(given);
  const stimulation = snapshot?.categories?.aparencia?.values?.stimulation;
  if (stimulation === "low") root.dataset.stimulation = "low";
  else delete root.dataset.stimulation;
  root.dataset.density = snapshot?.categories?.aparencia?.values?.density === "compact" ? "compact" : "comfortable";
  const terminal = snapshot?.categories?.terminal?.values;
  root.dataset.terminalFontSize = String(validInt(terminal?.font_size, 10, 24, 13));
  root.dataset.terminalScrollback = String(validInt(terminal?.scrollback_lines, 100, 1000, 1000));
  root.dataset.terminalCursorBlink = String(terminal?.cursor_blink !== false);
  try {
    const cached: Record<string, string> = {};
    for (const key of APPEARANCE_KEYS) if (root.dataset[key] !== undefined) cached[key] = root.dataset[key]!;
    root.ownerDocument.defaultView?.localStorage.setItem(APPEARANCE_CACHE_KEY, JSON.stringify(cached));
  } catch {
    // Storage blocked or full: the next start paints with the defaults until the host answers.
  }
  root.ownerDocument.defaultView?.dispatchEvent(new Event(TERMINAL_SETTINGS_EVENT));
}

/** Synchronous, before the first frame: the last applied appearance, validated; anything odd is ignored. */
export function restoreCachedAppearance(root: HTMLElement = document.documentElement): void {
  try {
    const raw = root.ownerDocument.defaultView?.localStorage.getItem(APPEARANCE_CACHE_KEY);
    const cached = raw ? JSON.parse(raw) : null;
    if (!cached || typeof cached !== "object") return;
    if (cached.stimulation === "low") root.dataset.stimulation = "low";
    if (cached.density === "compact" || cached.density === "comfortable") root.dataset.density = cached.density;
    const font = validInt(Number(cached.terminalFontSize), 10, 24, 0);
    if (font) root.dataset.terminalFontSize = String(font);
    const scrollback = validInt(Number(cached.terminalScrollback), 100, 1000, 0);
    if (scrollback) root.dataset.terminalScrollback = String(scrollback);
    if (cached.terminalCursorBlink === "true" || cached.terminalCursorBlink === "false") root.dataset.terminalCursorBlink = cached.terminalCursorBlink;
  } catch {
    // Unreadable cache: the defaults stay until the host answers.
  }
}
