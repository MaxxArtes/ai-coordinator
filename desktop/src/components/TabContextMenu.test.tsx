import React from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("xterm", async () => ({ Terminal: (await import("../test/fakeXterm")).FakeTerminal }));
vi.mock("xterm-addon-fit", async () => ({ FitAddon: (await import("../test/fakeXterm")).FakeFitAddon }));

import { FakeResizeObserver } from "../test/fakeXterm";
import { TabContextMenu } from "./TabContextMenu";
import { TerminalGrid } from "./TerminalGrid";

globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver;

describe("TabContextMenu and TerminalGrid right-click navigation (4D.4.2)", () => {
  it("opens right-click menu on tab strip showing saved authorized directories and selects one directly", async () => {
    const user = userEvent.setup();
    const onOpenAuthorizedProject = vi.fn();
    const onOpenProjectScreen = vi.fn();

    render(
      <TerminalGrid
        tabId="tab-main"
        projectPath={"C:\\Users\\10864"}
        panes={[{ sessionId: "sess-1", tabId: "tab-main", paneId: 0, kind: "human_terminal", number: 1 }]}
        authorizedProjects={["C:\\Users\\10864", "D:\\ai-coordinator"]}
        onOpenAuthorizedProject={onOpenAuthorizedProject}
        onOpenProjectScreen={onOpenProjectScreen}
        onAddPane={vi.fn()}
        onWrite={vi.fn()}
        onResize={vi.fn()}
      />
    );

    expect(screen.getByRole("button", { name: "Abrir projeto" })).toBeInTheDocument();

    const strip = screen.getByRole("group", { name: "Aba do projeto" });
    fireEvent.contextMenu(strip, { clientX: 120, clientY: 44 });

    const menu = screen.getByRole("menu", { name: "Ações da aba do projeto" });
    expect(menu).toHaveTextContent("Abrir terminal em...");

    const items = screen.getAllByRole("menuitem");
    expect(items[0]).toHaveTextContent("10864 (atual)");
    expect(items[1]).toHaveTextContent("ai-coordinator");
    expect(items[1]).toHaveTextContent("D:\\ai-coordinator");

    await user.click(items[1]);
    expect(onOpenAuthorizedProject).toHaveBeenCalledWith("D:\\ai-coordinator");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("blocks project opening at the 4-pane limit without discarding menu safety, and closes on Escape", async () => {
    const user = userEvent.setup();
    const onSelectProject = vi.fn();
    const onClose = vi.fn();
    render(
      <TabContextMenu
        open
        x={40}
        y={40}
        currentProjectPath="C:\\Users\\10864"
        authorizedProjects={["C:\\Users\\10864", "D:\\ai-coordinator"]}
        atLimit
        onSelectProject={onSelectProject}
        onClose={onClose}
      />
    );

    const items = screen.getAllByRole("menuitem");
    expect(items[1]).toHaveAttribute("aria-disabled", "true");
    await user.click(items[1]);
    expect(onSelectProject).not.toHaveBeenCalled();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("shows explicit empty state when no saved authorized directory exists", () => {
    render(
      <TabContextMenu
        open
        x={20}
        y={20}
        currentProjectPath=""
        authorizedProjects={[]}
        atLimit={false}
        onSelectProject={vi.fn()}
        onOpenOtherProject={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(screen.getByText("Nenhum diretório autorizado salvo")).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Outro diretório/ })).toBeInTheDocument();
  });

  it("opens context menu via Shift+F10 keyboard shortcut and preserves existing sessions on selection", async () => {
    const user = userEvent.setup();
    const onOpenAuthorizedProject = vi.fn();

    render(
      <TerminalGrid
        tabId="tab-main"
        projectPath={"C:\\Users\\10864"}
        panes={[
          { sessionId: "sess-1", tabId: "tab-main", paneId: 0, kind: "human_terminal", number: 1 },
          { sessionId: "sess-2", tabId: "tab-main", paneId: 1, kind: "human_terminal", number: 2 },
        ]}
        authorizedProjects={["C:\\Users\\10864", "D:\\ai-coordinator"]}
        onOpenAuthorizedProject={onOpenAuthorizedProject}
        onAddPane={vi.fn()}
        onWrite={vi.fn()}
        onResize={vi.fn()}
      />
    );

    const strip = screen.getByRole("group", { name: "Aba do projeto" });
    fireEvent.keyDown(strip, { key: "F10", shiftKey: true });

    const menu = screen.getByRole("menu", { name: "Ações da aba do projeto" });
    const items = screen.getAllByRole("menuitem");
    await user.click(items[1]);
    expect(onOpenAuthorizedProject).toHaveBeenCalledWith("D:\\ai-coordinator");
    expect(menu).not.toBeInTheDocument();
    // Both existing terminal panes remain rendered
    expect(document.querySelectorAll("[data-session-id]")).toHaveLength(2);
  });
});

/**
 * R6 da rodada consolidada da 4D.4 (r2): o menu de contexto da aba não pôde ser conferido na homologação (sandbox),
 * então cada exigência tem o seu teste aqui. A lista completa R1 a R7 está no cabeçalho do bloco "regressões dos
 * terminais" de App.views.test.tsx.
 *
 * - Abre onde foi pedido: no ponto do clique direito, logo abaixo da faixa pelo teclado, sempre inteiro na janela.
 * - Shift+F10 e a tecla Menu abrem; setas, Home e End percorrem os itens; Enter executa a ação.
 * - Fecha com Esc e com clique fora, sem executar nada.
 * - O foco volta à aba: ao botão de onde veio ou, depois de um clique direito que deixou o foco no body, à entrada da
 *   faixa (corrigido nesta rodada; antes ficava no body).
 * - Um contextmenu dentro do menu não abre o menu do WebView por cima dele (corrigido nesta rodada).
 */
describe("Menu de contexto da aba: regressão R6 (4D.4 r2)", () => {
  function renderGrid() {
    const props = {
      tabId: "tab-main",
      projectPath: "C:\\Users\\10864",
      panes: [{ sessionId: "sess-1", tabId: "tab-main", paneId: 0, kind: "human_terminal" as const, number: 1 }],
      authorizedProjects: ["C:\\Users\\10864", "D:\\ai-coordinator"],
      onOpenAuthorizedProject: vi.fn(),
      onOpenProjectScreen: vi.fn(),
      onAddPane: vi.fn(),
      onWrite: vi.fn(),
      onResize: vi.fn(),
    };
    render(<TerminalGrid {...props} />);
    return { ...props, strip: screen.getByRole("group", { name: "Aba do projeto" }) };
  }
  const menu = () => screen.getByRole("menu", { name: "Ações da aba do projeto" });

  it("clique direito com o ponteiro abre o menu no ponto clicado; Esc fecha e o foco volta à aba, nunca ao body", async () => {
    const user = userEvent.setup();
    const { strip, onOpenAuthorizedProject } = renderGrid();
    await user.pointer({ keys: "[MouseRight]", target: within(strip).getByText("10864"), coords: { clientX: 120, clientY: 44 } });

    expect(menu().style.left).toBe("120px");
    expect(menu().style.top).toBe("44px");
    expect(document.activeElement).toBe(within(menu()).getAllByRole("menuitem")[0]);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(within(strip).getByRole("button", { name: "Novo terminal" }));
    expect(onOpenAuthorizedProject).not.toHaveBeenCalled();
  });

  it("perto da borda da janela o menu fica inteiro dentro dela", () => {
    const { strip } = renderGrid();
    fireEvent.contextMenu(strip, { clientX: window.innerWidth - 4, clientY: window.innerHeight - 4 });
    expect(menu().style.left).toBe(`${window.innerWidth - 280}px`);
    expect(menu().style.top).toBe(`${window.innerHeight - 240}px`);
  });

  it("Shift+F10 num botão da faixa abre o menu logo abaixo dela; setas, Home e End percorrem os itens; Esc devolve o foco ao botão", async () => {
    const user = userEvent.setup();
    const { strip } = renderGrid();
    vi.spyOn(strip, "getBoundingClientRect").mockReturnValue({
      left: 100, right: 900, top: 8, bottom: 40, width: 800, height: 32, x: 100, y: 8, toJSON: () => ({}),
    } as DOMRect);
    const addButton = within(strip).getByRole("button", { name: "Novo terminal" });
    act(() => addButton.focus());
    await user.keyboard("{Shift>}{F10}{/Shift}");

    expect(menu().style.left).toBe("112px");
    expect(menu().style.top).toBe("42px");
    const items = within(menu()).getAllByRole("menuitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("10864 (atual)");
    expect(items[1]).toHaveTextContent("D:\\ai-coordinator");
    expect(items[2]).toHaveTextContent("Outro diretório (Abrir projeto)...");
    expect(document.activeElement).toBe(items[0]);
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(items[1]);
    await user.keyboard("{End}");
    expect(document.activeElement).toBe(items[2]);
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(items[0]);
    await user.keyboard("{ArrowUp}");
    expect(document.activeElement).toBe(items[2]);
    await user.keyboard("{Home}");
    expect(document.activeElement).toBe(items[0]);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(addButton);
  });

  it("a tecla Menu abre o menu; Enter em 'Outro diretório' chama Abrir projeto uma vez e o foco volta ao botão", async () => {
    const user = userEvent.setup();
    const { strip, onOpenProjectScreen, onOpenAuthorizedProject } = renderGrid();
    const openButton = within(strip).getByRole("button", { name: "Abrir projeto" });
    act(() => openButton.focus());
    fireEvent.keyDown(openButton, { key: "ContextMenu" });
    expect(menu()).toBeInTheDocument();

    await user.keyboard("{End}{Enter}");
    expect(onOpenProjectScreen).toHaveBeenCalledTimes(1);
    expect(onOpenAuthorizedProject).not.toHaveBeenCalled();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(openButton);
  });

  it("Enter num diretório autorizado abre esse diretório uma vez e fecha o menu", async () => {
    const user = userEvent.setup();
    const { strip, onOpenAuthorizedProject } = renderGrid();
    act(() => within(strip).getByRole("button", { name: "Novo terminal" }).focus());
    await user.keyboard("{Shift>}{F10}{/Shift}{ArrowDown}{Enter}");
    expect(onOpenAuthorizedProject).toHaveBeenCalledTimes(1);
    expect(onOpenAuthorizedProject).toHaveBeenCalledWith("D:\\ai-coordinator");
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("clique fora fecha o menu sem executar nada", async () => {
    const user = userEvent.setup();
    const { strip, onOpenAuthorizedProject, onOpenProjectScreen } = renderGrid();
    fireEvent.contextMenu(strip, { clientX: 50, clientY: 30 });
    expect(menu()).toBeInTheDocument();
    await user.click(screen.getByRole("region", { name: "Terminal 01" }));
    expect(screen.queryByRole("menu")).toBeNull();
    expect(onOpenAuthorizedProject).not.toHaveBeenCalled();
    expect(onOpenProjectScreen).not.toHaveBeenCalled();
  });

  it("um contextmenu dentro do menu (tecla Menu, Shift+F10, clique direito) não abre o menu do WebView nem move o menu", () => {
    const { strip } = renderGrid();
    fireEvent.contextMenu(strip, { clientX: 120, clientY: 44 });
    const first = within(menu()).getAllByRole("menuitem")[0];
    expect(fireEvent.contextMenu(first, { clientX: 300, clientY: 300 })).toBe(false); // default prevented
    expect(menu().style.left).toBe("120px");
    expect(menu().style.top).toBe("44px");
  });
});
