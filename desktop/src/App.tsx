import React, { useEffect, useRef, useState, useCallback } from "react";
import { TerminalGrid } from "./components/TerminalGrid";
import { TerminalPaneData } from "./components/TerminalPane";
import { Terminal } from "xterm";
import { UpdateModal } from "./components/UpdateModal";

interface RuntimeMetrics {
  host_pid: number;
  host_ram_bytes: number;
  total_tree_ram_bytes: number;
  cpu_usage_percent: number;
}

interface RuntimeStatus {
  core_version: string;
  host_os: string;
  is_isolated: boolean;
  python_path: string;
}

export const App: React.FC = () => {
  const [isTauri, setIsTauri] = useState<boolean>(false);
  const isTauriRef = useRef<boolean>(false);

  const [connectionStatus, setConnectionStatus] = useState<"connecting" | "authorizing" | "connected" | "failed">("connecting");
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [metrics, setMetrics] = useState<RuntimeMetrics | null>(null);
  const [activeTab, setActiveTab] = useState<string>("tab-main");
  const [projectPath, setProjectPath] = useState<string>("");
  const [authorizedProjects, setAuthorizedProjects] = useState<string[]>([]);
  const [isAuthorizing, setIsAuthorizing] = useState<boolean>(false);
  const [inputPath, setInputPath] = useState<string>("");
  const [panes, setPanes] = useState<TerminalPaneData[]>([]);
  const [notice, setNotice] = useState<string>("");
  const [isUpdateModalOpen, setIsUpdateModalOpen] = useState<boolean>(false);
  const [hasUpdateAvailable, setHasUpdateAvailable] = useState<boolean>(false);
  const [latestAvailableVersion, setLatestAvailableVersion] = useState<string>("");

  const termsRef = useRef<Map<string, Terminal>>(new Map());
  const lastSeqRef = useRef<Map<string, number>>(new Map());
  const createdTabsRef = useRef<Set<string>>(new Set());

  // Direct invocation without swallowing errors
  const callTauri = useCallback(async (cmd: string, args?: Record<string, any>): Promise<any> => {
    const { invoke } = await import("@tauri-apps/api/core");
    return await invoke(cmd, args);
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
      term.writeln("\x1b[1;36m=== AI Coordinator Desktop — Alpha v0.6.0 ===\x1b[0m");
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
        try {
          await callTauri("terminal_write", { sessionId, data });
        } catch (err: any) {
          setNotice(`Erro de escrita no terminal: ${err?.message || err}`);
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
          await callTauri("terminal_close_session", { sessionId });
        } catch (err: any) {
          setNotice(`Erro ao fechar sessão: ${err?.message || err}`);
        }
      }
      setPanes((prev) => prev.filter((p) => p.sessionId !== sessionId));
      termsRef.current.delete(sessionId);
      lastSeqRef.current.delete(sessionId);
    },
    [callTauri]
  );

  // Add new pane (Defaults strictly to human_terminal)
  const handleAddPane = useCallback(
    async (tabId: string) => {
      if (panes.length >= 4) {
        setNotice("Limite máximo de 4 painéis simultâneos por aba atingido.");
        return;
      }
      const newPaneId = panes.length;
      const kind = "human_terminal";

      try {
        let realSessionId = `preview-sess-${Date.now().toString(36)}-${newPaneId}`;

        if (isTauriRef.current) {
          await ensureTabCreated(tabId, projectPath);
          const sess = await callTauri("terminal_create_session", {
            tabId,
            paneId: newPaneId,
            kind,
            cwd: projectPath,
            cols: 80,
            rows: 24,
          });

          if (!sess || !sess.session_id) {
            throw new Error("Backend não retornou session_id válido");
          }
          realSessionId = sess.session_id;
        }

        const newPane: TerminalPaneData = {
          sessionId: realSessionId,
          tabId,
          paneId: newPaneId,
          kind,
          title: `Terminal Interativo ${newPaneId + 1}`,
        };

        setPanes((prev) => [...prev, newPane]);
      } catch (err: any) {
        const msg = err?.message || String(err);
        setNotice(`Falha ao criar terminal no painel ${newPaneId + 1}: ${msg}`);
      }
    },
    [panes.length, projectPath, ensureTabCreated, callTauri]
  );

  // Authorize project directory and start session
  const handleAuthorizeAndStart = useCallback(
    async (dirToAuthorize: string) => {
      if (!dirToAuthorize.trim()) {
        setNotice("Informe um caminho de diretório válido.");
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
            const firstPane: TerminalPaneData = {
              sessionId: sess.session_id,
              tabId: activeTab,
              paneId: 0,
              kind: "human_terminal",
              title: "Terminal Interativo 1",
            };
            setPanes([firstPane]);
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
        setNotice(`Falha na autorização do projeto: ${msg}`);
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
              const firstPane: TerminalPaneData = {
                sessionId: sess.session_id,
                tabId: activeTab,
                paneId: 0,
                kind: "human_terminal",
                title: "Terminal Interativo 1",
              };
              setPanes([firstPane]);
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
              title: "Terminal Interativo (Preview)",
            },
          ]);
          setConnectionStatus("connected");
        }
      } catch (err: any) {
        if (!mounted) return;
        setConnectionStatus("failed");
        const msg = err?.message || String(err);
        setNotice(`Erro de inicialização da Alpha: ${msg}`);
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
        if (m) setMetrics(m);
      } catch {
        // Telemetry errors are non-fatal
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

  // Save session layout
  const handleSaveSessions = useCallback(
    async (tabId: string) => {
      if (isTauriRef.current) {
        try {
          const res = await callTauri("terminal_save_sessions", { tabId });
          setNotice(`Sessão salva com sucesso: ${res?.total_sessions || panes.length} terminais arquivados.`);
        } catch (err: any) {
          setNotice(`Falha ao salvar sessão: ${err?.message || err}`);
        }
      } else {
        setNotice("Layout da sessão exportado (modo preview).");
      }
      setTimeout(() => setNotice(""), 4000);
    },
    [panes.length, callTauri]
  );

  const formatBytes = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  return (
    <div className="flex flex-col h-screen w-screen bg-[#11111b] text-[#cdd6f4]">
      {/* Top Application Header */}
      <header className="flex items-center justify-between px-4 py-2 bg-[#181825] border-b border-gray-700 select-none">
        <div className="flex items-center space-x-3">
          <span className="font-bold text-sm tracking-wide text-white">AI Coordinator Desktop</span>
          <span className="text-xs px-2 py-0.5 rounded bg-gray-800 text-gray-300">
            Core v{status?.core_version || "0.6.0"}
          </span>
          {isTauri ? (
            <span className="text-xs px-2 py-0.5 rounded bg-emerald-600 text-white font-medium">
              Tauri 2 + WebView2 (Nativo)
            </span>
          ) : (
            <span
              className="text-xs px-2 py-0.5 rounded bg-amber-600 text-white font-medium"
              title="Visualização provisória via Vite/Navegador sem host Tauri"
            >
              Vite Preview (Não-Homologado)
            </span>
          )}
          <span className="text-xs text-gray-400">
            Supervisão ConPTY:{" "}
            {connectionStatus === "connected" ? (
              <strong className="text-emerald-400">Ativa</strong>
            ) : connectionStatus === "authorizing" ? (
              <strong className="text-blue-400">Aguardando Autorização</strong>
            ) : connectionStatus === "connecting" ? (
              <strong className="text-amber-400">Conectando...</strong>
            ) : (
              <strong className="text-red-400">Falha</strong>
            )}
          </span>

          {/* Direct verification of Python runtime path */}
          {status?.python_path && (
            <span
              className="text-xs px-2 py-0.5 rounded bg-gray-800 text-gray-300 font-mono truncate max-w-xs"
              title={`Caminho efetivo do runtime Python no host: ${status.python_path}`}
            >
              Python: {status.python_path.replace(/\\/g, "/").split("/").pop()} ({status.is_isolated ? "Isolado" : "Host"})
            </span>
          )}
        </div>

        {/* Telemetry metrics & Updates button */}
        <div className="flex items-center space-x-3 text-xs font-mono text-gray-300">
          {metrics ? (
            <>
              <span>PID: {metrics.host_pid}</span>
              <span>RAM Host: {formatBytes(metrics.host_ram_bytes)}</span>
              <span>RAM Árvore: {formatBytes(metrics.total_tree_ram_bytes)}</span>
              <span>CPU: {metrics.cpu_usage_percent.toFixed(1)}%</span>
            </>
          ) : (
            <span className="text-gray-500">Métricas em Espera</span>
          )}

          <button
            onClick={() => setIsUpdateModalOpen(true)}
            className={`px-2.5 py-1 rounded text-xs font-sans font-medium flex items-center space-x-1.5 transition-all ${
              hasUpdateAvailable
                ? "bg-purple-600 hover:bg-purple-500 text-white shadow-md animate-pulse"
                : "bg-gray-800 hover:bg-gray-700 text-gray-300"
            }`}
            title="Configurações e Atualizações do AI Coordinator Desktop"
          >
            <span>{hasUpdateAvailable ? "✨" : "🔄"}</span>
            <span>{hasUpdateAvailable ? `Atualização v${latestAvailableVersion}` : "Atualizações"}</span>
          </button>
        </div>
      </header>

      {/* Notice / Error Banner */}
      {notice && (
        <div className="bg-red-950 border-b border-red-800 px-4 py-1.5 text-xs text-red-200 flex items-center justify-between">
          <span className="font-mono">{notice}</span>
          <button onClick={() => setNotice("")} className="text-red-300 hover:text-white font-bold ml-4">
            ✕
          </button>
        </div>
      )}

      {/* Main Terminal Area */}
      <main className="flex-1 w-full h-full min-h-0 overflow-hidden">
        {isAuthorizing || connectionStatus === "authorizing" ? (
          <div className="flex flex-col items-center justify-center h-full p-6 max-w-xl mx-auto">
            <div className="bg-[#1e1e2e] border border-blue-500/40 rounded-lg p-6 shadow-2xl w-full">
              <div className="flex items-center space-x-2 mb-3">
                <span className="text-xl">🛡️</span>
                <h2 className="text-sm font-bold text-white uppercase tracking-wider">
                  Autorização de Projeto Obrigatória
                </h2>
              </div>
              <p className="text-xs text-gray-300 mb-4 leading-relaxed">
                Nenhum projeto está autorizado para o perfil ativo (<code className="text-blue-300">default</code>).
                Para assegurar a contenção atômica do ConPTY e proteger o sistema de arquivos do host, informe e
                autorize explicitamente o diretório de trabalho antes de iniciar o terminal.
              </p>
              <div className="mb-4">
                <label className="block text-xs font-mono text-gray-400 mb-1">
                  Caminho do Projeto no Host:
                </label>
                <input
                  type="text"
                  value={inputPath}
                  onChange={(e) => setInputPath(e.target.value)}
                  placeholder="Ex: C:\Users\10864 ou D:\meu-projeto"
                  className="w-full bg-[#11111b] border border-gray-600 rounded px-3 py-2 text-xs font-mono text-white focus:outline-none focus:border-blue-500"
                />
              </div>
              <div className="flex items-center justify-end space-x-3">
                <button
                  onClick={() => handleAuthorizeAndStart(inputPath)}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded text-xs font-semibold tracking-wide transition-colors"
                >
                  Autorizar Diretório e Iniciar Terminal
                </button>
              </div>
            </div>
          </div>
        ) : connectionStatus === "connecting" && panes.length === 0 ? (
          <div className="flex items-center justify-center h-full text-gray-400 font-mono text-sm">
            Inicializando runtime do Core e conectando ponte ConPTY...
          </div>
        ) : connectionStatus === "failed" && panes.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-red-400 font-mono text-sm p-4">
            <p className="font-bold mb-2">Falha na Inicialização da Alpha Técnica</p>
            <p className="text-gray-300 text-xs mb-4">{notice || "Verifique se o runtime Python privado está disponível."}</p>
          </div>
        ) : (
          <TerminalGrid
            tabId={activeTab}
            projectPath={projectPath}
            panes={panes}
            onAddPane={handleAddPane}
            onSaveSessions={handleSaveSessions}
            onWrite={handleWrite}
            onResize={handleResize}
            onClose={handleClosePane}
            onMount={handleMount}
          />
        )}
      </main>

      {/* Update Modal */}
      <UpdateModal
        isOpen={isUpdateModalOpen}
        onClose={() => setIsUpdateModalOpen(false)}
        activePanesCount={panes.length}
        isTauri={isTauri}
        callTauri={callTauri}
      />
    </div>
  );
};
