/**
 * Development-only visual check (Marco 4D.3, fundação visual).
 * Served by the Vite dev server at /visual-check.html?view=<view>[&stimulation=low]; never bundled in the app.
 * Renders each shell state with fixed fixtures so screenshots are deterministic.
 */
import React from "react";
import ReactDOM from "react-dom/client";
import "../index.css";
import { App } from "../App";
import { AppHeader, type AppHeaderProps } from "../components/shell/AppHeader";
import { AuthorizationScreen } from "../components/shell/AuthorizationScreen";
import { NoticeBanner } from "../components/shell/NoticeBanner";
import { StartupStatus } from "../components/shell/StartupStatus";
import { UpdateModal, type UpdateCheckResult } from "../components/UpdateModal";

const params = new URLSearchParams(window.location.search);
const view = params.get("view") ?? "app-preview";
if (params.get("stimulation") === "low") document.documentElement.dataset.stimulation = "low";

const HEADER: AppHeaderProps = {
  status: { core_version: "0.6.0", host_os: "windows", is_isolated: true, python_path: "C:\\App\\runtime\\python\\python.exe" },
  appVersion: "0.6.1-alpha.50",
  isTauri: true,
  connectionStatus: "connected",
  metrics: { host_pid: 4242, host_ram_bytes: 61_000_000, total_tree_ram_bytes: 128_000_000, cpu_usage_percent: 1.4 },
  hasUpdateAvailable: false,
  latestAvailableVersion: "",
  onOpenUpdates: () => undefined,
};

const CHECK_BASE: UpdateCheckResult = {
  available: true,
  current_version: "0.6.1-alpha.50",
  latest_version: "0.6.1-alpha.51",
  notes: "Correções do backend ConPTY e recuperação segura do atualizador.",
  date: "2026-11-13T12:00:00Z",
  active_sessions_count: 2,
  sessions_known: true,
  offline: false,
  error: null,
  runtime_stopped: false,
};

function fakeTauri(check: UpdateCheckResult) {
  return async (cmd: string) => {
    if (cmd === "updater_get_channel_info") return { current_version: "0.6.1-alpha.50", channel: "alpha" };
    if (cmd === "updater_check") return check;
    throw new Error(`comando indisponível na conferência visual: ${cmd}`);
  };
}

const Shell: React.FC<{ header?: Partial<AppHeaderProps>; children: React.ReactNode }> = ({ header, children }) => (
  <div className="app">
    <AppHeader {...HEADER} {...header} />
    {children}
  </div>
);

const UpdateView: React.FC<{ check: UpdateCheckResult; panes?: number }> = ({ check, panes = 2 }) => (
  <Shell header={{ hasUpdateAvailable: check.available, latestAvailableVersion: check.latest_version }}>
    <main className="app__main">
      <StartupStatus state="connecting" />
    </main>
    <UpdateModal isOpen onClose={() => undefined} activePanesCount={panes} isTauri callTauri={fakeTauri(check)} />
  </Shell>
);

const VIEWS: Record<string, () => React.ReactElement> = {
  "app-preview": () => <App />,
  auth: () => (
    <Shell header={{ connectionStatus: "authorizing", metrics: null }}>
      <main className="app__main">
        <AuthorizationScreen profileName="default" inputPath="" onInputPathChange={() => undefined} onSubmit={() => undefined} />
      </main>
    </Shell>
  ),
  "startup-failed": () => (
    <Shell header={{ connectionStatus: "failed", metrics: null }}>
      <NoticeBanner
        message="Erro de inicialização da Alpha: Fail-closed: runtime Python privado não encontrado."
        tone="error"
        onDismiss={() => undefined}
      />
      <main className="app__main">
        <StartupStatus state="failed" detail="Fail-closed: runtime Python privado não encontrado." />
      </main>
    </Shell>
  ),
  notices: () => (
    <Shell header={{ connectionStatus: "connecting" }}>
      <NoticeBanner message="Falha ao salvar sessão: o Core recusou a exportação." tone="error" onDismiss={() => undefined} />
      <NoticeBanner message="Sessão salva com sucesso: 2 terminais arquivados." tone="success" onDismiss={() => undefined} />
      <NoticeBanner message="Limite máximo de 4 painéis simultâneos por aba atingido." tone="info" onDismiss={() => undefined} />
      <main className="app__main">
        <StartupStatus state="connecting" />
      </main>
    </Shell>
  ),
  "update-available": () => <UpdateView check={CHECK_BASE} />,
  "update-runtime-stopped": () => (
    <UpdateView check={{ ...CHECK_BASE, available: false, runtime_stopped: true, sessions_known: false, sessions_error: "IPC child stdin is not available" }} />
  ),
  "update-error": () => (
    <UpdateView
      check={{
        ...CHECK_BASE,
        available: false,
        offline: true,
        error: "Servidor de atualização inacessível (modo offline): tempo esgotado.",
      }}
    />
  ),
};

const render = VIEWS[view] ?? VIEWS["app-preview"];
ReactDOM.createRoot(document.getElementById("root")!).render(render());
