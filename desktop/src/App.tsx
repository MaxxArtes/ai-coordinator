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
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [metrics, setMetrics] = useState<RuntimeMetrics | null>(null);
  const [activeTab, setActiveTab] = useState<string>("tab-main");
  const [projectPath, setProjectPath] = useState<string>("D:\\ai-coordinator");
  const [panes, setPanes] = useState<TerminalPaneProps[]>([]);
  const [notice, setNotice] = useState<string>("");

  const termsRef = useRef<Map<string, Terminal>>(new Map());
  const outputOffsetsRef = useRef<Map<string, number>>(new Map());

  // Helper to safely call Tauri invoke
  const callTauri = useCallback(async (cmd: string, args?: Record<string, any>): Promise<any> => {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      return await invoke(cmd, args);
    } catch (err) {
      return null;
    }
  }, []);

  // Initialize runtime connection
  useEffect(() => {
    let mounted = true;

    async function init() {
      const pingRes = await callTauri("ping");
      if (mounted) {
        if (pingRes === "pong") {
          setIsTauri(true);
          const st = await callTauri("get_runtime_status");
          if (st) setStatus(st);
        } else {
          setIsTauri(false);
          setStatus({
            core_version: "0.6.0",
            host_os: "windows",
            is_isolated: true,
            python_path: "desktop/runtime/python_spec.json (preview)",
          });
        }
      }
    }

    init();

    return () => {
      mounted = false;
    };
  }, [callTauri]);

  // Periodic metrics refresh
  useEffect(() => {
    if (!isTauri) return;
    const interval = setInterval(async () => {
      const m = await callTauri("get_process_tree_metrics");
      if (m) setMetrics(m);
    }, 2000);
    return () => clearInterval(interval);
  }, [isTauri, callTauri]);

  // Terminal mount callback
  const handleMount = useCallback((sessionId: string, term: Terminal | null) => {
    if (term) {
      termsRef.current.set(sessionId, term);
      if (!outputOffsetsRef.current.has(sessionId)) {
        outputOffsetsRef.current.set(sessionId, 0);
      }
      // Initial welcome message in pane
      term.writeln("\x1b[1;36m=== AI Coordinator Desktop — Alpha v0.6.0 ===\x1b[0m");
      term.writeln("\x1b[90mPseudoterminal governado ConPTY inicializado com sucesso.\x1b[0m\r\n");
    } else {
      termsRef.current.delete(sessionId);
      outputOffsetsRef.current.delete(sessionId);
    }
  }, []);

  // Write keystroke to backend
  const handleWrite = useCallback(
    async (sessionId: string, data: string) => {
      if (isTauri) {
        await callTauri("terminal_write", { sessionId, data });
      } else {
        // Preview mode local echo & simulated response
        const term = termsRef.current.get(sessionId);
        if (term) {
          if (data === "\r") {
            term.write("\r\n");
            term.writeln("\x1b[33m[preview-pty]\x1b[0m Comando recebido (Host Tauri desconectado em modo web preview)");
            term.write("\x1b[1;32mai-coordinator:preview> \x1b[0m");
          } else if (data === "\u007f") {
            // Backspace
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
        await callTauri("terminal_resize", { sessionId, cols, rows });
      }
    },
    [isTauri, callTauri]
  );

  // Close pane handler
  const handleClosePane = useCallback(
    async (sessionId: string) => {
      if (isTauri) {
        await callTauri("terminal_close_session", { sessionId });
      }
      setPanes((prev) => prev.filter((p) => p.sessionId !== sessionId));
    },
    [isTauri, callTauri]
  );

  // Add new pane (up to 4)
  const handleAddPane = useCallback(
    async (tabId: string) => {
      if (panes.length >= 4) {
        setNotice("Limite máximo de 4 painéis simultâneos por aba atingido.");
        return;
      }
      const newPaneId = panes.length;
      const newSessionId = `sess-${Date.now().toString(36)}-${newPaneId}`;
      const kind = newPaneId === 0 ? "human_terminal" : "governed_agent";

      if (isTauri) {
        await callTauri("terminal_create_session", {
          tabId,
          paneId: newPaneId,
          kind,
          cwd: projectPath,
          cols: 80,
          rows: 24,
        });
      }

      const newPane: TerminalPaneProps = {
        sessionId: newSessionId,
        tabId,
        paneId: newPaneId,
        kind,
        title: newPaneId === 0 ? "Terminal Interativo" : `Agente Governado ${newPaneId}`,
        onWrite: handleWrite,
        onResize: handleResize,
        onClose: handleClosePane,
        onMount: handleMount,
      };

      setPanes((prev) => [...prev, newPane]);
    },
    [panes.length, projectPath, isTauri, callTauri, handleWrite, handleResize, handleClosePane, handleMount]
  );

  // Save session layout
  const handleSaveSessions = useCallback(
    async (tabId: string) => {
      if (isTauri) {
        const res = await callTauri("terminal_save_sessions", { tabId });
        setNotice(`Sessão salva com sucesso: ${res?.total_sessions || panes.length} terminais arquivados.`);
      } else {
        setNotice("Layout da sessão exportado (modo preview).");
      }
      setTimeout(() => setNotice(""), 4000);
    },
    [isTauri, panes.length, callTauri]
  );

  // Initialize initial pane
  useEffect(() => {
    if (panes.length === 0) {
      handleAddPane(activeTab);
    }
  }, [panes.length, activeTab, handleAddPane]);

  // Output polling loop in Tauri mode
  useEffect(() => {
    if (!isTauri) return;

    const interval = setInterval(async () => {
      for (const pane of panes) {
        const out = await callTauri("terminal_get_output", { sessionId: pane.sessionId });
        if (out && Array.isArray(out.lines)) {
          const currentOffset = outputOffsetsRef.current.get(pane.sessionId) || 0;
          if (out.lines.length > currentOffset) {
            const newLines = out.lines.slice(currentOffset);
            outputOffsetsRef.current.set(pane.sessionId, out.lines.length);
            const term = termsRef.current.get(pane.sessionId);
            if (term) {
              for (const line of newLines) {
                term.writeln(line);
              }
            }
          }
        }
      }
    }, 100);

    return () => clearInterval(interval);
  }, [isTauri, panes, callTauri]);

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
            <span className="text-xs px-2 py-0.5 rounded bg-amber-600 text-white font-medium" title="Visualização provisória via Vite/Navegador sem host Tauri">
              Vite Preview (Não-Homologado)
            </span>
          )}
          <span className="text-xs text-gray-400">
            Supervisão ConPTY: <strong className="text-emerald-400">Ativa</strong>
          </span>
        </div>

        {/* Telemetry metrics preview */}
        <div className="flex items-center space-x-4 text-xs font-mono text-gray-300">
          {metrics ? (
            <>
              <span>PID: {metrics.host_pid}</span>
              <span>RAM Host: {formatBytes(metrics.host_ram_bytes)}</span>
              <span>RAM Árvore: {formatBytes(metrics.total_tree_ram_bytes)}</span>
              <span>CPU: {metrics.cpu_usage_percent.toFixed(1)}%</span>
            </>
          ) : (
            <span className="text-gray-500">Métricas de Processo em Espera</span>
          )}
        </div>
      </header>

      {/* Notice Banner if any */}
      {notice && (
        <div className="bg-blue-900 border-b border-blue-700 px-4 py-1 text-xs text-blue-100 flex items-center justify-between">
          <span>{notice}</span>
          <button onClick={() => setNotice("")} className="text-blue-300 hover:text-white">✕</button>
        </div>
      )}

      {/* Main Terminal Quad Grid */}
      <main className="flex-1 w-full h-full min-h-0 overflow-hidden">
        <TerminalGrid
          tabId={activeTab}
          projectPath={projectPath}
          panes={panes}
          onAddPane={handleAddPane}
          onSaveSessions={handleSaveSessions}
        />
      </main>
    </div>
  );
};
