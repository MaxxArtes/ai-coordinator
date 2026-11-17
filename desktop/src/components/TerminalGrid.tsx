/**
 * Responsive Quad Terminal Grid for AI Coordinator Desktop (Marco 4D.2.3).
 *
 * Supports up to 4 simultaneous panes per tab:
 * - 1 Pane: Full width/height.
 * - 2 Panes: 50% / 50% split.
 * - 3-4 Panes: 2x2 grid view with independent focus and streaming.
 * - Pure data binding: panes contain metadata only; handlers are dynamically bound at render.
 *
 * Marco 4D.3, Etapa 5: the tab strip (project folder, layout, Salvar sessões, Novo terminal) replaces the old
 * "Aba: / Raiz:" toolbar and is the F6 region "strip"; every pane gets its position ("Terminal N de M").
 */

import React from "react";
import { Terminal } from "xterm";
import { TerminalPane, TerminalPaneData } from "./TerminalPane";
import { Icon } from "./ui/Icon";

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

// Same layout as Marco 4D.2: 1 pane full size, 2 panes side by side, 3-4 panes in a 2x2 grid.
export const getGridClass = (count: number): string => {
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

const LAYOUT_LABELS: Record<string, string> = {
  "terminal-grid__panes--1": "Painel único",
  "terminal-grid__panes--2": "Lado a lado",
  "terminal-grid__panes--4": "Grade 2×2",
};

/** The layout in words, by the same rule as the grid class; empty without a terminal. */
export const getLayoutLabel = (count: number): string => (count < 1 ? "" : LAYOUT_LABELS[getGridClass(count)]);

/** Last part of a Windows or POSIX path ("D:\\ai-coordinator" → "ai-coordinator"); "" when unknown. */
export const projectFolderName = (path: string): string => path.split(/[\\/]+/).filter(Boolean).pop() ?? "";

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
  const folder = projectFolderName(projectPath);
  const layout = getLayoutLabel(panes.length);
  const canAdd = !!onAddPane && panes.length < 4;

  return (
    <div className="terminal-grid">
      {/* Tab strip: F6 region "strip"; its entry is the first button */}
      <div className="tab-strip" data-f6-region="strip" role="group" aria-label="Aba do projeto">
        <span className="tab-strip__label" title={projectPath || undefined}>
          <Icon name="terminal" size="sm" />
          {folder && <span className="tab-strip__name">{folder}</span>}
        </span>
        <div className="tab-strip__actions">
          {layout && <span className="tab-strip__layout">{layout}</span>}
          {onSaveSessions && (
            <button
              type="button"
              onClick={() => onSaveSessions(tabId)}
              className="btn btn--sm"
              title="Salvar layout e histórico sanitizado da sessão"
              data-f6-entry=""
            >
              <Icon name="save" size="sm" />
              Salvar sessões
            </button>
          )}
          {canAdd && (
            <button
              type="button"
              onClick={() => onAddPane!(tabId)}
              className="btn btn--sm btn--primary"
              title="Adicionar novo terminal a esta aba"
              data-f6-entry={onSaveSessions ? undefined : ""}
            >
              <Icon name="plus" size="sm" />
              Novo terminal
            </button>
          )}
        </div>
      </div>

      {/* Grid of Panes */}
      <div className={`terminal-grid__panes ${getGridClass(panes.length)}`}>
        {panes.map((pane, index) => (
          <div key={pane.sessionId} className="terminal-grid__cell">
            <TerminalPane
              {...pane}
              position={index + 1}
              total={panes.length}
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
