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
  const getGridClass = (count: number) => {
    switch (count) {
      case 1:
        return "grid-cols-1 grid-rows-1";
      case 2:
        return "grid-cols-2 grid-rows-1";
      case 3:
      case 4:
      default:
        return "grid-cols-2 grid-rows-2";
    }
  };

  return (
    <div className="flex flex-col h-full w-full bg-[#11111b]">
      {/* Top Controls Toolbar */}
      <div className="flex items-center justify-between px-4 py-2 bg-[#181825] border-b border-gray-800 text-sm">
        <div className="flex items-center space-x-3">
          <span className="font-semibold text-gray-200">Aba: {tabId}</span>
          <span className="text-xs font-mono text-gray-400 truncate max-w-md">
            Raiz: {projectPath}
          </span>
          <span className="text-xs px-2 py-0.5 rounded bg-gray-800 text-gray-300">
            {panes.length} / 4 Painéis
          </span>
        </div>
        <div className="flex items-center space-x-2">
          {onSaveSessions && (
            <button
              onClick={() => onSaveSessions(tabId)}
              className="px-3 py-1 bg-gray-700 hover:bg-gray-600 text-gray-200 text-xs rounded transition focus:ring-1 focus:ring-blue-400"
              title="Salvar layout e histórico sanitizado da sessão"
            >
              Salvar Sessões
            </button>
          )}
          {onAddPane && panes.length < 4 && (
            <button
              onClick={() => onAddPane(tabId)}
              className="px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white text-xs rounded font-medium transition focus:ring-1 focus:ring-blue-400"
              title="Adicionar novo terminal a esta aba"
            >
              + Novo Terminal
            </button>
          )}
        </div>
      </div>

      {/* Grid of Panes */}
      <div className={`grid flex-1 p-2 gap-2 h-full overflow-hidden ${getGridClass(panes.length)}`}>
        {panes.map((pane) => (
          <div key={pane.sessionId} className="h-full w-full min-h-0 min-w-0">
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
