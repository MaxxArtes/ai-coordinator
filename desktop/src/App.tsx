import React, { useEffect, useRef, useState, useCallback } from "react";
import { TerminalGrid } from "./components/TerminalGrid";
import { TerminalPaneProps } from "./components/TerminalPane";
import { Terminal } from "xterm";

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
  const [connectionStatus, setConnectionStatus] = useState<"connecting" | "connected" | "failed">("connecting");
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [metrics, setMetrics] = useState<RuntimeMetrics | null>(null);
  const [activeTab, setActiveTab] = useState<string>("tab-main");
  const [projectPath, setProjectPath] = useState<string>("D:\\ai-coordinator");
  const [panes, setPanes] = useState<TerminalPaneProps[]>([]);
  const [notice, setNotice] = useState<string>("");

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
      if (isTauri) {
        await callTauri("terminal_create_tab", { tabId, projectPath: path });
      }
      createdTabsRef.current.add(tabId);
    },
    [isTauri, callTauri]
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

  // Write keystroke to backend
  const handleWrite = useCallback(
    async (sessionId: string, data: string) => {
      if (isTauri) {
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
    [isTauri, callTauri]
  );

  // Resize handler
  const handleResize = useCallback(
    async (sessionId: string, cols: number, rows: number) => {
      if (isTauri) {
        try {
          await callTauri("terminal_resize", { sessionId, cols, rows });
        } catch (err) {
          // Resize errors can be safely ignored during pane disposal
        }
      }
    },
    [isTauri, callTauri]
  );

  // Close pane handler
  const handleClosePane = useCallback(
    async (sessionId: string) => {
      if (isTauri) {
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
    [isTauri, callTauri]
  );

  // Add new pane (Problem 6: Defaults strictly to human_terminal)
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

        if (isTauri) {
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

        const newPane: TerminalPaneProps = {
          sessionId: realSessionId,
          tabId,
          paneId: newPaneId,
          kind,
          title: `Terminal Interativo ${newPaneId + 1}`,
          onWrite: handleWrite,
          onResize: handleResize,
          onClose: handleClosePane,
          onMount: handleMount,
        };

        setPanes((prev) => [...prev, newPane]);
      } catch (err: any) {
        const msg = err?.message || String(err);
        setNotice(`Falha ao criar terminal no painel ${newPaneId + 1}: ${msg}`);
      }
    },
    [panes.length, isTauri, projectPath, ensureTabCreated, callTauri, handleWrite, handleResize, handleClosePane, handleMount]
  );

  // Initialize runtime connection and first pane
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
          setIsTauri(true);
          const st = await callTauri("get_runtime_status");
          if (mounted && st) setStatus(st);

          // Step 1 & 2: Ensure Tab is created in Core before creating session
          await callTauri("terminal_create_tab", {
            tabId: activeTab,
            projectPath: projectPath,
          });
          createdTabsRef.current.add(activeTab);

          // Step 3: Create initial session and obtain real backend session_id
          const sess = await callTauri("terminal_create_session", {
            tabId: activeTab,
            paneId: 0,
            kind: "human_terminal",
            cwd: projectPath,
            cols: 80,
            rows: 24,
          });

          if (!mounted) return;
          if (sess && sess.session_id) {
            const firstPane: TerminalPaneProps = {
              sessionId: sess.session_id,
              tabId: activeTab,
              paneId: 0,
              kind: "human_terminal",
              title: "Terminal Interativo 1",
              onWrite: handleWrite,
              onResize: handleResize,
              onClose: handleClosePane,
              onMount: handleMount,
            };
            setPanes([firstPane]);
            setConnectionStatus("connected");
          } else {
            throw new Error("Sessão inicial não retornou session_id válido");
          }
        } else {
          // Preview mode in standard browser
          setIsTauri(false);
          setStatus({
            core_version: "0.6.0",
            host_os: "windows",
            is_isolated: true,
            python_path: "desktop/runtime/python_spec.json (preview)",
          });
          const mockSessionId = `preview-sess-0`;
          setPanes([
            {
              sessionId: mockSessionId,
              tabId: activeTab,
              paneId: 0,
              kind: "human_terminal",
              title: "Terminal Interativo (Preview)",
              onWrite: handleWrite,
              onResize: handleResize,
              onClose: handleClosePane,
              onMount: handleMount,
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
  }, []);

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

  // Output polling loop with monotonic sequence tracking (Problem 7)
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

          if (out && Array.isArray(out.lines) && out.lines.length > 0) {
            const term = termsRef.current.get(pane.sessionId);
            if (term) {
              for (const line of out.lines) {
                term.writeln(line);
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
    }, 100);

    return () => clearInterval(interval);
  }, [isTauri, panes, callTauri]);

  // Save session layout
  const handleSaveSessions = useCallback(
    async (tabId: string) => {
      if (isTauri) {
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
    [isTauri, panes.length, callTauri]
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
            ) : connectionStatus === "connecting" ? (
              <strong className="text-amber-400">Conectando...</strong>
            ) : (
              <strong className="text-red-400">Falha</strong>
            )}
          </span>
        </div>

        {/* Telemetry metrics */}
        <div className="flex items-center space-x-4 text-xs font-mono text-gray-300">
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

      {/* Main Terminal Quad Grid */}
      <main className="flex-1 w-full h-full min-h-0 overflow-hidden">
        {connectionStatus === "connecting" && panes.length === 0 ? (
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
          />
        )}
      </main>
    </div>
  );
};
