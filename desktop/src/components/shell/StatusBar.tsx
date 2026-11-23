import React from "react";
import { formatVersion, isPreviewMode, type ConnectionStatus, type RuntimeMetrics } from "./AppHeader";

/**
 * Status bar (Etapa 5, decision 6): honest values for each state.
 * - RAM of the process tree as measured by the host, "aguardando" before the first reading, "indisponível (preview)"
 *   without a host. CPU is always "indisponível" until a reliable reading exists (4D.4): its field is never read here.
 * - No live region: the values change every 2 s and must not be announced.
 */
export const MAX_TERMINALS = 4;

export interface StatusBarProps {
  isTauri: boolean;
  connectionStatus: ConnectionStatus;
  coreVersion?: string | null;
  paneCount: number;
  /** "(3 ativos)" from the live states of the terminals; omitted when unknown to the caller. */
  activeText?: string | null;
  metrics: RuntimeMetrics | null;
  /** The last metrics reading failed: the previous value is no longer a measurement. */
  metricsUnavailable?: boolean;
  projectPath: string;
  /** Extra items at the end of the bar (the F6 hint). */
  children?: React.ReactNode;
}

const STATE: Record<ConnectionStatus, string> = {
  connected: "conectado",
  connecting: "conectando…",
  authorizing: "aguardando autorização",
  failed: "falha",
};

const MIB = 1024 * 1024;
const ONE_DECIMAL = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

/**
 * Base 1024 with KB, MB and GB, as the Windows Task Manager shows them; null when not a measurement (the host reports
 * 0 when it cannot find the process).
 */
export function formatBytesPtBR(bytes: number): string | null {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) return null;
  if (bytes >= 1024 * MIB) return `${ONE_DECIMAL.format(bytes / (1024 * MIB))} GB`;
  if (bytes >= MIB) return `${ONE_DECIMAL.format(bytes / MIB)} MB`;
  return `${ONE_DECIMAL.format(bytes / 1024)} KB`;
}

export function coreStatusText(isTauri: boolean, status: ConnectionStatus, coreVersion?: string | null): string {
  if (isPreviewMode(isTauri, status)) return "Preview: sem Core";
  const version = formatVersion(coreVersion);
  const core = version ? `Core ${version}` : coreVersion?.trim() ? "Core (versão indisponível)" : "Core";
  return `${core} · ${STATE[status]}`;
}

export function treeRamText(
  isTauri: boolean,
  status: ConnectionStatus,
  metrics: RuntimeMetrics | null,
  unavailable = false
): string {
  if (isPreviewMode(isTauri, status)) return "indisponível (preview)";
  if (unavailable) return "indisponível";
  if (!metrics) return "aguardando";
  const value = formatBytesPtBR(metrics.total_tree_ram_bytes);
  return value ? `${value} (medida)` : "indisponível";
}

export function treeCpuText(
  isTauri: boolean,
  status: ConnectionStatus,
  metrics: RuntimeMetrics | null,
  unavailable = false
): string {
  if (isPreviewMode(isTauri, status)) return "indisponível (preview)";
  if (unavailable) return "indisponível";
  if (!metrics) return "aguardando";
  const prov = metrics.cpu_provenance;
  if (prov !== "measured" && prov !== "estimated") return "indisponível";
  const value = metrics.cpu_usage_percent;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "indisponível";
  const label = prov === "measured" ? "medida" : "estimada";
  return `${ONE_DECIMAL.format(value)}% (${label})`;
}

/** Splits a path before its last segment, so that the ellipsis falls in the middle and the folder name stays. */
export function splitProjectPath(path: string): { head: string; tail: string } {
  const trimmed = path.replace(/[\\/]+$/, "");
  const i = Math.max(trimmed.lastIndexOf("\\"), trimmed.lastIndexOf("/"));
  return i < 0 ? { head: "", tail: trimmed } : { head: trimmed.slice(0, i + 1), tail: trimmed.slice(i + 1) };
}

const Item: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <span className="status-bar__item">
    <span className="status-bar__label">{label}</span> <span className="status-bar__value">{value}</span>
  </span>
);

export const StatusBar: React.FC<StatusBarProps> = ({
  isTauri,
  connectionStatus,
  coreVersion,
  paneCount,
  activeText,
  metrics,
  metricsUnavailable = false,
  projectPath,
  children,
}) => {
  const preview = isPreviewMode(isTauri, connectionStatus);
  const { head, tail } = splitProjectPath(projectPath);
  return (
    <footer className="status-bar" aria-label="Barra de status">
      <div className="status-bar__group">
        <span className="status-bar__item">{coreStatusText(isTauri, connectionStatus, coreVersion)}</span>
        <Item
          label="Terminais:"
          value={`${paneCount} de ${MAX_TERMINALS}${preview ? " (preview)" : activeText ? ` ${activeText}` : ""}`}
        />
        <Item label="RAM da árvore:" value={treeRamText(isTauri, connectionStatus, metrics, metricsUnavailable)} />
        <Item label="CPU:" value={treeCpuText(isTauri, connectionStatus, metrics, metricsUnavailable)} />
      </div>
      <div className="status-bar__group status-bar__group--end">
        {projectPath && (
          <span className="status-bar__item status-bar__path" title={projectPath}>
            <span className="status-bar__path-head" aria-hidden="true">
              {head}
            </span>
            <span className="status-bar__path-tail" aria-hidden="true">
              {tail}
            </span>
            <span className="sr-only">Projeto: {projectPath}</span>
          </span>
        )}
        {children}
      </div>
    </footer>
  );
};
