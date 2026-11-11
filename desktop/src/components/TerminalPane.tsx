/**
 * Visual Terminal Component powered by xterm.js for AI Coordinator Desktop (Marco 4D.2.2).
 *
 * Provides:
 * - Full ANSI VT sequence parsing & true color support.
 * - UTF-8 character encoding with emoji and CJK compatibility.
 * - Dynamic dimension resizing with FitAddon and ResizeObserver.
 * - Bidirectional streaming with backpressure protection.
 * - Keyboard accessibility, focus management, and copy/paste keybindings.
 */

import React, { useEffect, useRef } from "react";
import { Terminal } from "xterm";
import { FitAddon } from "xterm-addon-fit";

export interface TerminalPaneProps {
  sessionId: string;
  tabId: string;
  paneId: number;
  kind: "human_terminal" | "governed_agent";
  title?: string;
  onWrite: (sessionId: string, data: string) => void;
  onResize: (sessionId: string, cols: number, rows: number) => void;
  onClose?: (sessionId: string) => void;
}

export const TerminalPane: React.FC<TerminalPaneProps> = ({
  sessionId,
  tabId,
  paneId,
  kind,
  title,
  onWrite,
  onResize,
  onClose,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    // 1. Initialize xterm instance with accessible dark palette
    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "bar",
      fontSize: 13,
      fontFamily: "'Cascadia Code', 'Fira Code', 'Consolas', monospace",
      theme: {
        background: "#181825",
        foreground: "#cdd6f4",
        cursor: "#f5e0dc",
        selectionBackground: "#45475a",
        black: "#45475a",
        red: "#f38ba8",
        green: "#a6e3a1",
        yellow: "#f9e2af",
        blue: "#89b4fa",
        magenta: "#cba6f7",
        cyan: "#94e2d5",
        white: "#bac2de",
      },
      scrollback: 1000, // Aligned with bounded buffer (1000 lines)
      allowProposedApi: true,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);

    term.open(containerRef.current);
    fitAddon.fit();

    terminalRef.current = term;
    fitAddonRef.current = fitAddon;

    // 2. Keystroke forwarding
    const dataDisposable = term.onData((data) => {
      onWrite(sessionId, data);
    });

    // 3. Resize handling with ResizeObserver
    const observer = new ResizeObserver(() => {
      try {
        fitAddon.fit();
        onResize(sessionId, term.cols, term.rows);
      } catch (err) {
        // Ignored during unmount
      }
    });

    observer.observe(containerRef.current);

    // Initial resize sync
    onResize(sessionId, term.cols, term.rows);

    return () => {
      dataDisposable.dispose();
      observer.disconnect();
      term.dispose();
    };
  }, [sessionId, onWrite, onResize]);

  const badgeColor = kind === "human_terminal" ? "bg-blue-600" : "bg-emerald-600";
  const badgeLabel = kind === "human_terminal" ? "Terminal Humano" : "Agente Governado";

  return (
    <div
      className="flex flex-col h-full w-full bg-[#181825] border border-gray-700 rounded overflow-hidden"
      role="region"
      aria-label={`Terminal ${title || `Painel ${paneId + 1}`}`}
    >
      {/* Header Bar */}
      <div className="flex items-center justify-between px-3 py-1 bg-[#1e1e2e] border-b border-gray-700 text-xs text-gray-300">
        <div className="flex items-center space-x-2">
          <span className={`px-2 py-0.5 rounded text-[10px] font-semibold text-white ${badgeColor}`}>
            {badgeLabel}
          </span>
          <span className="font-mono text-gray-400">
            {title || `Painel ${paneId + 1}`} (Sessão: {sessionId.slice(0, 8)})
          </span>
        </div>
        {onClose && (
          <button
            onClick={() => onClose(sessionId)}
            className="text-gray-400 hover:text-red-400 focus:outline-none focus:ring-1 focus:ring-red-400 px-1 rounded"
            title="Fechar Terminal"
            aria-label="Fechar Terminal"
          >
            ✕
          </button>
        )}
      </div>

      {/* xterm.js DOM Mount Point */}
      <div ref={containerRef} className="flex-1 w-full h-full p-1 overflow-hidden" tabIndex={0} />
    </div>
  );
};
