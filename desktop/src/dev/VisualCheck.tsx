/**
 * Development-only visual check (Marco 4D.3, fundação visual).
 * Served by the Vite dev server at /visual-check.html?view=<view>[&stimulation=low]; never bundled in the app.
 * Renders each shell state with fixed fixtures so screenshots are deterministic.
 * Every fixture view carries the FIXTURE band: its version, connection, metrics and update are not real.
 */
import React, { useState } from "react";
import ReactDOM from "react-dom/client";
import "../index.css";
import { App } from "../App";
import {
  AppHeader,
  type AppHeaderProps,
  type ConnectionStatus,
  type RuntimeMetrics,
  type RuntimeStatus,
} from "../components/shell/AppHeader";
import { MainNav, type MainViewId } from "../components/shell/MainNav";
import { MainViews } from "../components/shell/PlannedView";
import { StatusBar } from "../components/shell/StatusBar";
import { TerminalGrid } from "../components/TerminalGrid";
import type { TerminalPaneData } from "../components/TerminalPane";
import type { PaneState } from "../components/useTerminalStates";
import { PaneIndicators } from "../components/indicators/PaneIndicators";
import { INDICATOR_KINDS, type IndicatorReading } from "../components/indicators/readings";
import { AuthorizationScreen } from "../components/shell/AuthorizationScreen";
import { NoticeBanner } from "../components/shell/NoticeBanner";
import { SystemNotices } from "../components/shell/SystemNotices";
import { StartupStatus } from "../components/shell/StartupStatus";
import { SettingsDialog } from "../components/settings/SettingsDialog";
import type { SettingsCategoryId } from "../components/settings/categories";
import type { UpdateCheckResult } from "../components/settings/UpdatesSection";
import {
  applyInterfaceSettings,
  type AgentsSnapshot,
  type RoutesSnapshot,
  type SecurityStatus,
  type SettingsSnapshot,
  type VaultStatus,
} from "../components/settings/api";

const params = new URLSearchParams(window.location.search);
const view = params.get("view") ?? "app-preview";
if (params.get("stimulation") === "low") document.documentElement.dataset.stimulation = "low";

const RUNTIME: RuntimeStatus = {
  core_version: "0.6.0",
  python_version: "3.12.8",
  app_version: "0.6.1-alpha.50",
  license: "PolyForm-Noncommercial-1.0.0",
  host_os: "windows",
  host_arch: "x86_64",
  is_isolated: true,
  python_path: "C:\\App\\runtime\\python\\python.exe",
};

const FIXTURE_METRICS: RuntimeMetrics = { host_pid: 4242, host_ram_bytes: 61_000_000, total_tree_ram_bytes: 128_000_000 };
const PROJECT = "D:\\ai-coordinator";

