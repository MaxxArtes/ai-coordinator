import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
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

const BACKED: BackedCategory[] = ["perfil", "geral", "aparencia", "terminal", "coordinator"];

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
    const { invoke } = await import("@tauri-apps/api/core");
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
   * Opens a terminal in an authorized project. Remembering the project for the next start is a convenience that runs
   * after the terminal exists and never blocks it (K5); a failure there is only reported.
   */
  const startProject = useCallback(
    async (dir: string, alive: () => boolean = () => true): Promise<void> => {
      let tabId = activeTabRef.current;
      const bound = tabProjectRef.current.get(tabId);
      if (bound !== undefined && !samePath(bound, dir)) tabId = `tab-${++tabSeqRef.current}`;
      await ensureTabCreated(tabId, dir);
      const sess = await callTauri("terminal_create_session", {
        tabId,
        paneId: 0,
        kind: "human_terminal",
        cwd: dir,
        cols: 80,
        rows: 24,
      });
      if (!alive()) return;
      if (!sess || !sess.session_id) throw new Error("Sessão inicial não retornou session_id válido");
      activeTabRef.current = tabId;
      setActiveTab(tabId);
      setProjectPath(dir);
      setIsAuthorizing(false);
      setAuthNote("");
      setPanes([paneFromSession(sess, takeNumber(), tabId, 0)]);
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
    setConnectionStatus("authorizing");
  }, []);

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
      const neighbour = current[index + 1] ?? current[index - 1];
      focusAfterRef.current = neighbour ? { sessionId: neighbour.sessionId } : { strip: true };
      endedRef.current.delete(sessionId);
      setPanes((prev) => prev.filter((p) => p.sessionId !== sessionId));
      termsRef.current.delete(sessionId);
      lastSeqRef.current.delete(sessionId);
    },
    [callTauri]
  );

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
      const requested = dirToAuthorize.trim();
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
        setConnectionStatus("connecting");
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
      if (!beginStart()) return;
      setIsAuthorizing(false);
      setConnectionStatus("connecting");
      try {
        await startProject(path);
      } catch (err) {
        showNotice(`Não foi possível abrir ${path}: ${errorText(err)}`);
        showProjectScreen("");
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
      if (open && !stillAuthorized(open, list) && panesRef.current.length === 0) {
        setProjectPath("");
        showProjectScreen(`A autorização de ${open} foi revogada. Escolha outro projeto ou autorize um diretório.`);
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
          if (authList.length > 0 && autoOpen) {
            const activeDir = authList.find((p) => samePath(p, profInfo.last_project)) ?? authList[0];
            try {
              await startProject(activeDir, alive);
            } catch (err) {
              if (!mounted) return;
              // The project cannot be opened (removed, unplugged…): the chooser, not a dead end.
              showProjectScreen(`Não foi possível abrir ${activeDir}: ${errorText(err)}`);
            }
          } else {
            showProjectScreen(
              authList.length > 0 ? "A abertura automática do último projeto está desligada (Configurações → Geral)." : ""
            );
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

  // Periodic metrics refresh
  useEffect(() => {
    if (!isTauri) return;
    const interval = setInterval(async () => {
      try {
        const m = await callTauri("get_process_tree_metrics");
        if (m) {
          setMetrics(m);
          setMetricsFailed(false);
        } else {
          setMetricsFailed(true);
        }
      } catch {
        // Telemetry errors are non-fatal, but the last value is no longer a measurement.
        setMetricsFailed(true);
      }
    }, 2000);
    return () => clearInterval(interval);
  }, [isTauri, callTauri]);

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

  // Conversas shows the project and startup screens too; the navigation stays available on all of them.
  const conversasContent = isAuthorizingView ? (
    <AuthorizationScreen
      profileName={profileName || "perfil ativo"}
      authorizedProjects={authorizedProjects}
      note={authNote}
      inputPath={inputPath}
      onInputPathChange={setInputPath}
      onSubmit={(path) => void handleAuthorizeAndStart(path)}
      onOpenProject={handleOpenAuthorized}
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
  ) : (
    // No "Salvar sessões": the host only builds the sanitized history in memory and nothing writes it to a file yet
    // (A5); the button would claim a save the operator cannot get.
    <TerminalGrid
      tabId={activeTab}
      projectPath={projectPath}
      panes={panes}
      states={paneStates}
      onAddPane={handleAddPane}
      onWrite={handleWrite}
      onResize={handleResize}
      onClose={handleClosePane}
      onMount={handleMount}
    />
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
      <MainViews active={mainView} conversas={conversasContent} />

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
        <span className="status-bar__item">
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
