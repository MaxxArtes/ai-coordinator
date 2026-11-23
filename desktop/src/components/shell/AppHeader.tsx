import React from "react";
import { Icon } from "../ui/Icon";
import { BrandMark } from "./BrandMark";

export type ConnectionStatus = "connecting" | "authorizing" | "connected" | "failed";

export interface RuntimeStatus {
  core_version: string;
  python_version?: string | null;
  app_version?: string;
  license?: string;
  host_os: string;
  host_arch?: string;
  is_isolated: boolean;
  python_path: string;
}

export type MetricProvenance = "measured" | "estimated" | "unknown";
export type ProcessRole = "host" | "core" | "webview" | "terminal" | "child";

export interface ProcessNodeMetrics {
  pid: number;
  ppid: number | null;
  start_time?: number;
  create_time_ns?: number | null;
  role: ProcessRole | string;
  name: string;
  ram_bytes: number | null;
  ram_provenance: MetricProvenance;
  cpu_usage_percent: number | null;
  cpu_provenance: MetricProvenance;
}

export interface CategoryResourceMetrics {
  role: ProcessRole | string;
  process_count: number;
  ram_bytes: number;
  ram_provenance: MetricProvenance;
  cpu_usage_percent: number | null;
  cpu_provenance: MetricProvenance;
}

export interface ProcessTreeCategories {
  host: CategoryResourceMetrics;
  core: CategoryResourceMetrics;
  webview: CategoryResourceMetrics;
  terminal: CategoryResourceMetrics;
  child: CategoryResourceMetrics;
}

export interface RuntimeMetrics {
  host_pid: number;
  core_pid?: number | null;
  host_ram_bytes: number;
  total_tree_ram_bytes: number;
  ram_provenance?: MetricProvenance;
  cpu_usage_percent?: number | null;
  cpu_provenance?: MetricProvenance;
  sample_interval_ms?: number | null;
  process_count?: number;
  sampled_at_unix_ms?: number;
  categories?: ProcessTreeCategories;
  nodes?: ProcessNodeMetrics[];
}

export interface AppHeaderProps {
  /** Real application version (package metadata); empty or invalid shows nothing. */
  appVersion: string;
  /** Name of the active profile: the display name saved in Perfil, else the profile name. */
  displayName?: string | null;
  isTauri: boolean;
  connectionStatus: ConnectionStatus;
  /** True only after a real updater_check answered "available". */
  hasUpdateAvailable: boolean;
  latestAvailableVersion: string;
  /** Opens Configurações (at Sobre when an update is available). */
  onOpenSettings: () => void;
  /** Main navigation (MainNav), in the middle of the header. */
  nav?: React.ReactNode;
}

export type ConnectionTone = "ok" | "info" | "warn" | "err";

export interface ConnectionLabel {
  text: string;
  tone: ConnectionTone;
}

const CONNECTION_LABEL: Record<ConnectionStatus, ConnectionLabel> = {
  connected: { text: "Core conectado", tone: "ok" },
  connecting: { text: "Conectando…", tone: "warn" },
  authorizing: { text: "Aguardando autorização", tone: "info" },
  failed: { text: "Falha", tone: "err" },
};

/** isTauri is false both before the host answers and in the browser preview; only "connecting" is ambiguous. */
export const isPreviewMode = (isTauri: boolean, status: ConnectionStatus): boolean => !isTauri && status !== "connecting";

/** The preview never claims a connection: it has no Core. */
export function connectionLabel(isTauri: boolean, status: ConnectionStatus): ConnectionLabel {
  return isPreviewMode(isTauri, status) ? { text: "Preview: sem Core", tone: "warn" } : CONNECTION_LABEL[status];
}

const VERSION_RE = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;

/** "v0.6.1-alpha.50" for a real version; null for empty or text such as "indisponível" (never a lone "v"). */
export function formatVersion(raw: string | null | undefined): string | null {
  const value = raw?.trim();
  return value && VERSION_RE.test(value) ? `v${value}` : null;
}

export const AppHeader: React.FC<AppHeaderProps> = ({
  appVersion,
  displayName,
  isTauri,
  connectionStatus,
  hasUpdateAvailable,
  latestAvailableVersion,
  onOpenSettings,
  nav,
}) => {
  const connection = connectionLabel(isTauri, connectionStatus);
  const version = formatVersion(appVersion);
  const latest = formatVersion(latestAvailableVersion);
  const gearName = hasUpdateAvailable ? `Configurações · atualização ${latest ?? "disponível"}` : "Configurações";

  return (
    <header className="app-header" data-f6-region="header">
      <div className="app-header__start">
        <div className="brand">
          <BrandMark />
          <h1 className="brand__title">AI Coordinator Desktop</h1>
        </div>
        {version && <span className="app-header__version">{version}</span>}
        <span className={`status-pill status-pill--${connection.tone}`}>
          <span className="status-pill__dot" aria-hidden="true" />
          {connection.text}
        </span>
      </div>

      {nav}

      <div className="app-header__end">
        {displayName && (
          <span className="app-header__operator" title={`Perfil: ${displayName}`}>
            <span className="sr-only">Perfil: </span>
            {displayName}
          </span>
        )}
        <button
          type="button"
          onClick={onOpenSettings}
          aria-label={gearName}
          className={hasUpdateAvailable ? "btn btn--sm btn--ghost btn--attention app-gear" : "btn btn--sm btn--ghost app-gear"}
          title={
            hasUpdateAvailable
              ? `Configurações: atualização ${latest ? `${latest} ` : ""}disponível em Sobre`
              : "Configurações do AI Coordinator Desktop"
          }
        >
          <Icon name="settings" size="sm" />
          {hasUpdateAvailable && <span className="app-gear__label">Atualização</span>}
        </button>
      </div>
    </header>
  );
};