const HEADER: AppHeaderProps = {
  appVersion: "0.6.1-alpha.50",
  isTauri: true,
  connectionStatus: "connected",
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

function snapshot(overrides: Record<string, any> = {}, top: Partial<SettingsSnapshot> = {}): SettingsSnapshot {
  const categories: any = {
    perfil: { values: { display_name: null }, saved_fields: [], validated: true, applied: false, application: "interface", invalid_fields: [] },
    geral: { values: { open_last_project_on_start: true }, saved_fields: [], validated: true, applied: false, application: "interface", invalid_fields: [] },
    aparencia: { values: { stimulation: "standard", density: "comfortable" }, saved_fields: [], validated: true, applied: false, application: "interface", invalid_fields: [] },
    terminal: { values: { font_size: 13, cursor_blink: true, scrollback_lines: 1000 }, saved_fields: [], validated: true, applied: false, application: "interface", invalid_fields: [] },
    coordinator: {
      values: { flight_budget: { max_steps: 20, max_elapsed_seconds: 1800, max_uncheckpointed_steps: 5, max_worker_calls: 10 } },
      saved_fields: ["flight_budget"],
      validated: true,
      applied: false,
      application: "unsupported",
    },
  };
  for (const [k, v] of Object.entries(overrides)) categories[k] = { ...categories[k], ...v };
  return { schema_version: 1, stored_schema_version: 1, read_only: false, stored_issue: null, unsupported_categories: {}, categories, ...top };
}

/** Fixture shapes of the host's discovery, routes and security answers (desktop/ipc/discovery.py, server.py). */
const AGENTS: AgentsSnapshot = {
  agents: [
    { name: "Codex", client: "codex", available: true, reason: "Launcher encontrado; autenticação e cota não verificadas." },
    { name: "Claude Code", client: "claude", available: false, reason: "Launcher indisponível ou configuração ilegível." },
  ],
  mcp_servers: ["exemplo-fixture"],
  mcp_complete: true,
  source: "configured",
  unsupported: [{ name: "Gemini CLI", reason: "Exige um novo contrato de cliente no Core." }],
};
const ROUTES: RoutesSnapshot = {
  routes: [{ category: "fixture", candidates: [{ client: "codex", model: "modelo-fixture", effort: "medium" }] }],
  available: true,
  source: "packaged_defaults",
  quota_status: "unknown",
  quota_milestone: "4D.4.3",
};
const SECURITY: SecurityStatus = {
  authorized_projects: [`${PROJECT}-fixture`],
  policy: {
    project_fencing: true,
    human_terminal: "operator_permissions",
    human_terminal_filesystem_isolation: false,
    description: "FIXTURE: a autorização limita a abertura de projetos; o terminal usa as permissões do operador.",
  },
};

interface Fakes {
  settings?: SettingsSnapshot;
  vault?: VaultStatus;
  vaultKeys?: string[];
  agents?: AgentsSnapshot;
  routes?: RoutesSnapshot;
  security?: SecurityStatus;
}

function fakeTauri(check: UpdateCheckResult, fakes: Fakes = {}) {
  const security = { ...(fakes.security ?? SECURITY) };
  return async (cmd: string, args?: Record<string, any>) => {
    if (cmd === "updater_get_channel_info") return { current_version: "0.6.1-alpha.50", channel: "alpha" };
    if (cmd === "updater_check") return check;
    if (cmd === "settings_get") return fakes.settings ?? snapshot();
    if (cmd === "vault_status") return fakes.vault ?? { available: false, backend: "unavailable", reason: "unavailable" };
    if (cmd === "vault_list_keys") return { keys: fakes.vaultKeys ?? [], revision: "f".repeat(64) };
    if (cmd === "discovery_agents") return fakes.agents ?? AGENTS;
    if (cmd === "providers_routes") return fakes.routes ?? ROUTES;
    if (cmd === "security_status") return security;
    if (cmd === "profile_revoke_project") {
      // Fixture only: the list shrinks on screen; nothing on disk is touched.
      security.authorized_projects = security.authorized_projects.filter((p) => p !== args?.projectPath);
      return { revoked: true, authorized_projects: security.authorized_projects, last_project: null };
    }
    throw new Error(`comando indisponível na conferência visual: ${cmd}`);
  };
}

/** A full-width band (not a badge): everything below it is fixture data. */
const FixtureBanner: React.FC = () => (
  <p className="fixture-banner" role="note">
    FIXTURE: dados fictícios para conferência visual
  </p>
);

interface ShellProps {
  connection?: ConnectionStatus;
  update?: string;
  panes?: number;
  metrics?: RuntimeMetrics | null;
  view?: MainViewId;
  notices?: React.ReactNode;
  children: React.ReactNode;
}

const Shell: React.FC<ShellProps> = ({
  connection = "connected",
  update,
  panes = 0,
  metrics = FIXTURE_METRICS,
  view = "conversas",
  notices,
  children,
}) => {
  const [active, setActive] = useState<MainViewId>(view);
  return (
    <div className="app">
      <FixtureBanner />
      <AppHeader
        {...HEADER}
        connectionStatus={connection}
        hasUpdateAvailable={!!update}
        latestAvailableVersion={update ?? ""}
        nav={<MainNav active={active} onSelect={setActive} paneCount={panes} />}
      />
      {notices}
      <MainViews active={active} conversas={children} />
      <StatusBar
        isTauri
        connectionStatus={connection}
        coreVersion={RUNTIME.core_version}
        paneCount={panes}
        metrics={metrics}
        projectPath={PROJECT}
      />
    </div>
  );
};

const SettingsView: React.FC<{ check: UpdateCheckResult; category?: SettingsCategoryId; panes?: number; fakes?: Fakes }> = ({
  check,
  category = "sobre",
  panes = 2,
  fakes,
}) => (
  <Shell update={check.available ? check.latest_version : undefined} panes={panes}>
    <StartupStatus state="connecting" />
    <SettingsDialog
      open
      onClose={() => undefined}
      initialCategory={category}
      isTauri
      runtimeStatus={RUNTIME}
      connectionStatus="connected"
      activePanesCount={panes}
      metrics={FIXTURE_METRICS}
      callTauri={fakeTauri(check, fakes)}
      onSettingsChanged={(s) => applyInterfaceSettings(s)}
    />
  </Shell>
);

const FOUR_PANES: TerminalPaneData[] = [0, 1, 2, 3].map((i) => ({
  sessionId: `fixture-${i + 1}`,
  tabId: "tab-main",
  paneId: i,
  kind: "human_terminal",
  number: [1, 2, 5, 6][i],
  program: "cmd.exe",
  cwd: PROJECT,
  pid: 4100 + i,
}));

/** Fixture states, one of each kind, so every header variant can be compared. */
const FIXTURE_STATES: Record<string, PaneState> = {
  "fixture-1": { kind: "alive" },
  "fixture-2": { kind: "alive" },
  "fixture-3": { kind: "ended", exitCode: 0 },
  "fixture-4": { kind: "unknown" },
};

const noop = () => undefined;

const ConversasView: React.FC<{ view: MainViewId }> = ({ view: initial }) => (
  <Shell view={initial} panes={4}>
    <TerminalGrid
      tabId="tab-main"
      projectPath={PROJECT}
      panes={FOUR_PANES}
      states={FIXTURE_STATES}
      onAddPane={noop}
      onSaveSessions={noop}
      onWrite={noop}
      onResize={noop}
      onClose={noop}
      onMount={(_, term) => term?.writeln("FIXTURE: terminal fictício, sem sessão real")}
    />
  </Shell>
);

/** Values for this page only, always marked test_fixture. */
const IndicatorFixtures: React.FC<{ open?: boolean }> = ({ open = false }) => {
  const readings: IndicatorReading[] = INDICATOR_KINDS.map((kind, i) => ({
    kind, provenance: i === 1 ? "estimated" : "measured", origin: "test_fixture",
    percent: [18, 65, 91][i], source: "conferência visual fictícia", asOf: "2026-11-17T12:00:00Z",
  }));
  return (
    <Shell panes={1}>
      <div className="terminal-grid">
        <div className="terminal-grid__panes terminal-grid__panes--1">
          <div className="terminal-pane">
            <div className="terminal-pane__header">
              <span className="terminal-pane__title">01 · Terminal (fixture)</span>
              <PaneIndicators terminalName="Terminal 01" readings={readings} initiallyOpen={open} />
            </div>
          </div>
        </div>
      </div>
    </Shell>
  );
};

const VIEWS: Record<string, () => React.ReactElement> = {
  "app-preview": () => <App />,
  "conversas-4": () => <ConversasView view="conversas" />,
  "indicadores-fixture": () => <IndicatorFixtures />,
  "indicadores-popover": () => <IndicatorFixtures open />,
  monitoramento: () => <ConversasView view="monitoramento" />,
  missoes: () => <ConversasView view="missoes" />,
  auth: () => (
    <Shell connection="authorizing" metrics={null}>
      <AuthorizationScreen profileName="default" inputPath="" onInputPathChange={() => undefined} onSubmit={() => undefined} />
    </Shell>
  ),
  "startup-failed": () => (
    <Shell
      connection="failed"
      metrics={null}
      notices={
        <NoticeBanner
          message="Erro de inicialização da Alpha: Fail-closed: runtime Python privado não encontrado."
          tone="error"
          onDismiss={() => undefined}
        />
      }
    >
      <StartupStatus state="failed" detail="Fail-closed: runtime Python privado não encontrado." />
    </Shell>
  ),
  notices: () => (
    <Shell
      connection="connecting"
      metrics={null}
      notices={
        <>
          <NoticeBanner message="Falha ao salvar sessão: o Core recusou a exportação." tone="error" onDismiss={() => undefined} />
          <NoticeBanner message="Sessão salva com sucesso: 2 terminais arquivados." tone="success" onDismiss={() => undefined} />
          <NoticeBanner message="Limite máximo de 4 painéis simultâneos por aba atingido." tone="info" onDismiss={() => undefined} />
        </>
      }
    >
      <StartupStatus state="connecting" />
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
  "e7-provedores-cofre-ocupado": () => (
    <SettingsView check={CHECK_BASE} category="provedores" fakes={{ vault: { available: false, backend: "dpapi", reason: "busy" } }} />
  ),
  "e7-rotas-ilegiveis": () => (
    <SettingsView
      check={CHECK_BASE}
      category="provedores"
      fakes={{
        routes: {
          routes: [],
          available: false,
          source: "unreadable",
          reason: "Configuração de rotas ilegível; nada foi alterado.",
          quota_status: "unknown",
          quota_milestone: "4D.4.3",
        },
      }}
    />
  ),
  "e7-agentes": () => <SettingsView check={CHECK_BASE} category="agentes" />,
  "e7-agentes-mcp-incompleto": () => (
    <SettingsView check={CHECK_BASE} category="agentes" fakes={{ agents: { ...AGENTS, mcp_servers: [], mcp_complete: false, source: "unreadable" } }} />
  ),
  "e7-seguranca": () => <SettingsView check={CHECK_BASE} category="seguranca" />,
  "e7-configuracoes-protegidas": () => (
    <SettingsView
      check={CHECK_BASE}
      category="terminal"
      fakes={{
        settings: snapshot(
          {},
          {
            read_only: true,
            stored_issue: { reason: "incompatible_version", message: "O esquema salvo não é compatível com esta versão. Os dados foram preservados." },
          },
        ),
      }}
    />
  ),
  "e7-campos-invalidos": () => (
    <SettingsView
      check={CHECK_BASE}
      category="terminal"
      fakes={{ settings: snapshot({ terminal: { saved_fields: ["font_size"], validated: false, invalid_fields: ["font_size"] } }) }}
    />
  ),
  "e7-escolha-de-projeto": () => (
    <Shell connection="authorizing" metrics={null}>
      <AuthorizationScreen
        profileName="default"
        authorizedProjects={[PROJECT, `${PROJECT}-fixture`]}
        note="A abertura automática do último projeto está desligada (Configurações → Geral)."
        inputPath=""
        onInputPathChange={() => undefined}
        onSubmit={() => undefined}
        onOpenProject={() => undefined}
      />
    </Shell>
  ),
  "e7-avisos-persistentes": () => (
    <Shell
      connection="connected"
      notices={
        <SystemNotices
          notices={[
            {
              id: "profile-recovery",
              tone: "warn",
              title: "Perfil ilegível recriado: sem projetos autorizados e sem configurações",
              body: (
                <>
                  FIXTURE: o perfil ativo não pôde ser lido. Cópia preservada no diretório do perfil:{" "}
                  <code>profile.json.corrupt-20261117T120000Z</code>
                </>
              ),
            },
            {
              id: "settings-load",
              tone: "error",
              title: "Preferências não carregadas: usando os padrões",
              body: "FIXTURE: o esquema salvo não é compatível com esta versão. Nada foi sobrescrito.",
              action: { label: "Abrir Configurações", onClick: () => undefined },
            },
          ]}
        />
      }
    >
      <StartupStatus state="connecting" />
    </Shell>
  ),
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
