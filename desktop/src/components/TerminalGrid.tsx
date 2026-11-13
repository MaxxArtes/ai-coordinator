/**
 * Responsive Quad Terminal Grid for AI Coordinator Desktop (Marco 4D.2.3).
 *
 * Supports up to 4 simultaneous panes per tab:
 * - 1 Pane: Full width/height.
 * - 2 Panes: 50% / 50% split.
 * - 3-4 Panes: 2x2 grid view with independent focus and streaming.
 * - Pure data binding: panes contain metadata only; handlers are dynamically bound at render.
 */

import React from "react";
import { Terminal } from "xterm";
import { TerminalPane, TerminalPaneData } from "./TerminalPane";

export interface TerminalGridProps {
  tabId: string;
  projectPath: string;
  panes: TerminalPaneData[];
  onAddPane?: (tabId: string) => void;
  onSaveSessions?: (tabId: string) => void;
  onWrite: (sessionId: string, data: string) => void;
  onResize: (sessionId: string, cols: number, rows: number) => void;
  onClose?: (sessionId: string) => void;
  onMount?: (sessionId: string, term: Terminal | null) => void;
}

export const TerminalGrid: React.FC<TerminalGridProps> = ({
  tabId,
  projectPath,
  panes,
  onAddPane,
  onSaveSessions,
  onWrite,
  onResize,
  onClose,
  onMount,
}) => {
  // Same layout as Marco 4D.2: 1 pane full size, 2 panes side by side, 3-4 panes in a 2x2 grid.
  const getGridClass = (count: number) => {
    switch (count) {
      case 1:
        return "terminal-grid__panes--1";
      case 2:
        return "terminal-grid__panes--2";
      case 3:
      case 4:
      default:
        return "terminal-grid__panes--4";
    }
  };

  return (
    <div className="terminal-grid">
      {/* Top Controls Toolbar */}
      <div className="terminal-toolbar">
        <div className="terminal-toolbar__group">
          <span className="terminal-toolbar__tab">Aba: {tabId}</span>
          <span className="terminal-toolbar__path" title={projectPath}>
            Raiz: {projectPath}
          </span>
          <span className="badge">{panes.length} / 4 painéis</span>
        </div>
        <div className="terminal-toolbar__group">
          {onSaveSessions && (
            <button
              type="button"
              onClick={() => onSaveSessions(tabId)}
              className="btn btn--sm"
              title="Salvar layout e histórico sanitizado da sessão"
            >
              Salvar sessões
            </button>
          )}
          {onAddPane && panes.length < 4 && (
            <button
              type="button"
              onClick={() => onAddPane(tabId)}
              className="btn btn--sm btn--primary"
              title="Adicionar novo terminal a esta aba"
            >
              + Novo terminal
            </button>
          )}
        </div>
      </div>

      {/* Grid of Panes */}
      <div className={`terminal-grid__panes ${getGridClass(panes.length)}`}>
        {panes.map((pane) => (
          <div key={pane.sessionId} className="terminal-grid__cell">
            <TerminalPane
              {...pane}
              onWrite={onWrite}
              onResize={onResize}
              onClose={onClose}
              onMount={onMount}
            />
          </div>
        ))}
      </div>
    </div>
  );
};
