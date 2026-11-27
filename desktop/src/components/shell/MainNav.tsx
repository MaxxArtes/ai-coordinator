import React, { useCallback, useRef } from "react";

/**
 * Main navigation (Etapa 5): Conversas, Monitoramento and Missões as horizontal tabs.
 * - Arrow keys with wrap-around, Home and End; roving tabIndex and automatic activation (same pattern as the
 *   Configurações tabs).
 * - Conversas shows the real number of terminals.
 * - The active tab is the F6 entry of the header region.
 */
export type MainViewId = "conversas" | "monitoramento" | "missoes";

export const MAIN_VIEWS: ReadonlyArray<{ id: MainViewId; label: string; planned: boolean }> = [
  { id: "conversas", label: "Conversas", planned: false },
  { id: "monitoramento", label: "Monitoramento", planned: false },
  { id: "missoes", label: "Missões", planned: false },
];

export const mainViewTabId = (id: MainViewId): string => `main-tab-${id}`;
export const mainViewPanelId = (id: MainViewId): string => `main-view-${id}`;

export interface MainNavProps {
  active: MainViewId;
  onSelect: (id: MainViewId) => void;
  /** Number of open terminals, shown on Conversas. */
  paneCount: number;
}

export const MainNav: React.FC<MainNavProps> = ({ active, onSelect, paneCount }) => {
  const tabRefs = useRef<Partial<Record<MainViewId, HTMLButtonElement | null>>>({});

  const select = useCallback(
    (id: MainViewId) => {
      onSelect(id);
      tabRefs.current[id]?.focus();
    },
    [onSelect]
  );

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const last = MAIN_VIEWS.length - 1;
    let next: number | null = null;
    if (event.key === "ArrowRight") next = index === last ? 0 : index + 1;
    else if (event.key === "ArrowLeft") next = index === 0 ? last : index - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = last;
    if (next === null) return;
    event.preventDefault();
    select(MAIN_VIEWS[next].id);
  };

  return (
    <nav className="main-nav" aria-label="Seções principais">
      <div role="tablist" aria-orientation="horizontal" aria-label="Seções" className="main-nav__list">
        {MAIN_VIEWS.map((view, index) => {
          const selected = view.id === active;
          return (
            <button
              key={view.id}
              ref={(el) => {
                tabRefs.current[view.id] = el;
              }}
              type="button"
              role="tab"
              id={mainViewTabId(view.id)}
              aria-selected={selected}
              aria-controls={mainViewPanelId(view.id)}
              tabIndex={selected ? 0 : -1}
              data-f6-entry={selected ? "" : undefined}
              className="main-nav__tab"
              onClick={() => select(view.id)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              <span>{view.label}</span>
              {view.id === "conversas" && (
                <>
                  <span className="main-nav__count" aria-hidden="true">
                    {paneCount}
                  </span>
                  <span className="sr-only">
                    , {paneCount} {paneCount === 1 ? "terminal" : "terminais"}
                  </span>
                </>
              )}
              {view.planned && <span className="sr-only"> (planejado)</span>}
            </button>
          );
        })}
      </div>
    </nav>
  );
};
