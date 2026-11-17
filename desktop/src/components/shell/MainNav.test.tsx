/**
 * Main navigation and the stacked views (Etapa 5, decisions 4 and 9).
 */
import React, { useState } from "react";
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MainNav, MAIN_VIEWS, type MainViewId } from "./MainNav";
import { MainViews, PLANNED_CONTENT } from "./PlannedView";

const Harness: React.FC<{ paneCount?: number; initial?: MainViewId }> = ({ paneCount = 2, initial = "conversas" }) => {
  const [active, setActive] = useState<MainViewId>(initial);
  return (
    <>
      <MainNav active={active} onSelect={setActive} paneCount={paneCount} />
      <MainViews active={active} conversas={<p>conteúdo de Conversas</p>} />
    </>
  );
};

const tabs = () => screen.getAllByRole("tab");

describe("MainNav", () => {
  it("one navigation, one tablist and the three tabs in order", () => {
    render(<Harness />);
    const nav = screen.getByRole("navigation", { name: "Seções principais" });
    expect(within(nav).getAllByRole("tablist")).toHaveLength(1);
    expect(tabs().map((t) => t.textContent?.replace(/[,(].*$/, "").replace(/\d+$/, "").trim())).toEqual([
      "Conversas",
      "Monitoramento",
      "Missões",
    ]);
    expect(MAIN_VIEWS.map((v) => v.id)).toEqual(["conversas", "monitoramento", "missoes"]);
  });

  it("only the active tab is selected, tabbable and the F6 entry", () => {
    render(<Harness />);
    const [conversas, ...others] = tabs();
    expect(conversas).toHaveAttribute("aria-selected", "true");
    expect(conversas).toHaveAttribute("tabindex", "0");
    expect(conversas).toHaveAttribute("data-f6-entry");
    for (const tab of others) {
      expect(tab).toHaveAttribute("aria-selected", "false");
      expect(tab).toHaveAttribute("tabindex", "-1");
      expect(tab).not.toHaveAttribute("data-f6-entry");
    }
  });

  it("arrow keys wrap around, Home and End jump, and the selection follows the focus", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    tabs()[0].focus();
    const steps: Array<[string, string]> = [
      ["{ArrowRight}", "main-tab-monitoramento"],
      ["{ArrowRight}", "main-tab-missoes"],
      ["{ArrowRight}", "main-tab-conversas"],
      ["{ArrowLeft}", "main-tab-missoes"],
      ["{Home}", "main-tab-conversas"],
      ["{End}", "main-tab-missoes"],
    ];
    for (const [key, id] of steps) {
      await user.keyboard(key);
      expect(document.activeElement?.id).toBe(id);
      expect(document.getElementById(id)).toHaveAttribute("aria-selected", "true");
    }
  });

  it("tabs and panels point to each other", () => {
    render(<Harness />);
    for (const tab of tabs()) {
      const panel = document.getElementById(tab.getAttribute("aria-controls")!);
      expect(panel).toHaveAttribute("role", "tabpanel");
      expect(panel).toHaveAttribute("aria-labelledby", tab.id);
    }
  });

  it.each([0, 1, 2, 3, 4])("the Conversas counter shows the real number of terminals (%i)", (n) => {
    render(<Harness paneCount={n} />);
    // The visible number is aria-hidden: the name says it once, in words.
    const conversas = screen.getByRole("tab", { name: new RegExp(`^Conversas\\s*,\\s*${n} ${n === 1 ? "terminal" : "terminais"}$`) });
    expect(conversas.querySelector(".main-nav__count")).toHaveTextContent(String(n));
  });

  it("Monitoramento and Missões are announced as planned", () => {
    render(<Harness />);
    expect(screen.getByRole("tab", { name: /^Monitoramento\s*\(planejado\)$/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Missões\s*\(planejado\)$/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /^Conversas/ }).textContent).not.toMatch(/planejado/);
  });
});

describe("MainViews", () => {
  it("an inactive Conversas keeps its box: inert and invisible, never hidden", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const conversas = document.getElementById("main-view-conversas")!;
    expect(conversas).not.toHaveAttribute("inert");
    expect(conversas).not.toHaveClass("is-invisible");

    await user.click(screen.getByRole("tab", { name: /^Monitoramento/ }));
    expect(conversas).toHaveAttribute("inert");
    expect(conversas).toHaveClass("is-invisible");
    expect(conversas).not.toHaveAttribute("hidden");
    expect(screen.getByText("conteúdo de Conversas")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: /^Conversas/ }));
    expect(conversas).not.toHaveAttribute("inert");
    expect(conversas).not.toHaveClass("is-invisible");
  });

  it("only the temporary views are F6 regions of their own; Conversas holds the strip and pane regions", () => {
    render(<Harness />);
    const conversas = document.getElementById("main-view-conversas")!;
    expect(conversas).not.toHaveAttribute("tabindex");
    expect(conversas).not.toHaveAttribute("data-f6-region");
    for (const id of ["main-view-monitoramento", "main-view-missoes"]) {
      const view = document.getElementById(id)!;
      expect(view).toHaveAttribute("tabindex", "0");
      expect(view).toHaveAttribute("data-f6-region", "view");
      expect(view).toHaveAttribute("data-f6-entry");
    }
  });

  it.each([
    ["monitoramento", "Monitoramento", "4D.4"],
    ["missoes", "Missões", "4D.5"],
  ] as const)("the %s screen is a clearly identified temporary screen, with no data and no control", async (id, label, milestone) => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("tab", { name: new RegExp(`^${label}`) }));
    const view = document.getElementById(`main-view-${id}`)!;
    expect(view).not.toHaveAttribute("hidden");
    const badges = Array.from(view.querySelectorAll(".badge")).map((b) => b.textContent);
    expect(badges).toEqual(["Tela temporária", "planejado", milestone]);
    expect(within(view).getByRole("heading", { level: 2, name: label })).toBeInTheDocument();
    expect(view.querySelectorAll("button, a, input, select, textarea, progress, meter, [role=progressbar], [role=meter]")).toHaveLength(0);
    expect((view.textContent ?? "").replace(/4D\.\d+/g, "")).not.toMatch(/\d/);
    expect(view).toHaveTextContent("Até lá, esta tela não mostra dados nem tem controles.");
  });

  it("the approved texts (operator, 17/11)", () => {
    expect(PLANNED_CONTENT.monitoramento.text).toBe(
      "Vai mostrar os recursos usados pela árvore de processos do aplicativo, as cotas e os custos dos provedores, " +
        "sempre com a origem de cada medida. Até lá, esta tela não mostra dados nem tem controles.",
    );
    expect(PLANNED_CONTENT.missoes.text).toBe(
      "Vai acompanhar as operações reais do Core: o grafo de cada missão, as tarefas que esperam decisão humana e os " +
        "checkpoints de retomada. Até lá, esta tela não mostra dados nem tem controles.",
    );
  });
});
