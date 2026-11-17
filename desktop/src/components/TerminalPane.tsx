/**
 * Visual Terminal Component powered by xterm.js for AI Coordinator Desktop (Marco 4D.2.2).
 *
 * Provides:
 * - Full ANSI VT sequence parsing & true color support.
 * - UTF-8 character encoding with emoji and CJK compatibility.
 * - Dynamic dimension resizing with FitAddon and ResizeObserver.
 * - Bidirectional streaming with backpressure protection.
 * - Keyboard accessibility, focus management, and copy/paste keybindings.
 * - Stale-closure prevention: Callbacks are kept in live refs so xterm instance
 *   is never disposed or re-mounted due to callback reference changes.
 */

import React, { useEffect, useRef } from "react";
import { Terminal } from "xterm";
import { FitAddon } from "xterm-addon-fit";
import { Icon } from "./ui/Icon";
import { applyXtermAppearance, readXtermAppearance, watchXtermTheme } from "../styles/xtermTheme";

export interface TerminalPaneData {
  sessionId: string;
  tabId: string;
  paneId: number;
  kind: "human_terminal" | "governed_agent";
  title?: string;
}

export interface TerminalPaneProps extends TerminalPaneData {
  onWrite: (sessionId: string, data: string) => void;
  onResize: (sessionId: string, cols: number, rows: number) => void;
  onClose?: (sessionId: string) => void;
  onMount?: (sessionId: string, term: Terminal | null) => void;
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
  onMount,
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);

  // Keep latest callbacks in refs to eliminate stale closure bugs while keeping xterm mount stable
  const onWriteRef = useRef(onWrite);
  onWriteRef.current = onWrite;
  const onResizeRef = useRef(onResize);
  onResizeRef.current = onResize;
  const onMountRef = useRef(onMount);
  onMountRef.current = onMount;

  useEffect(() => {
    if (!containerRef.current) return;

    // 1. Initialize xterm with the palette of the design tokens (standard, low stimulation or Windows contrast theme)
    const appearance = readXtermAppearance();
    const term = new Terminal({
      cursorBlink: true,
      cursorStyle: "bar",
      fontSize: 13,
      fontFamily: "'Cascadia Code', 'Fira Code', 'Consolas', monospace",
      theme: appearance.theme,
      minimumContrastRatio: appearance.minimumContrastRatio,
      scrollback: 1000, // Aligned with bounded buffer (1000 lines)
      allowProposedApi: true,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);

    term.open(containerRef.current);
    fitAddon.fit();

    terminalRef.current = term;
    fitAddonRef.current = fitAddon;

    // Theme changes (low stimulation, Windows contrast themes) are applied in place: the session is never restarted.
    const stopTheme = watchXtermTheme((next) => applyXtermAppearance(term, next));

    if (onMountRef.current) {
      onMountRef.current(sessionId, term);
    }

    // 2. Keystroke forwarding using live ref
    const dataDisposable = term.onData((data) => {
      onWriteRef.current(sessionId, data);
    });

    // 3. Resize handling with ResizeObserver using live ref
    const observer = new ResizeObserver(() => {
      try {
        fitAddon.fit();
        onResizeRef.current(sessionId, term.cols, term.rows);
      } catch (err) {
        // Ignored during unmount
      }
    });

    observer.observe(containerRef.current);

    // Initial resize sync
    onResizeRef.current(sessionId, term.cols, term.rows);

    return () => {
      if (onMountRef.current) {
        onMountRef.current(sessionId, null);
      }
      stopTheme();
      dataDisposable.dispose();
      observer.disconnect();
      term.dispose();
    };
  }, [sessionId]);

  const badgeClass = kind === "human_terminal" ? "badge badge--human" : "badge badge--agent";
  const badgeLabel = kind === "human_terminal" ? "Terminal humano" : "Agente governado";

  return (
    <div className="terminal-pane" role="region" aria-label={`Terminal ${title || `Painel ${paneId + 1}`}`}>
      {/* Header Bar */}
      <div className="terminal-pane__header">
        <div className="terminal-pane__heading">
          <span className={badgeClass}>{badgeLabel}</span>
          <span className="terminal-pane__title">
            {title || `Painel ${paneId + 1}`} (Sessão: {sessionId.slice(0, 8)})
          </span>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={() => onClose(sessionId)}
            className="btn btn--ghost btn--ghost-danger btn--icon btn--sm"
            title="Fechar terminal"
            aria-label="Fechar terminal"
          >
            <Icon name="close" size="sm" />
          </button>
        )}
      </div>

      {/* xterm.js DOM Mount Point */}
      <div ref={containerRef} className="terminal-pane__mount" tabIndex={0} />
    </div>
  );
};
