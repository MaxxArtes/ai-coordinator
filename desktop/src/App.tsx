import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import { TerminalGrid } from "./components/TerminalGrid";
import { TerminalPaneData } from "./components/TerminalPane";
import { Terminal } from "xterm";
import { SettingsDialog } from "./components/settings/SettingsDialog";
import type { SettingsCategoryId } from "./components/settings/categories";
import { applyInterfaceSettings, getSettings } from "./components/settings/api";
import {
  AppHeader,
  connectionLabel,
  type ConnectionStatus,
  type RuntimeMetrics,
  type RuntimeStatus,
} from "./components/shell/AppHeader";
import { MainNav, type MainViewId } from "./components/shell/MainNav";
import { MainViews } from "./components/shell/PlannedView";
import { StatusBar } from "./components/shell/StatusBar";
import { installGlobalShortcuts } from "./components/shell/shortcuts";
import { lowestFreeSlot, paneFromSession } from "./components/terminalIdentity";
import { activeCountText, resolvePaneState, useTerminalStates, type PaneState } from "./components/useTerminalStates";
import { NoticeBanner, type NoticeTone } from "./components/shell/NoticeBanner";
import { AuthorizationScreen } from "./components/shell/AuthorizationScreen";
import { StartupStatus } from "./components/shell/StartupStatus";

