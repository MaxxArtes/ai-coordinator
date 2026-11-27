import React, { useEffect, useLayoutEffect, useMemo, useRef, useState, useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";
import { TerminalGrid } from "./components/TerminalGrid";
import { TerminalPaneData } from "./components/TerminalPane";
import { Terminal } from "xterm";
import { SettingsDialog } from "./components/settings/SettingsDialog";
import { SETTINGS_CATEGORIES, type SettingsCategoryId } from "./components/settings/categories";
import {
  applyInterfaceSettings,
  authorizeProject,
  errorText,
  getActiveProfile,
  getProvidersTelemetry,
  getSettings,
  invalidFieldsOf,
  pathWithin,
  rememberProject,
  samePath,
  settingsInUse,
  toStructuredError,
  type BackedCategory,
  type ProfileRecovery,
  type RevokeResult,
  type SettingsSnapshot,
} from "./components/settings/api";
import {
  AppHeader,
  connectionLabel,
  type ConnectionStatus,
  type RuntimeMetrics,
  type RuntimeStatus,
} from "./components/shell/AppHeader";
import { MainNav, mainViewPanelId, type MainViewId } from "./components/shell/MainNav";
import { MainViews } from "./components/shell/PlannedView";
import { MissionsView } from "./components/shell/MissionsView";
import { MonitoringView } from "./components/monitoring/MonitoringView";
import { appendHistorySample, isRepeatedReading, type HistorySample } from "./components/monitoring/history";
import {
  DEFAULT_PROVIDERS_TTL_MS,
  METRICS_DETAIL_INTERVAL_MS,
  METRICS_MAX_BACKOFF_MS,
  METRICS_SUMMARY_INTERVAL_MS,
  PROVIDERS_FALLBACK_INTERVAL_MS,
  providersRefreshDelayMs,
  usePolling,
} from "./components/monitoring/polling";
import type { ProvidersTelemetrySnapshot } from "./components/monitoring/telemetryContract";
import { StatusBar } from "./components/shell/StatusBar";
import { findEntry, findRegions, installGlobalShortcuts } from "./components/shell/shortcuts";
import { lowestFreeSlot, paneFromSession } from "./components/terminalIdentity";
import { createSerialSender, mergeWrites, useTerminalOutput } from "./components/terminalIo";
import { activeCountText, resolvePaneState, useTerminalStates, type PaneState } from "./components/useTerminalStates";
import { NoticeBanner, type NoticeTone } from "./components/shell/NoticeBanner";
import { AuthorizationScreen } from "./components/shell/AuthorizationScreen";
import { StartupStatus } from "./components/shell/StartupStatus";
import { SystemNotices, type SystemNotice } from "./components/shell/SystemNotices";

/**
 * Longest wait for the preferences before the terminal area is shown with the defaults. The rest of the shell is never
 * hidden: it paints at once with the appearance cached from the previous run (main.tsx).
 */
export const PREFERENCES_DEADLINE_MS = 1500;
export const DEFAULT_STARTUP_DIRECTORY = "C:\\Users\\10864";
export const DEFAULT_INTERPRETER_ID = "powershell";

const BACKED: BackedCategory[] = ["perfil", "geral", "aparencia", "terminal", "coordinator"];
const MAX_TAB_ID_ATTEMPTS = 16;

/** A typed path without the surrounding spaces and the quotes "Copiar como caminho" of the Explorer adds. */
const typedProjectPath = (text: string): string => {
  const trimmed = text.trim();
  return trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"') ? trimmed.slice(1, -1).trim() : trimmed;
};

/** The open project is still usable when it, or a directory above it, is authorized (same rule as the host). */
const stillAuthorized = (project: string, authorized: string[]): boolean => authorized.some((dir) => pathWithin(project, dir));

export const App: React.FC = () => {
  const [isTauri, setIsTauri] = useState<boolean>(false);
  const isTauriRef = useRef<boolean>(false);

  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("connecting");
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [metrics, setMetrics] = useState<RuntimeMetrics | null>(null);
  // The last metrics reading failed: the previous value stops being shown as a measurement.
  const [metricsFailed, setMetricsFailed] = useState<boolean>(false);
  const [metricsHistory, setMetricsHistory] = useState<HistorySample[]>([]);
  const [providersTelemetry, setProvidersTelemetry] = useState<ProvidersTelemetrySnapshot | null>(null);
  const [providersLoading, setProvidersLoading] = useState<boolean>(false);
  const [providersError, setProvidersError] = useState<string | null>(null);
  // One project tab per window (D5). A new id is used only when another project replaces a revoked one: the host
  // keeps a tab bound to the project it was created with.
  const [activeTab, setActiveTab] = useState<string>("tab-main");
  const activeTabRef = useRef<string>("tab-main");
  const tabSeqRef = useRef<number>(1);
  const tabProjectRef = useRef<Map<string, string>>(new Map());
  // The main view is separate from activeTab: switching views never re-runs the connection effect.
  const [mainView, setMainView] = useState<MainViewId>("conversas");
  const [projectPath, setProjectPath] = useState<string>("");
  const projectPathRef = useRef<string>("");
  projectPathRef.current = projectPath;
  const [authorizedProjects, setAuthorizedProjects] = useState<string[]>([]);
  const [isAuthorizing, setIsAuthorizing] = useState<boolean>(false);
  // Why the project screen is shown instead of a terminal; empty on a clean install.
  const [authNote, setAuthNote] = useState<string>("");
  // A directory the host refused as broad (L5), waiting for the operator's explicit answer (L7). The ref is read by
  // the handlers, so a double activation or a cancel in the same instant never sends a confirmation twice.
  const [broadScopePath, setBroadScopePathState] = useState<string | null>(null);
  const broadScopeRef = useRef<string | null>(null);
  const setBroadScopePath = useCallback((path: string | null) => {
    broadScopeRef.current = path;
    setBroadScopePathState(path);
  }, []);
  const [starting, setStarting] = useState<boolean>(false);
  const startingRef = useRef<boolean>(false);
  const [inputPath, setInputPath] = useState<string>("");
  const [panes, setPanes] = useState<TerminalPaneData[]>([]);
  const [interpreters, setInterpreters] = useState<{ id: string; label: string }[]>([]);
  const [switchingSessionIds, setSwitchingSessionIds] = useState<Set<string>>(new Set());
  const switchingSessionIdsRef = useRef<Set<string>>(new Set());
  const [notice, setNotice] = useState<string>("");
  const [noticeTone, setNoticeTone] = useState<NoticeTone>("error");
  const [isSettingsOpen, setIsSettingsOpen] = useState<boolean>(false);
  const [settingsCategory, setSettingsCategory] = useState<SettingsCategoryId>("perfil");
  const [hasUpdateAvailable, setHasUpdateAvailable] = useState<boolean>(false);
  const [latestAvailableVersion, setLatestAvailableVersion] = useState<string>("");
  // Read from the host (get_runtime_status / updater_get_channel_info); empty until known.
  const [appVersion, setAppVersion] = useState<string>("");
  const [settings, setSettings] = useState<SettingsSnapshot | null>(null);
  // settings_get failed at start: the defaults are in use and this stays visible until a load succeeds.
  const [settingsLoadError, setSettingsLoadError] = useState<string | null>(null);
  const [preferencesLoading, setPreferencesLoading] = useState(true);
  const [profileName, setProfileName] = useState<string>("");
  const [profileDisplayName, setProfileDisplayName] = useState<string>("");
  const [profileRecovery, setProfileRecovery] = useState<ProfileRecovery | null>(null);
  // Incremented by "Tentar novamente" after a failed start: the connection effect runs again.
  const [initAttempt, setInitAttempt] = useState<number>(0);
  // False when the Core closed the communication (L2): trying again in this window cannot help, only a restart.
  const [initRetryable, setInitRetryable] = useState<boolean>(true);
  const acceptSettings = useCallback((next: SettingsSnapshot) => {
    applyInterfaceSettings(next);
    setSettings(next);
    setSettingsLoadError(null);
  }, []);

  // Never a blank window: whatever happens to the preferences, the terminal area is shown after the deadline.
  useEffect(() => {
    const timer = window.setTimeout(() => setPreferencesLoading(false), PREFERENCES_DEADLINE_MS);
    return () => window.clearTimeout(timer);
  }, []);

  const termsRef = useRef<Map<string, Terminal>>(new Map());
  const lastSeqRef = useRef<Map<string, number>>(new Map());
  const createdTabsRef = useRef<Set<string>>(new Set());
  // Terminal names use a number that never repeats while the tab exists (F10): never the position on screen.
  const nextNumberRef = useRef<number>(1);
  const takeNumber = () => nextNumberRef.current++;
  const panesRef = useRef<TerminalPaneData[]>([]);
  panesRef.current = panes;
  // Where the keyboard focus goes after a pane is created or closed (never left on the page body).
  const focusAfterRef = useRef<{ sessionId?: string; strip?: boolean } | null>(null);
  // Terminals ended by `exit`: input is no longer sent to them.
  const endedRef = useRef<Set<string>>(new Set());

  // A connection change is announced once per change (never periodically), in a polite hidden status region.
  const connectionText = connectionLabel(isTauri, connectionStatus).text;
  const [connectionNotice, setConnectionNotice] = useState<string>("");
  const lastConnectionTextRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = lastConnectionTextRef.current;
    lastConnectionTextRef.current = connectionText;
    if (previous !== null && previous !== connectionText) setConnectionNotice(`Conexão: ${connectionText}`);
  }, [connectionText]);

  const openSettings = useCallback((category: SettingsCategoryId) => {
    setSettingsCategory(category);
    setIsSettingsOpen(true);
  }, []);

  // F6/Shift+F6 between regions, the application shortcuts and the F5/Ctrl+R guard: one listener on the window.
  const shortcutActionsRef = useRef({
    openSettings: () => openSettings("geral"),
    focusPane: (index: number) => {
      if (index >= panesRef.current.length) return;
      setMainView("conversas");
      requestAnimationFrame(() => document.querySelectorAll<HTMLElement>(".terminal-pane__focus-region")[index]?.focus());
    },
    cycleView: (direction: 1 | -1) => {
      const views: MainViewId[] = ["conversas", "monitoramento", "missoes"];
      setMainView((previous) => views[(views.indexOf(previous) + direction + views.length) % views.length]);
      requestAnimationFrame(() => document.querySelector<HTMLElement>('.main-nav [aria-selected="true"]')?.focus());
    },
  });
  useEffect(() => installGlobalShortcuts(window, shortcutActionsRef.current), []);

  // Direct invocation without swallowing errors
  const callTauri = useCallback(async (cmd: string, args?: Record<string, any>): Promise<any> => {
    return await invoke(cmd, args);
  }, []);

  // Application-level message with an explicit tone (errors by default).
  const showNotice = useCallback((message: string, tone: NoticeTone = "error") => {
    setNotice(message);
    setNoticeTone(tone);
  }, []);

  const ensureTabCreated = useCallback(
    async (tabId: string, path: string) => {
      if (createdTabsRef.current.has(tabId)) return;
      if (isTauriRef.current) {
        await callTauri("terminal_create_tab", { tabId, projectPath: path });
      }
      createdTabsRef.current.add(tabId);
      tabProjectRef.current.set(tabId, path);
    },
    [callTauri]
  );

  /**
   * Opens a terminal in an authorized project while preserving any live sessions and their directories.
   * Remembering the project for the next start runs after creation and never blocks it (K5).
   */
  const startProject = useCallback(
    async (dir: string, alive: () => boolean = () => true, interpreterId = DEFAULT_INTERPRETER_ID): Promise<void> => {
      const slot = lowestFreeSlot(panesRef.current);
      if (slot === null) {
        throw new Error("Limite máximo de 4 terminais simultâneos atingido. Feche um terminal antes de abrir outro projeto.");
      }

      const matchedEntry = Array.from(tabProjectRef.current.entries()).find(([, project]) => samePath(project, dir));
      let tabId = matchedEntry?.[0] ?? activeTabRef.current;
      const bound = tabProjectRef.current.get(tabId);
      if (bound !== undefined && !samePath(bound, dir)) {
        tabId = `tab-${++tabSeqRef.current}`;
      }

      // The Core may already hold this id bound to another directory (another window, a webview refresh): the next free
      // tab-N is used, as many times as needed. The taken id is never marked as created here, so it is not used later
      // without asking the Core again.
      for (let attempt = 1; ; attempt++) {
        try {
          await ensureTabCreated(tabId, dir);
          break;
        } catch (err) {
          const reason = toStructuredError(err).reason;
          const taken = reason === "tab_exists" || /already exists|tab_exists/i.test(errorText(err));
          if (!taken || attempt >= MAX_TAB_ID_ATTEMPTS) throw err;
          do {
            tabId = `tab-${++tabSeqRef.current}`;
          } while (createdTabsRef.current.has(tabId));
        }
      }

      const sess = await callTauri("terminal_create_session", {
        tabId,
        paneId: slot,
        kind: "human_terminal",
        interpreterId,
        cwd: dir,
        cols: 80,
        rows: 24,
      });
      if (!alive()) return;
      if (!sess || !sess.session_id) throw new Error("Sessão inicial não retornou session_id válido");
      const createdPane = paneFromSession(sess, takeNumber(), tabId, slot);
      focusAfterRef.current = { sessionId: createdPane.sessionId };
      activeTabRef.current = tabId;
      setActiveTab(tabId);
      setProjectPath(dir);
      setIsAuthorizing(false);
      setAuthNote("");
      setPanes((prev) => [...prev, createdPane]);
      setConnectionStatus("connected");
      void rememberProject(callTauri, dir).catch((err) => {
        if (alive()) showNotice(`O terminal abriu, mas o projeto não foi lembrado para a próxima inicialização: ${errorText(err)}`, "info");
      });
    },
    [callTauri, ensureTabCreated, showNotice]
  );

  const showProjectScreen = useCallback((note: string) => {
    setAuthNote(note);
    setIsAuthorizing(true);
    setConnectionStatus(panesRef.current.length > 0 ? "connected" : "authorizing");
  }, []);

  const handleOpenProjectScreen = useCallback(() => {
    setBroadScopePath(null);
    setAuthNote("");
    setInputPath("");
    setIsAuthorizing(true);
    if (panesRef.current.length === 0) setConnectionStatus("authorizing");
  }, [setBroadScopePath]);

  const handleCancelProjectScreen = useCallback(() => {
    if (startingRef.current) return;
    setBroadScopePath(null);
    setAuthNote("");
    setIsAuthorizing(false);
    if (panesRef.current.length > 0) {
      setConnectionStatus("connected");
      requestAnimationFrame(() => {
        const target =
          document.querySelector<HTMLElement>(".terminal-pane__focus-region") ??
          document.querySelector<HTMLElement>(".tab-strip [data-f6-entry]");
        target?.focus();
      });
    }
  }, [setBroadScopePath]);

  // Terminal mount callback
  const handleMount = useCallback((sessionId: string, term: Terminal | null) => {
    if (term) {
      termsRef.current.set(sessionId, term);
      if (!lastSeqRef.current.has(sessionId)) {
        lastSeqRef.current.set(sessionId, 0);
      }
      // Header information for interactive session
      term.writeln("\x1b[1;36m=== AI Coordinator Desktop ===\x1b[0m");
      term.writeln(`\x1b[90mSessão conectada: ${sessionId.slice(0, 8)} | Terminal Interativo\x1b[0m\r\n`);
    } else {
      termsRef.current.delete(sessionId);
      lastSeqRef.current.delete(sessionId);
    }
  }, []);

  // Keystrokes reach the host in the order they were typed: one terminal_write in flight per session, what is typed
  // meanwhile goes right after it in one piece (since L1 two invokes can reach the Core in either order).
  const [terminalWriter] = useState(() =>
    createSerialSender<string>({
      deliver: (sessionId, data) => callTauri("terminal_write", { sessionId, data }),
      merge: mergeWrites,
      keepQueuedOnError: false,
      onError: (_sessionId, err, dropped) =>
        showNotice(`Erro de escrita no terminal: ${errorText(err)}${dropped ? " O que foi digitado depois disso não foi enviado." : ""}`),
    })
  );
  // Only the last size matters: one terminal_resize in flight per session, the latest size right after it.
  const [terminalResizer] = useState(() =>
    createSerialSender<{ cols: number; rows: number }>({
      deliver: (sessionId, { cols, rows }) => callTauri("terminal_resize", { sessionId, cols, rows }),
      merge: (_queued, next) => next,
      keepQueuedOnError: true,
      // Resize errors can be safely ignored during pane disposal
    })
  );

  // Write keystroke to backend with zero stale closure bugs
  const handleWrite = useCallback(
    (sessionId: string, data: string) => {
      if (isTauriRef.current) {
        if (endedRef.current.has(sessionId)) return; // the process ended: nothing to write to
        terminalWriter.send(sessionId, data);
      } else {
        // Preview mode local echo & simulated response
        const term = termsRef.current.get(sessionId);
        if (term) {
          if (data === "\r") {
            term.write("\r\n");
            term.writeln("\x1b[33m[preview-pty]\x1b[0m Comando recebido (Host Tauri desconectado em modo web preview)");
            term.write("\x1b[1;32mai-coordinator:preview> \x1b[0m");
          } else if (data === "\u007f") {
            term.write("\b \b");
          } else {
            term.write(data);
          }
        }
      }
    },
    [terminalWriter]
  );

  // Resize handler with live ref
  const handleResize = useCallback(
    (sessionId: string, cols: number, rows: number) => {
      if (isTauriRef.current) terminalResizer.send(sessionId, { cols, rows });
    },
    [terminalResizer]
  );

  // Close pane handler with live ref
  const handleClosePane = useCallback(
    async (sessionId: string) => {
      if (isTauriRef.current) {
        try {
          const result = await callTauri("terminal_close_session", { sessionId });
          if (result?.closed !== true) throw new Error("O host não confirmou o fechamento");
        } catch (err) {
          // The pane stays: removing it would leave a process the interface no longer shows nor closes (F10).
          showNotice(`Erro ao fechar sessão: ${errorText(err)}. O terminal continua aberto.`);
          return;
        }
      }
      // The focus goes to the pane that takes this place, else the previous one, else the tab strip.
      const current = panesRef.current;
      const index = current.findIndex((p) => p.sessionId === sessionId);
      const remaining = current.filter((p) => p.sessionId !== sessionId);
      const neighbour = current[index + 1] ?? current[index - 1];
      focusAfterRef.current = neighbour ? { sessionId: neighbour.sessionId } : { strip: true };
      endedRef.current.delete(sessionId);
      setPanes(remaining);
      termsRef.current.delete(sessionId);
      lastSeqRef.current.delete(sessionId);
      if (remaining.length > 0 && !remaining.some((p) => p.tabId === activeTabRef.current)) {
        const nextPane = neighbour ?? remaining[0];
        const nextProject = nextPane.cwd ?? tabProjectRef.current.get(nextPane.tabId) ?? "";
        activeTabRef.current = nextPane.tabId;
        setActiveTab(nextPane.tabId);
        if (nextProject) setProjectPath(nextProject);
      }
    },
    [callTauri]
  );

  const handleChangeInterpreter = useCallback(async (sessionId: string, interpreterId: string) => {
    const current = panesRef.current.find((pane) => pane.sessionId === sessionId);
    if (!current || current.interpreterId === interpreterId || switchingSessionIdsRef.current.has(sessionId)) return;
    const selected = interpreters.find((item) => item.id === interpreterId);
    if (!selected || !window.confirm(`Trocar este terminal para ${selected.label}? A sessão atual será encerrada e somente este terminal será reiniciado.`)) return;

    switchingSessionIdsRef.current.add(sessionId);
    setSwitchingSessionIds((previous) => new Set(previous).add(sessionId));
    try {
      if (isTauriRef.current) {
        // Validate availability, path translation (including WSL), and authorization before closing the live session.
        await callTauri("terminal_validate_interpreter", {
          interpreterId,
          cwd: current.cwd ?? projectPath,
        });
        const result = await callTauri("terminal_close_session", { sessionId });
        if (result?.closed !== true) throw new Error("O host não confirmou o fechamento supervisionado");
      }
      endedRef.current.delete(sessionId);
      termsRef.current.delete(sessionId);
      lastSeqRef.current.delete(sessionId);
      setPanes((previous) => previous.filter((pane) => pane.sessionId !== sessionId));

      const next = await callTauri("terminal_create_session", {
        tabId: current.tabId,
        paneId: current.paneId,
        kind: "human_terminal",
        interpreterId,
        cwd: current.cwd ?? projectPath,
        cols: 80,
        rows: 24,
      });
      if (!next?.session_id) throw new Error("O host não retornou a nova sessão");
      const replacement = paneFromSession(next, takeNumber(), current.tabId, current.paneId);
      focusAfterRef.current = { sessionId: replacement.sessionId };
      setPanes((previous) => [...previous, replacement]);
    } catch (err) {
      showNotice(`Não foi possível trocar o interpretador: ${errorText(err)}`);
    } finally {
      switchingSessionIdsRef.current.delete(sessionId);
      setSwitchingSessionIds((previous) => {
        const next = new Set(previous);
        next.delete(sessionId);
        return next;
      });
    }
  }, [callTauri, interpreters, projectPath, showNotice]);

  // Add new pane (Defaults strictly to human_terminal)
  const handleAddPane = useCallback(
    async (tabId: string) => {
      // A free slot, never the number of panes: a pane ended by `exit` keeps its slot until closed (F10).
      const slot = lowestFreeSlot(panes);
      if (slot === null) {
        showNotice("Limite máximo de 4 painéis simultâneos por aba atingido.", "info");
        return;
      }
      const kind = "human_terminal";

      try {
        let newPane: TerminalPaneData;

        if (isTauriRef.current) {
          await ensureTabCreated(tabId, projectPath);
          const sess = await callTauri("terminal_create_session", {
            tabId,
            paneId: slot,
            kind,
            cwd: projectPath,
            cols: 80,
            rows: 24,
          });

          if (!sess || !sess.session_id) {
            throw new Error("Backend não retornou session_id válido");
          }
          newPane = paneFromSession(sess, takeNumber(), tabId, slot, kind);
        } else {
          newPane = { sessionId: `preview-sess-${Date.now().toString(36)}-${slot}`, tabId, paneId: slot, kind, number: takeNumber() };
        }

        focusAfterRef.current = { sessionId: newPane.sessionId };
        setPanes((prev) => [...prev, newPane]);
      } catch (err) {
        showNotice(`Falha ao criar terminal: ${errorText(err)}`);
      }
    },
    [panes, projectPath, ensureTabCreated, callTauri]
  );

  // One project open at a time: a second click while one is opening is ignored.
  const beginStart = (): boolean => {
    if (startingRef.current) return false;
    startingRef.current = true;
    setStarting(true);
    setNotice("");
    return true;
  };
  const endStart = () => {
    startingRef.current = false;
    setStarting(false);
  };

  // The authorized list as the host has it now (after an answer that never came, the request may have gone through).
  const refreshAuthorizedProjects = useCallback(async () => {
    try {
      const profile = await getActiveProfile(callTauri);
      if (Array.isArray(profile?.authorized_projects)) setAuthorizedProjects(profile.authorized_projects);
    } catch {
      // The notice already tells the operator to check before repeating.
    }
  }, [callTauri]);

  // Authorize project directory and start session. `confirmBroad` only carries the operator's explicit yes to the
  // broad-directory question (L7); it is never sent on a first request.
  const handleAuthorizeAndStart = useCallback(
    async (dirToAuthorize: string, confirmBroad = false) => {
      const requested = typedProjectPath(dirToAuthorize);
      if (!requested) {
        showNotice("Informe um caminho de diretório válido.");
        return;
      }
      // An open question is answered first (Cancelar or the explicit yes), never bypassed by another request.
      if (broadScopeRef.current !== null) return;
      if (!beginStart()) return;
      try {
        if (!isTauriRef.current) {
          // Preview mode fallback
          setAuthorizedProjects([requested]);
          setProjectPath(requested);
          setIsAuthorizing(false);
          setConnectionStatus("connected");
          return;
        }
        let canonical: string;
        try {
          const res = await authorizeProject(callTauri, requested, confirmBroad);
          if (!res || res.success !== true || typeof res.project_path !== "string") throw new Error("Resposta inesperada do autorizador.");
          canonical = res.project_path;
          if (Array.isArray(res.authorized_projects)) setAuthorizedProjects(res.authorized_projects);
        } catch (err) {
          const error = toStructuredError(err);
          if (error.reason === "broad_scope" && !confirmBroad) {
            // Nothing was authorized: the question itself says why (no error banner) and waits for an answer.
            setBroadScopePath(requested);
            return;
          }
          if (error.reason === "timeout") {
            // The host may have authorized it after all: the list is re-read, so the chooser shows what is true.
            showNotice(`A autorização do projeto não foi confirmada: ${errorText(error)}`);
            void refreshAuthorizedProjects();
            return;
          }
          showNotice(`Falha na autorização do projeto: ${errorText(error)}`);
          return;
        }
        setIsAuthorizing(false);
        if (panesRef.current.length === 0) setConnectionStatus("connecting");
        try {
          await startProject(canonical);
        } catch (err) {
          // The authorization itself went through: it is not reported as a failed authorization.
          showNotice(`Diretório autorizado, mas o terminal não abriu: ${errorText(err)}`);
          showProjectScreen("");
        }
      } finally {
        endStart();
      }
    },
    [callTauri, showNotice, startProject, showProjectScreen, setBroadScopePath, refreshAuthorizedProjects]
  );

  // The explicit yes to the broad-directory question: the same request again, now with confirmBroad (L7).
  const handleConfirmBroadScope = useCallback(() => {
    const path = broadScopeRef.current;
    if (path === null || startingRef.current) return;
    setBroadScopePath(null);
    void handleAuthorizeAndStart(path, true);
  }, [handleAuthorizeAndStart, setBroadScopePath]);

  const handleCancelBroadScope = useCallback(() => {
    if (startingRef.current) return;
    setBroadScopePath(null);
  }, [setBroadScopePath]);

  // Open a project that is already authorized, without authorizing it again.
  const handleOpenAuthorized = useCallback(
    async (path: string) => {
      if (broadScopeRef.current !== null) return; // the open question is answered first
      if (panesRef.current.length > 0 && lowestFreeSlot(panesRef.current) === null) {
        showNotice("Limite máximo de 4 terminais simultâneos atingido. Feche um terminal antes de abrir outro projeto.", "info");
        return;
      }
      if (!beginStart()) return;
      setIsAuthorizing(false);
      if (panesRef.current.length === 0) setConnectionStatus("connecting");
      try {
        if (!isTauriRef.current) {
          const slot = lowestFreeSlot(panesRef.current);
          if (slot === null) {
            showNotice("Limite máximo de 4 terminais simultâneos atingido. Feche um terminal antes de abrir outro projeto.", "info");
            return;
          }
          const previewPane: TerminalPaneData = {
            sessionId: `preview-sess-${Date.now().toString(36)}-${slot}`,
            tabId: activeTabRef.current,
            paneId: slot,
            kind: "human_terminal",
            number: takeNumber(),
            cwd: path,
            interpreterId: DEFAULT_INTERPRETER_ID,
            interpreterLabel: "PowerShell 7",
          };
          focusAfterRef.current = { sessionId: previewPane.sessionId };
          setProjectPath(path);
          setPanes((prev) => [...prev, previewPane]);
          setConnectionStatus("connected");
          return;
        }
        await startProject(path);
      } catch (err) {
        showNotice(`Não foi possível abrir ${path}: ${errorText(err)}`);
        if (panesRef.current.length === 0) showProjectScreen("");
      } finally {
        endStart();
      }
    },
    [showNotice, startProject, showProjectScreen]
  );

  // A directory was revoked in Configurações → Segurança. When it was the open project and no terminal is left there
  // (the host refuses otherwise), the App goes back to the project screen instead of using a revoked directory.
  const handleAuthorizationChanged = useCallback(
    (result: RevokeResult) => {
      const list = Array.isArray(result?.authorized_projects) ? result.authorized_projects : [];
      setAuthorizedProjects(list);
      const open = projectPathRef.current;
      if (open && !stillAuthorized(open, list)) {
        const surviving = panesRef.current.find((p) => p.cwd && stillAuthorized(p.cwd, list));
        if (surviving && surviving.cwd) {
          activeTabRef.current = surviving.tabId;
          setActiveTab(surviving.tabId);
          setProjectPath(surviving.cwd);
        } else if (panesRef.current.length === 0) {
          setProjectPath("");
          showProjectScreen(`A autorização de ${open} foi revogada. Escolha outro projeto ou autorize um diretório.`);
        }
      }
    },
    [showProjectScreen]
  );

  // Initialize runtime connection and check project authorization
  useEffect(() => {
    let mounted = true;
    const alive = () => mounted;

    async function init() {
      setConnectionStatus("connecting");
      setInitRetryable(true);
      try {
        let pingRes: string | null = null;
        try {
          pingRes = await callTauri("ping");
        } catch {
          pingRes = null;
        }

        if (!mounted) return;

        if (pingRes === "pong") {
          isTauriRef.current = true;
          setIsTauri(true);

          void callTauri("terminal_list_interpreters")
            .then((shellResult) => {
              if (mounted && Array.isArray(shellResult?.interpreters)) setInterpreters(shellResult.interpreters);
            })
            .catch(() => {
              // Listing options is best-effort; terminal creation still validates the requested shell in the Core.
            });

          const st = await callTauri("get_runtime_status");
          if (mounted && st) setStatus(st);
          if (mounted && st?.app_version) setAppVersion(st.app_version);

          // Preferences before the first terminal. A failure never blocks the start (E2-6): the defaults are used,
          // nothing is overwritten and the reason stays visible.
          let initialSettings: SettingsSnapshot | null = null;
          try {
            initialSettings = await getSettings(callTauri);
          } catch (err) {
            if (!mounted) return;
            applyInterfaceSettings(null);
            setSettingsLoadError(errorText(err));
          }
          if (!mounted) return;
          if (initialSettings) acceptSettings(initialSettings);
          setPreferencesLoading(false);

          // Query active profile and authorized projects
          const profInfo = await getActiveProfile(callTauri);
          if (!mounted) return;
          const authList: string[] = Array.isArray(profInfo?.authorized_projects) ? profInfo.authorized_projects : [];
          setAuthorizedProjects(authList);
          setProfileName(typeof profInfo?.name === "string" ? profInfo.name : "");
          setProfileDisplayName(typeof profInfo?.display_name === "string" ? profInfo.display_name : "");
          setProfileRecovery(profInfo?.recovery && typeof profInfo.recovery === "object" ? profInfo.recovery : null);

          // Protected settings (another version): the default is in use, as the host says.
          const autoOpen = settingsInUse(initialSettings)?.categories?.geral?.values?.open_last_project_on_start !== false;
          if (stillAuthorized(DEFAULT_STARTUP_DIRECTORY, authList) && autoOpen) {
            try {
              await startProject(DEFAULT_STARTUP_DIRECTORY, alive, DEFAULT_INTERPRETER_ID);
            } catch (err) {
              if (!mounted) return;
              showProjectScreen(`Não foi possível abrir ${DEFAULT_STARTUP_DIRECTORY}: ${errorText(err)}`);
            }
          } else {
            setInputPath(DEFAULT_STARTUP_DIRECTORY);
            showProjectScreen(!autoOpen
              ? "A abertura automática está desligada (Configurações → Geral)."
              : `Autorize explicitamente ${DEFAULT_STARTUP_DIRECTORY} para iniciar o terminal PowerShell automaticamente.`);
          }
        } else {
          // Preview mode in standard browser
          setPreferencesLoading(false);
          isTauriRef.current = false;
          setIsTauri(false);
          setStatus({
            core_version: "0.6.0",
            host_os: "windows",
            is_isolated: true,
            python_path: "desktop/runtime/python_spec.json (preview)",
          });
          const mockDir = "D:\\ai-coordinator";
          setAuthorizedProjects([mockDir]);
          setProjectPath(mockDir);
          const mockSessionId = `preview-sess-0`;
          setPanes([
            {
              sessionId: mockSessionId,
              tabId: activeTabRef.current,
              paneId: 0,
              kind: "human_terminal",
              number: takeNumber(),
            },
          ]);
          setConnectionStatus("connected");
        }
      } catch (err) {
        if (!mounted) return;
        setPreferencesLoading(false);
        setConnectionStatus("failed");
        setInitRetryable(toStructuredError(err).reason !== "disconnected");
        showNotice(`Erro de inicialização da Alpha: ${errorText(err)}`);
      }
    }

    init();

    return () => {
      mounted = false;
    };
  }, [callTauri, initAttempt]);

  // Metrics (Bloco 5): every 2 s with the system counters and the process list while Monitoramento is visible; every
  // 5 s, tree CPU and RAM only, when just the status bar shows them. One reading at a time, none while the window is
  // hidden, a longer wait after failures (usePolling). The first reading keeps the 2 s CPU window after connecting.
  const metricsDetail = mainView === "monitoramento";
  const metricsDetailRef = useRef(metricsDetail);
  metricsDetailRef.current = metricsDetail;
  const readMetrics = useCallback(async (): Promise<boolean> => {
    const detail = metricsDetailRef.current;
    try {
      const m = await callTauri("get_process_tree_metrics", { includeSystem: detail, includeNodes: detail });
      if (m) {
        setMetrics(m);
        setMetricsFailed(false);
        setMetricsHistory((prev) => (isRepeatedReading(prev, m) ? prev : appendHistorySample(prev, m, false)));
        return true;
      }
    } catch {
      // Telemetry errors are non-fatal, but the last value is no longer a measurement.
    }
    setMetricsFailed(true);
    setMetricsHistory((prev) => appendHistorySample(prev, null, true));
    return false;
  }, [callTauri]);
  const metricsPolling = usePolling({
    enabled: isTauri,
    intervalMs: metricsDetail ? METRICS_DETAIL_INTERVAL_MS : METRICS_SUMMARY_INTERVAL_MS,
    initialDelayMs: METRICS_DETAIL_INTERVAL_MS,
    maxBackoffMs: METRICS_MAX_BACKOFF_MS,
    read: readMetrics,
  });
  // Opening Monitoramento reads the full set at once instead of showing the status bar's reading until the next tick.
  useEffect(() => {
    if (metricsDetail) metricsPolling.runNow();
  }, [metricsDetail, metricsPolling]);

  // Provider telemetry: only while Monitoramento is visible and connected, at the pace of its source (the local
  // reader's cache, else the snapshot TTL), never on the metrics tick.
  const readProvidersTelemetry = useCallback(async (): Promise<number | false> => {
    if (!isTauriRef.current) return false;
    setProvidersLoading(true);
    try {
      const snap = await getProvidersTelemetry(callTauri);
      if (snap && Array.isArray(snap.providers)) {
        setProvidersTelemetry(snap);
        setProvidersError(null);
        return providersRefreshDelayMs(snap, Date.now());
      }
      setProvidersError("Resposta de provedores indisponível.");
      return false;
    } catch (err) {
      setProvidersError(errorText(err));
      return false;
    } finally {
      setProvidersLoading(false);
    }
  }, [callTauri]);
  const providersPolling = usePolling({
    enabled: isTauri && connectionStatus === "connected" && mainView === "monitoramento",
    intervalMs: PROVIDERS_FALLBACK_INTERVAL_MS,
    maxBackoffMs: DEFAULT_PROVIDERS_TTL_MS,
    read: readProvidersTelemetry,
  });

  // Output polling: never two readings at once, each answer written only for the position it was asked from (no
  // repeated output), a pause after a timeout, and a stop when the Core closed the communication.
  const handleOutputDisconnected = useCallback(
    (err: unknown) => {
      setInitRetryable(false);
      setConnectionStatus("failed");
      showNotice(`Os terminais pararam de atualizar: ${errorText(err)}`);
    },
    [showNotice]
  );
  useTerminalOutput({
    enabled: isTauri,
    callTauri,
    sessionIds: panes.map((p) => p.sessionId),
    terms: termsRef,
    lastSeq: lastSeqRef,
    onDisconnected: handleOutputDisconnected,
  });

  // Silent background check for updates upon connection
  useEffect(() => {
    if (isTauri && connectionStatus === "connected") {
      callTauri("updater_get_channel_info")
        .then((info: any) => {
          if (info?.current_version) setAppVersion(info.current_version);
        })
        .catch(() => {});

      callTauri("updater_check")
        .then((res: any) => {
          if (res && res.available) {
            setHasUpdateAvailable(true);
            setLatestAvailableVersion(res.latest_version);
          }
        })
        .catch(() => {
          // Gracefully ignore network/offline issues
        });
    }
  }, [isTauri, connectionStatus, callTauri]);


  // Live state of every terminal (F9): only what the host reports; unknown otherwise.
  const stateSnapshot = useTerminalStates(isTauri, callTauri, activeTab, panes.map((p) => p.sessionId));
  const paneStates = useMemo<Record<string, PaneState>>(
    () => Object.fromEntries(panes.map((p) => [p.sessionId, resolvePaneState(isTauri, stateSnapshot, p.sessionId, p.startedAlive)])),
    [panes, isTauri, stateSnapshot]
  );
  endedRef.current = new Set(Object.keys(paneStates).filter((id) => paneStates[id].kind === "ended"));
  const activeText = panes.length > 0 ? activeCountText(Object.values(paneStates)) : null;

  // After a pane is created or closed, the focus lands on a pane region or on the tab strip, never on the body.
  useEffect(() => {
    const target = focusAfterRef.current;
    if (!target) return;
    focusAfterRef.current = null;
    const el = target.sessionId
      ? Array.from(document.querySelectorAll<HTMLElement>("[data-session-id]"))
          .find((pane) => pane.getAttribute("data-session-id") === target.sessionId)
          ?.querySelector<HTMLElement>(".terminal-pane__focus-region")
      : document.querySelector<HTMLElement>(".tab-strip [data-f6-entry]");
    el?.focus();
  }, [panes]);

  // Closing Configurações gives the focus back to the control that opened it (Dialog). When that control is gone (the
  // open project was revoked there and the tab strip went with the terminal grid), the focus goes to the entry of the
  // visible view, never to the page body (4D.3.5). The Dialog's cleanup, which lifts inert, has already run here.
  const settingsWasOpenRef = useRef(false);
  useEffect(() => {
    const wasOpen = settingsWasOpenRef.current;
    settingsWasOpenRef.current = isSettingsOpen;
    if (isSettingsOpen || !wasOpen) return;
    if (document.activeElement && document.activeElement !== document.body) return;
    const view = document.getElementById(mainViewPanelId(mainView));
    const inView = !view ? [] : view.matches("[data-f6-region]") ? [view] : findRegions(view);
    // Else (a view without an entry, e.g. the startup screen) the first region of the window, as F6 from the body.
    [...inView, ...findRegions()].map(findEntry).find(Boolean)?.focus();
  }, [isSettingsOpen]);

  // Persistent state the operator must see; never replaced by a transient notice.
  const systemNotices = useMemo<SystemNotice[]>(() => {
    const list: SystemNotice[] = [];
    if (profileRecovery) {
      list.push({
        id: "profile-recovery",
        tone: "warn",
        title: "Perfil ilegível recriado: sem projetos autorizados e sem configurações",
        body: (
          <>
            {profileRecovery.message || "O perfil ativo não pôde ser lido e foi recriado."} Arquivo da cópia, na
            pasta do perfil: <code>{profileRecovery.backup_file}</code>
          </>
        ),
      });
    }
    const toSettings = { label: "Abrir Configurações", onClick: () => openSettings("perfil") };
    if (settingsLoadError) {
      list.push({
        id: "settings-load",
        tone: "error",
        title: "Preferências não carregadas: usando os padrões",
        body: `${settingsLoadError} Nada foi sobrescrito.`,
        action: toSettings,
      });
    } else if (settings?.read_only) {
      list.push({
        id: "settings-read-only",
        tone: "warn",
        title: "Configurações de outra versão: protegidas, somente leitura",
        body: `${settings.stored_issue?.message || "As configurações foram gravadas por outra versão e estão protegidas."} Esta versão usa os próprios padrões e não grava sobre elas.`,
        action: toSettings,
      });
    } else if (settings) {
      const invalid = BACKED.map((category) => [category, invalidFieldsOf(settings, category).length] as const).filter(([, n]) => n > 0);
      if (invalid.length > 0 || settings.stored_issue) {
        const labels = invalid.map(([category, n]) => {
          const label = SETTINGS_CATEGORIES.find((c) => c.id === category)?.label ?? category;
          return `${label} (${n} ${n === 1 ? "campo" : "campos"})`;
        });
        list.push({
          id: "settings-invalid",
          tone: "warn",
          title: "Preferências salvas inválidas: usando o padrão nesses campos",
          body: [
            settings.stored_issue?.message || "Os valores salvos ficam preservados.",
            labels.length ? `Em: ${labels.join(", ")}.` : "",
          ]
            .filter(Boolean)
            .join(" "),
          action: toSettings,
        });
      }
    }
    return list;
  }, [profileRecovery, settingsLoadError, settings, openSettings]);

  const isAuthorizingView = isAuthorizing || connectionStatus === "authorizing";
  const showTerminals =
    !isAuthorizingView && !(connectionStatus === "connecting" && panes.length === 0) && !(connectionStatus === "failed" && panes.length === 0);
  const terminalHostRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = terminalHostRef.current;
    if (!el) return;
    if (showTerminals) el.removeAttribute("inert");
    else el.setAttribute("inert", "");
  }, [showTerminals]);

  // Conversas shows the project and startup screens too; existing terminal panes stay mounted and inert when choosing another project.
  const conversasContent = (
    <>
      {isAuthorizingView ? (
        <AuthorizationScreen
          profileName={profileName || "perfil ativo"}
          authorizedProjects={authorizedProjects}
          note={authNote}
          inputPath={inputPath}
          onInputPathChange={setInputPath}
          onSubmit={(path) => void handleAuthorizeAndStart(path)}
          onOpenProject={handleOpenAuthorized}
          onCancel={panes.length > 0 ? handleCancelProjectScreen : undefined}
          busy={starting}
          broadScopePath={broadScopePath}
          onConfirmBroadScope={handleConfirmBroadScope}
          onCancelBroadScope={handleCancelBroadScope}
        />
      ) : connectionStatus === "connecting" && panes.length === 0 ? (
        <StartupStatus state="connecting" />
      ) : connectionStatus === "failed" && panes.length === 0 ? (
        <StartupStatus
          state="failed"
          detail={notice}
          onRetry={
            initRetryable
              ? () => {
                  setNotice("");
                  setInitAttempt((n) => n + 1);
                }
              : undefined
          }
        />
      ) : null}
      {(showTerminals || panes.length > 0) && (
        <div
          ref={terminalHostRef}
          hidden={!showTerminals}
          style={{ display: showTerminals ? "contents" : "none" }}
        >
          {/* No "Salvar sessões": the host only builds the sanitized history in memory and nothing writes it to a file yet
          (A5); the button would claim a save the operator cannot get. */}
          <TerminalGrid
            tabId={activeTab}
            projectPath={projectPath}
            panes={panes}
            states={paneStates}
            authorizedProjects={authorizedProjects}
            onOpenAuthorizedProject={(dir) => void handleOpenAuthorized(dir)}
            openingProject={starting}
            interpreters={interpreters}
            switchingSessionIds={switchingSessionIds}
            onChangeInterpreter={handleChangeInterpreter}
            onAddPane={handleAddPane}
            onOpenProjectScreen={handleOpenProjectScreen}
            onWrite={handleWrite}
            onResize={handleResize}
            onClose={handleClosePane}
            onMount={handleMount}
          />
        </div>
      )}
    </>
  );

  const headerName = settingsInUse(settings)?.categories?.perfil?.values?.display_name || profileDisplayName || profileName || null;

  return (
    <div className="app" data-preferences-loading={preferencesLoading || undefined}>
      <AppHeader
        appVersion={appVersion}
        displayName={headerName}
        isTauri={isTauri}
        connectionStatus={connectionStatus}
        hasUpdateAvailable={hasUpdateAvailable}
        latestAvailableVersion={latestAvailableVersion}
        onOpenSettings={() => openSettings(hasUpdateAvailable ? "sobre" : "perfil")}
        nav={<MainNav active={mainView} onSelect={setMainView} paneCount={panes.length} />}
      />
      <p className="sr-only" role="status" data-testid="connection-notice">
        {connectionNotice}
      </p>

      <SystemNotices notices={systemNotices} />
      {notice && <NoticeBanner message={notice} tone={noticeTone} onDismiss={() => setNotice("")} />}

      {/* The three views share one cell; Conversas stays mounted (invisible and inert while another view is shown). */}
      <MainViews
        active={mainView}
        conversas={conversasContent}
        missoes={
          <MissionsView
            key={projectPath}
            active={mainView === "missoes"}
            projectPath={projectPath}
            invoke={isTauri && connectionStatus === "connected" ? callTauri : undefined}
          />
        }
        monitoramento={
          <MonitoringView
            active={mainView === "monitoramento"}
            isTauri={isTauri}
            connectionStatus={connectionStatus}
            metrics={metrics}
            metricsUnavailable={metricsFailed}
            history={metricsHistory}
            panes={panes}
            paneStates={paneStates}
            settings={settings}
            providersTelemetry={providersTelemetry}
            providersLoading={providersLoading}
            providersError={providersError}
            onRefreshProviders={providersPolling.runNow}
          />
        }
      />

      <StatusBar
        isTauri={isTauri}
        connectionStatus={connectionStatus}
        coreVersion={status?.core_version}
        paneCount={panes.length}
        activeText={activeText}
        metrics={metrics}
        metricsUnavailable={metricsFailed}
        projectPath={projectPath}
      >
        <span className="status-bar__item status-bar__item--hint">
          <kbd className="kbd">F6</kbd> alternar regiões
        </span>
      </StatusBar>

      {/* Unified settings (updates live in Configurações → Sobre) */}
      <SettingsDialog
        open={isSettingsOpen}
        onClose={() => setIsSettingsOpen(false)}
        initialCategory={settingsCategory}
        isTauri={isTauri}
        runtimeStatus={status}
        connectionStatus={connectionStatus}
        activePanesCount={panes.length}
        metrics={metricsFailed ? null : metrics}
        callTauri={callTauri}
        onSettingsChanged={acceptSettings}
        onAuthorizationChanged={handleAuthorizationChanged}
      />
    </div>
  );
};
