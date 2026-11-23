import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
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
});
