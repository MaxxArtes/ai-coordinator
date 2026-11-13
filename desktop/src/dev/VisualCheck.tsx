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
import { SettingsDialog } from "../components/settings/SettingsDialog";
import type { SettingsCategoryId } from "../components/settings/categories";
import type { UpdateCheckResult } from "../components/settings/UpdatesSection";
import { applyInterfaceSettings, type SettingsSnapshot, type VaultStatus } from "../components/settings/api";

const params = new URLSearchParams(window.location.search);
const view = params.get("view") ?? "app-preview";
if (params.get("stimulation") === "low") document.documentElement.dataset.stimulation = "low";

const HEADER: AppHeaderProps = {
  status: {
    core_version: "0.6.0",
    python_version: "3.12.8",
    app_version: "0.6.1-alpha.50",
    license: "PolyForm-Noncommercial-1.0.0",
    host_os: "windows",
    host_arch: "x86_64",
    is_isolated: true,
    python_path: "C:\\App\\runtime\\python\\python.exe",
  },
  appVersion: "0.6.1-alpha.50",
  isTauri: true,
  connectionStatus: "connected",
  metrics: { host_pid: 4242, host_ram_bytes: 61_000_000, total_tree_ram_bytes: 128_000_000, cpu_usage_percent: 1.4 },
  hasUpdateAvailable: false,
  latestAvailableVersion: "",
  onOpenSettings: () => undefined,
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

function snapshot(overrides: Record<string, any> = {}): SettingsSnapshot {
  const categories: any = {
    perfil: { values: { display_name: null }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
    geral: { values: { open_last_project_on_start: true }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
    aparencia: { values: { stimulation: "standard", density: "comfortable" }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
    terminal: { values: { font_size: 13, cursor_blink: true, scrollback_lines: 1000 }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
    coordinator: {
      values: { flight_budget: { max_steps: 20, max_elapsed_seconds: 1800, max_uncheckpointed_steps: 5, max_worker_calls: 10 } },
      saved_fields: ["flight_budget"],
      validated: true,
      applied: false,
      application: "unsupported",
    },
  };
  for (const [k, v] of Object.entries(overrides)) categories[k] = { ...categories[k], ...v };
  return { schema_version: 1, stored_schema_version: 1, unsupported_categories: {}, categories };
}

interface Fakes {
  settings?: SettingsSnapshot;
  vault?: VaultStatus;
  vaultKeys?: string[];
}

function fakeTauri(check: UpdateCheckResult, fakes: Fakes = {}) {
  return async (cmd: string) => {
    if (cmd === "updater_get_channel_info") return { current_version: "0.6.1-alpha.50", channel: "alpha" };
    if (cmd === "updater_check") return check;
    if (cmd === "settings_get") return fakes.settings ?? snapshot();
    if (cmd === "vault_status") return fakes.vault ?? { available: false, backend: "unavailable" };
    if (cmd === "vault_list_keys") return { keys: fakes.vaultKeys ?? [] };
    throw new Error(`comando indisponível na conferência visual: ${cmd}`);
  };
}

const Shell: React.FC<{ header?: Partial<AppHeaderProps>; children: React.ReactNode }> = ({ header, children }) => (
  <div className="app">
    <AppHeader {...HEADER} {...header} />
    {children}
  </div>
);

const SettingsView: React.FC<{ check: UpdateCheckResult; category?: SettingsCategoryId; panes?: number; fakes?: Fakes }> = ({
  check,
  category = "sobre",
  panes = 2,
  fakes,
}) => (
  <Shell header={{ hasUpdateAvailable: check.available, latestAvailableVersion: check.latest_version }}>
    <main className="app__main">
      <StartupStatus state="connecting" />
    </main>
    <SettingsDialog
      open
      onClose={() => undefined}
      initialCategory={category}
      isTauri
      runtimeStatus={HEADER.status}
      connectionStatus="connected"
      activePanesCount={panes}
      callTauri={fakeTauri(check, fakes)}
      onSettingsChanged={(s) => applyInterfaceSettings(s)}
    />
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
  "settings-perfil": () => <SettingsView check={CHECK_BASE} category="perfil" />,
  "settings-provedores": () => <SettingsView check={CHECK_BASE} category="provedores" />,
  "update-available": () => <SettingsView check={CHECK_BASE} />,
  "e4-terminal": () => <SettingsView check={CHECK_BASE} category="terminal" />,
  "e4-aparencia-aplicada": () => (
    <SettingsView
      check={CHECK_BASE}
      category="aparencia"
      fakes={{ settings: snapshot({ aparencia: { values: { stimulation: "low", density: "comfortable" }, saved_fields: ["density", "stimulation"] } }) }}
    />
  ),
  "e4-coordinator": () => <SettingsView check={CHECK_BASE} category="coordinator" />,
  "e4-provedores-dpapi": () => (
    <SettingsView check={CHECK_BASE} category="provedores" fakes={{ vault: { available: true, backend: "dpapi" }, vaultKeys: ["OPENROUTER_API_KEY"] }} />
  ),
  "e4-provedores-indisponivel": () => <SettingsView check={CHECK_BASE} category="provedores" />,
  "update-runtime-stopped": () => (
    <SettingsView check={{ ...CHECK_BASE, available: false, runtime_stopped: true, sessions_known: false, sessions_error: "IPC child stdin is not available" }} />
  ),
  "update-error": () => (
    <SettingsView
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
