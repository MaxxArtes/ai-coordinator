/**
 * Typed access to the settings and vault commands (Marco 4D.3, Etapa 4).
 * Validation is authoritative in the Core IPC; the UI only mirrors ranges for input hints.
 * Vault calls never return secret values, and nothing here logs request payloads.
 */

export type CallTauri = (cmd: string, args?: Record<string, any>) => Promise<any>;

export const SCHEMA_VERSION = 1;

export type BackedCategory = "perfil" | "geral" | "aparencia" | "terminal" | "coordinator";
export type Application = "pending_ui" | "unsupported";

export interface CategorySnapshot {
  values: Record<string, any>;
  saved_fields: string[];
  validated: boolean;
  applied: boolean;
  application: Application;
}

export interface SettingsSnapshot {
  schema_version: number;
  stored_schema_version: number | null;
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
}

/** Normalizes any rejection (Core structured error, transport failure, plain string) without echoing inputs. */
export function toStructuredError(err: unknown): StructuredError {
  const e = err as any;
  if (e && typeof e === "object" && typeof e.message === "string") {
    return {
      code: typeof e.code === "number" ? e.code : -32603,
      message: e.message,
      field: e.data?.field,
      reason: e.data?.reason,
    };
  }
  return { code: -32603, message: typeof err === "string" ? err : "Falha inesperada na comunicação com o Core." };
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

export const listVaultKeys = (callTauri: CallTauri) =>
  call<{ keys: string[] }>(callTauri, "vault_list_keys").then((r) => r.keys);

export const setVaultSecret = (callTauri: CallTauri, key: string, value: string) =>
  call<{ stored: boolean; key: string }>(callTauri, "vault_set_secret", { key, value });

export const deleteVaultSecret = (callTauri: CallTauri, key: string) =>
  call<{ deleted: boolean; key: string }>(callTauri, "vault_delete_secret", { key });

/** Settings the interface applies itself; everything else is only saved and validated for now (D4). */
export const APPLIED_BY_INTERFACE: ReadonlySet<string> = new Set(["aparencia.stimulation"]);

/** Applies interface-level preferences to the document. Returns the keys that are now really applied. */
export function applyInterfaceSettings(snapshot: SettingsSnapshot | null, root: HTMLElement = document.documentElement): string[] {
  const stimulation = snapshot?.categories?.aparencia?.values?.stimulation;
  if (stimulation === "low") root.dataset.stimulation = "low";
  else delete root.dataset.stimulation;
  return snapshot ? ["aparencia.stimulation"] : [];
}
