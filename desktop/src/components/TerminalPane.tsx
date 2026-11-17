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
 *
 * Marco 4D.3, Etapa 5:
 * - Theme from the design tokens, changed live (low stimulation, Windows contrast themes) without a restart.
 * - Size: no fit and no PTY resize while the box has no area (a hidden view, a minimized window); the PTY is told
 *   only when the columns or rows really change.
 * - Keyboard: one Tab stop per terminal; F6/Shift+F6 leave the terminal (every other key goes to the shell); the pane
 *   has a focus region in its header ("Terminal N de M"), the F6 entry, from which Enter or Space enters the terminal.
 */

import React, { useEffect, useId, useRef } from "react";
import { Terminal } from "xterm";
import { FitAddon } from "xterm-addon-fit";
import { Icon } from "./ui/Icon";
import { terminalKeyFilter } from "./shell/shortcuts";
import { applyXtermAppearance, readXtermAppearance, watchXtermTheme } from "../styles/xtermTheme";

export interface TerminalPaneData {
  sessionId: string;
  tabId: string;
  paneId: number;
  kind: "human_terminal" | "governed_agent";
  title?: string;
}

export interface TerminalPaneProps extends TerminalPaneData {
  /** 1-based position of the pane in the tab ("Terminal N de M"). */
  position: number;
  /** Number of panes in the tab. */
  total: number;
  onWrite: (sessionId: string, data: string) => void;
  onResize: (sessionId: string, cols: number, rows: number) => void;
  onClose?: (sessionId: string) => void;
  onMount?: (sessionId: string, term: Terminal | null) => void;
}

/** A box under 1 px (hidden view, minimized window): FitAddon would shrink the terminal to 2×1. */
export const hasArea = (width: number, height: number): boolean => width >= 1 && height >= 1;

export const TerminalPane: React.FC<TerminalPaneProps> = ({
  sessionId,
  tabId,
  paneId,
  kind,
  title,
  position,
  total,
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
    const container = containerRef.current;
    if (!container) return;

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
    // F6/Shift+F6 leave the terminal (shell/shortcuts.ts); every other key goes to the shell.
    term.attachCustomKeyEventHandler(terminalKeyFilter);

    term.open(container);
    // One Tab stop per terminal: since Chromium 130 a scrollable area is itself a Tab stop once there is history.
    // A click on the scrollbar still focuses the terminal (xterm handles mousedown on its element).
    term.element?.querySelector<HTMLElement>(".xterm-viewport")?.setAttribute("tabindex", "-1");
    const initialBox = container.getBoundingClientRect();
    if (hasArea(initialBox.width, initialBox.height)) fitAddon.fit();

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

    // 3. The PTY hears only real changes of columns or rows. The last size sent starts empty, so the initial sync
    //    always goes out (the session is born 80×24 and the first fit may also give 80×24).
    let lastSent: { cols: number; rows: number } | null = null;
    const syncSize = () => {
      if (lastSent && lastSent.cols === term.cols && lastSent.rows === term.rows) return;
      lastSent = { cols: term.cols, rows: term.rows };
      onResizeRef.current(sessionId, term.cols, term.rows);
    };

    const observer = new ResizeObserver((entries) => {
      const box = entries[entries.length - 1]?.contentRect;
      if (!box || !hasArea(box.width, box.height)) return; // zero area: no fit and no PTY resize
      try {
        fitAddon.fit();
        syncSize();
      } catch (err) {
        // Ignored during unmount
      }
    });

    observer.observe(container);

    // Initial resize sync
    syncSize();

    return () => {
      stopTheme();
      if (onMountRef.current) {
        onMountRef.current(sessionId, null);
      }
      dataDisposable.dispose();
      observer.disconnect();
      term.dispose();
    };
  }, [sessionId]);

  // xterm announces its input as "Terminal input": the name now says which terminal it is and how to leave it.
  const terminalName = `Terminal ${position} de ${total}. F6 sai do terminal`;
  useEffect(() => {
    terminalRef.current?.textarea?.setAttribute("aria-label", terminalName);
  }, [sessionId, terminalName]);

  const hintId = useId();
  const hint = onClose
    ? "Enter: digitar no terminal. Tab: botão fechar. F6: próxima região."
    : "Enter: digitar no terminal. F6: próxima região.";

  // A click enters the terminal too: screen readers in browse mode turn Enter and Space into a click on the region.
  const enterTerminal = () => terminalRef.current?.focus();

  // F6 entry of the pane. preventDefault on keydown also suppresses the keypress: the Enter never reaches the shell.
  const handleEntryKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget || event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    enterTerminal();
  };

  const badgeClass = kind === "human_terminal" ? "badge badge--human" : "badge badge--agent";
  const badgeLabel = kind === "human_terminal" ? "Terminal humano" : "Agente governado";

  return (
    <div
      className="terminal-pane"
      role="region"
      aria-label={`Terminal ${title || `Painel ${paneId + 1}`}`}
      data-f6-region="pane"
    >
      {/* Header Bar */}
      <div className="terminal-pane__header">
        <div
          className="terminal-pane__heading terminal-pane__focus-region"
          role="group"
          aria-label={`Terminal ${position} de ${total}`}
          aria-describedby={hintId}
          tabIndex={-1}
          data-f6-entry=""
          onKeyDown={handleEntryKeyDown}
          onClick={enterTerminal}
        >
          <span className={badgeClass}>{badgeLabel}</span>
          <span className="terminal-pane__title">
            {title || `Painel ${paneId + 1}`} (Sessão: {sessionId.slice(0, 8)})
          </span>
        </div>
        <span id={hintId} className="sr-only">
          {hint}
        </span>
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

      {/* xterm.js DOM Mount Point (no Tab stop of its own: the terminal input is the stop) */}
      <div ref={containerRef} className="terminal-pane__mount" />
    </div>
  );
};
