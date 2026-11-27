import React, { useLayoutEffect, useRef } from "react";
import { mainViewPanelId, mainViewTabId, type MainViewId } from "./MainNav";
import { MissionsView } from "./MissionsView";

/**
 * The three main views stacked in one grid cell (Etapa 5).
 * - Conversas stays mounted when another view is active: hidden by visibility (.is-invisible) and made inert, so its
 *   terminals are neither recreated nor resized (never display:none).
 * - Monitoramento can render its approved placeholder when its content is unavailable.
 */
export type PlannedViewId = "monitoramento";

export const PLANNED_CONTENT: Record<PlannedViewId, { title: string; milestone: string; text: string }> = {
  monitoramento: {
    title: "Monitoramento",
    milestone: "4D.4",
    text:
      "Vai mostrar os recursos usados pela árvore de processos do aplicativo, as cotas e os custos dos provedores, " +
      "sempre com a origem de cada medida. Até lá, esta tela não mostra dados nem tem controles.",
  },
};

export const PlannedView: React.FC<{ view: PlannedViewId; active: boolean }> = ({ view, active }) => {
  const content = PLANNED_CONTENT[view];
  return (
    <div
      role="tabpanel"
      id={mainViewPanelId(view)}
      aria-labelledby={mainViewTabId(view)}
      hidden={!active}
      tabIndex={0}
      className="app-view planned-view"
      data-f6-region="view"
      data-f6-entry=""
    >
      <div className="planned-view__card">
        <div className="planned-view__badges">
          <span className="badge badge--temporary">Tela temporária</span>
          <span className="badge badge--planned">planejado</span>
          <span className="badge badge--milestone" title={`Implementação prevista para a fase ${content.milestone}`}>
            {content.milestone}
          </span>
        </div>
        <h2 className="planned-view__title">{content.title}</h2>
        <p className="planned-view__text">{content.text}</p>
      </div>
    </div>
  );
};

export interface MainViewsProps {
  active: MainViewId;
  /** Content of Conversas: the terminal grid, or the authorization and startup screens. */
  conversas: React.ReactNode;
  /** Content of Monitoramento (4D.4.2): renders in place of the temporary screen when provided. */
  monitoramento?: React.ReactNode;
  missoes?: React.ReactNode;
}

export const MainViews: React.FC<MainViewsProps> = ({ active, conversas, monitoramento, missoes }) => {
  const conversasRef = useRef<HTMLDivElement>(null);
  const conversasActive = active === "conversas";

  // jsdom and React 18 do not know the inert property: the attribute is set directly, before the next paint.
  useLayoutEffect(() => {
    const el = conversasRef.current;
    if (!el) return;
    if (conversasActive) el.removeAttribute("inert");
    else el.setAttribute("inert", "");
  }, [conversasActive]);

  return (
    <main className="app__main">
      <div
        ref={conversasRef}
        role="tabpanel"
        id={mainViewPanelId("conversas")}
        aria-labelledby={mainViewTabId("conversas")}
        className={conversasActive ? "app-view" : "app-view is-invisible"}
      >
        {conversas}
      </div>
      {monitoramento ?? <PlannedView view="monitoramento" active={active === "monitoramento"} />}
      {missoes ?? <MissionsView active={active === "missoes"} />}
    </main>
  );
};