export const App: React.FC = () => {
  const [isTauri, setIsTauri] = useState<boolean>(false);
  const isTauriRef = useRef<boolean>(false);

  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("connecting");
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [metrics, setMetrics] = useState<RuntimeMetrics | null>(null);
  // The last metrics reading failed: the previous value stops being shown as a measurement.
  const [metricsFailed, setMetricsFailed] = useState<boolean>(false);
  const [activeTab, setActiveTab] = useState<string>("tab-main");
  // The main view is separate from activeTab: switching views never re-runs the connection effect, which depends
  // on activeTab and would recreate the tab and its sessions.
  const [mainView, setMainView] = useState<MainViewId>("conversas");
  const [projectPath, setProjectPath] = useState<string>("");
  const [authorizedProjects, setAuthorizedProjects] = useState<string[]>([]);
  const [isAuthorizing, setIsAuthorizing] = useState<boolean>(false);
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

  // F6/Shift+F6 between regions and the F5/Ctrl+R/Ctrl+Shift+R guard: a single listener on the window.
  useEffect(() => installGlobalShortcuts(window), []);

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
    },
    [callTauri]
  );

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

  // Write keystroke to backend with zero stale closure bugs
  const handleWrite = useCallback(
    async (sessionId: string, data: string) => {
      if (isTauriRef.current) {
        if (endedRef.current.has(sessionId)) return; // the process ended: nothing to write to
        try {
          await callTauri("terminal_write", { sessionId, data });
        } catch (err: any) {
          showNotice(`Erro de escrita no terminal: ${err?.message || err}`);
        }
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
    [callTauri]
  );

  // Resize handler with live ref
  const handleResize = useCallback(
    async (sessionId: string, cols: number, rows: number) => {
      if (isTauriRef.current) {
        try {
          await callTauri("terminal_resize", { sessionId, cols, rows });
        } catch {
          // Resize errors can be safely ignored during pane disposal
        }
      }
    },
    [callTauri]
  );

  // Close pane handler with live ref
  const handleClosePane = useCallback(
    async (sessionId: string) => {
      if (isTauriRef.current) {
        try {
          const result = await callTauri("terminal_close_session", { sessionId });
          if (result?.closed !== true) throw new Error("O host não confirmou o fechamento");
        } catch (err: any) {
          // The pane stays: removing it would leave a process the interface no longer shows nor closes (F10).
          showNotice(`Erro ao fechar sessão: ${err?.message || err}. O terminal continua aberto.`);
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
      } catch (err: any) {
        const msg = err?.message || String(err);
        showNotice(`Falha ao criar terminal: ${msg}`);
      }
    },
    [panes, projectPath, ensureTabCreated, callTauri]
  );

  // Authorize project directory and start session
  const handleAuthorizeAndStart = useCallback(
    async (dirToAuthorize: string) => {
      if (!dirToAuthorize.trim()) {
        showNotice("Informe um caminho de diretório válido.");
        return;
      }
      try {
        setNotice("");
        if (isTauriRef.current) {
          const res = await callTauri("profile_authorize_project", { projectPath: dirToAuthorize.trim() });
          if (!res || !res.success) {
            throw new Error("Resposta inesperada do autorizador.");
          }
          const canonical = res.project_path;
          setAuthorizedProjects(res.authorized_projects);
          setProjectPath(canonical);
          setIsAuthorizing(false);
          setConnectionStatus("connecting");

          // Step 1: Create tab in Core
          await callTauri("terminal_create_tab", {
            tabId: activeTab,
            projectPath: canonical,
          });
          createdTabsRef.current.add(activeTab);

          // Step 2: Create initial session
          const sess = await callTauri("terminal_create_session", {
            tabId: activeTab,
            paneId: 0,
            kind: "human_terminal",
            cwd: canonical,
            cols: 80,
            rows: 24,
          });

          if (sess && sess.session_id) {
            setPanes([paneFromSession(sess, takeNumber(), activeTab, 0)]);
            setConnectionStatus("connected");
          } else {
            throw new Error("Sessão inicial não retornou session_id válido");
          }
        } else {
          // Preview mode fallback
          setAuthorizedProjects([dirToAuthorize.trim()]);
          setProjectPath(dirToAuthorize.trim());
          setIsAuthorizing(false);
          setConnectionStatus("connected");
        }
      } catch (err: any) {
        const msg = err?.message || String(err);
        showNotice(`Falha na autorização do projeto: ${msg}`);
        setConnectionStatus("authorizing");
      }
    },
    [activeTab, callTauri]
  );

  // Initialize runtime connection and check project authorization
  useEffect(() => {
    let mounted = true;

    async function init() {
      setConnectionStatus("connecting");
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

          // Query active profile and authorized projects
          const profInfo = await callTauri("profile_get_active");
          const authList: string[] = Array.isArray(profInfo?.authorized_projects)
            ? profInfo.authorized_projects
            : [];
          if (mounted) setAuthorizedProjects(authList);

          if (authList.length > 0) {
            // Already has authorized project, select first
            const activeDir = authList[0];
            if (mounted) setProjectPath(activeDir);

            // Step 1: Ensure Tab is created in Core before creating session
            await callTauri("terminal_create_tab", {
              tabId: activeTab,
              projectPath: activeDir,
            });
            createdTabsRef.current.add(activeTab);

            // Step 2: Create initial session
            const sess = await callTauri("terminal_create_session", {
              tabId: activeTab,
              paneId: 0,
              kind: "human_terminal",
              cwd: activeDir,
              cols: 80,
              rows: 24,
            });

            if (!mounted) return;
            if (sess && sess.session_id) {
              setPanes([paneFromSession(sess, takeNumber(), activeTab, 0)]);
              setConnectionStatus("connected");
            } else {
              throw new Error("Sessão inicial não retornou session_id válido");
            }
          } else {
            // Clean install: no authorized project yet. Prompt operator explicitly
            if (!mounted) return;
            setIsAuthorizing(true);
            setConnectionStatus("authorizing");
          }
        } else {
          // Preview mode in standard browser
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
              tabId: activeTab,
              paneId: 0,
              kind: "human_terminal",
              number: takeNumber(),
            },
          ]);
          setConnectionStatus("connected");
        }
      } catch (err: any) {
        if (!mounted) return;
        setConnectionStatus("failed");
        const msg = err?.message || String(err);
        showNotice(`Erro de inicialização da Alpha: ${msg}`);
      }
    }

    init();

    return () => {
      mounted = false;
    };
  }, [activeTab, callTauri]);

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

  // Output polling loop with raw chunk stream preservation for xterm.js
  useEffect(() => {
    if (!isTauri || panes.length === 0) return;

    const interval = setInterval(async () => {
      for (const pane of panes) {
        const sinceSeq = lastSeqRef.current.get(pane.sessionId) || 0;
        try {
          const out = await callTauri("terminal_get_output", {
            sessionId: pane.sessionId,
            sinceSeq,
          });

          if (out) {
            const term = termsRef.current.get(pane.sessionId);
            if (term) {
              // Raw PTY chunk stream preserves exact CR, LF, ANSI escapes, prompts
              if (typeof out.raw === "string" && out.raw.length > 0) {
                term.write(out.raw);
              } else if (Array.isArray(out.lines) && out.lines.length > 0) {
                for (const line of out.lines) {
                  term.writeln(line);
                }
              }
            }
            if (typeof out.next_seq === "number") {
              lastSeqRef.current.set(pane.sessionId, out.next_seq);
            }
          }
        } catch {
          // Polling errors for closing sessions are swallowed
        }
      }
    }, 80);

    return () => clearInterval(interval);
  }, [isTauri, panes, callTauri]);

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

  // Saved interface preferences (e.g. low-stimulation mode) are applied as soon as the host answers.
  useEffect(() => {
    if (!isTauri) return;
    getSettings(callTauri)
      .then((snapshot) => applyInterfaceSettings(snapshot))
      .catch(() => {
        // Defaults stay in place; Configurações shows the load error when opened.
      });
  }, [isTauri, callTauri]);

  // Save session layout
  const handleSaveSessions = useCallback(
    async (tabId: string) => {
      if (isTauriRef.current) {
        try {
          const res = await callTauri("terminal_save_sessions", { tabId });
          showNotice(`Sessão salva com sucesso: ${res?.total_sessions || panes.length} terminais arquivados.`, "success");
        } catch (err: any) {
          showNotice(`Falha ao salvar sessão: ${err?.message || err}`);
        }
      } else {
        showNotice("Layout da sessão exportado (modo preview).", "info");
      }
      setTimeout(() => setNotice(""), 4000);
    },
    [panes.length, callTauri]
  );

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

  const isAuthorizingView = isAuthorizing || connectionStatus === "authorizing";

  // Conversas shows the authorization and startup screens too; the navigation stays available on all of them.
  const conversasContent = isAuthorizingView ? (
    <AuthorizationScreen
      profileName="default"
      inputPath={inputPath}
      onInputPathChange={setInputPath}
      onSubmit={handleAuthorizeAndStart}
    />
  ) : connectionStatus === "connecting" && panes.length === 0 ? (
    <StartupStatus state="connecting" />
  ) : connectionStatus === "failed" && panes.length === 0 ? (
    <StartupStatus state="failed" detail={notice} />
  ) : (
    <TerminalGrid
      tabId={activeTab}
      projectPath={projectPath}
      panes={panes}
      states={paneStates}
      onAddPane={handleAddPane}
      onSaveSessions={handleSaveSessions}
      onWrite={handleWrite}
      onResize={handleResize}
      onClose={handleClosePane}
      onMount={handleMount}
    />
  );

  return (
    <div className="app">
      <AppHeader
        appVersion={appVersion}
        isTauri={isTauri}
        connectionStatus={connectionStatus}
        hasUpdateAvailable={hasUpdateAvailable}
        latestAvailableVersion={latestAvailableVersion}
        onOpenSettings={() => {
          setSettingsCategory(hasUpdateAvailable ? "sobre" : "perfil");
          setIsSettingsOpen(true);
        }}
        nav={<MainNav active={mainView} onSelect={setMainView} paneCount={panes.length} />}
      />
      <p className="sr-only" role="status" data-testid="connection-notice">
        {connectionNotice}
      </p>

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
        onSettingsChanged={(snapshot) => applyInterfaceSettings(snapshot)}
      />
    </div>
  );
};
