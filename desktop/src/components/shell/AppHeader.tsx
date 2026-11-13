import React from "react";
import { Icon } from "../ui/Icon";

export type ConnectionStatus = "connecting" | "authorizing" | "connected" | "failed";

export interface RuntimeStatus {
  core_version: string;
  host_os: string;
  is_isolated: boolean;
  python_path: string;
}

export interface RuntimeMetrics {
  host_pid: number;
  host_ram_bytes: number;
  total_tree_ram_bytes: number;
  cpu_usage_percent: number;
}

export interface AppHeaderProps {
  status: RuntimeStatus | null;
  appVersion: string;
  isTauri: boolean;
  connectionStatus: ConnectionStatus;
  metrics: RuntimeMetrics | null;
  hasUpdateAvailable: boolean;
  latestAvailableVersion: string;
  onOpenUpdates: () => void;
}

export const formatBytes = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

const CONNECTION_LABEL: Record<ConnectionStatus, { text: string; tone: string }> = {
  connected: { text: "Ativa", tone: "status--ok" },
  authorizing: { text: "Aguardando autorização", tone: "status--info" },
  connecting: { text: "Conectando…", tone: "status--warn" },
  failed: { text: "Falha", tone: "status--err" },
};

export const AppHeader: React.FC<AppHeaderProps> = ({
  status,
  appVersion,
  isTauri,
  connectionStatus,
  metrics,
  hasUpdateAvailable,
  latestAvailableVersion,
  onOpenUpdates,
}) => {
  const connection = CONNECTION_LABEL[connectionStatus];
  const pythonName = status?.python_path ? status.python_path.replace(/\\/g, "/").split("/").pop() : null;

  return (
    <header className="app-header">
      <div className="app-header__identity">
        <h1 className="app-header__title">AI Coordinator Desktop</h1>
        <span className="badge">Core v{status?.core_version || "0.6.0"}</span>
        <span className="badge badge--channel badge--mono">Alpha v{appVersion}</span>
        {isTauri ? (
          <span className="badge badge--ok">Tauri 2 + WebView2 (nativo)</span>
        ) : (
          <span className="badge badge--warn" title="Visualização provisória via Vite/navegador sem host Tauri">
            Vite Preview (não homologado)
          </span>
        )}
        <span className="status-text" role="status">
          Supervisão ConPTY: <strong className={connection.tone}>{connection.text}</strong>
        </span>
        {pythonName && (
          <span
            className="badge badge--mono badge--truncate"
            title={`Caminho efetivo do runtime Python no host: ${status!.python_path}`}
          >
            Python: {pythonName} ({status!.is_isolated ? "isolado" : "host"})
          </span>
        )}
      </div>

      <div className="app-header__meta">
        {metrics ? (
          <dl className="app-header__metrics" aria-label="Métricas do processo">
            <div>
              <dt>PID</dt>
              <dd>{metrics.host_pid}</dd>
            </div>
            <div>
              <dt>RAM host</dt>
              <dd>{formatBytes(metrics.host_ram_bytes)}</dd>
            </div>
            <div>
              <dt>RAM árvore</dt>
              <dd>{formatBytes(metrics.total_tree_ram_bytes)}</dd>
            </div>
            <div>
              <dt>CPU</dt>
              <dd>{metrics.cpu_usage_percent.toFixed(1)}%</dd>
            </div>
          </dl>
        ) : (
          <span className="status-text">Métricas em espera</span>
        )}

        <button
          type="button"
          onClick={onOpenUpdates}
          className={hasUpdateAvailable ? "btn btn--sm btn--attention" : "btn btn--sm"}
          title="Atualizações do AI Coordinator Desktop"
        >
          <Icon name={hasUpdateAvailable ? "update" : "refresh"} size="sm" />
          <span>{hasUpdateAvailable ? `Atualização v${latestAvailableVersion}` : "Atualizações"}</span>
        </button>
      </div>
    </header>
  );
};
