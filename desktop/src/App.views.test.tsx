/**
 * The real App with a simulated host and xterm (Etapa 5): switching views keeps the terminals, the connection change
 * is announced once, the status bar and Sobre show only measured values, and the preview invents nothing.
 */
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.hoisted(() => vi.fn());
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));
vi.mock("xterm", async () => ({ Terminal: (await import("./test/fakeXterm")).FakeTerminal }));
vi.mock("xterm-addon-fit", async () => ({ FitAddon: (await import("./test/fakeXterm")).FakeFitAddon }));

import { App } from "./App";
import { FakeResizeObserver, resetXterm, xtermLog } from "./test/fakeXterm";

const METRICS = { host_pid: 4242, host_ram_bytes: 61_027_123, total_tree_ram_bytes: 134_637_159, cpu_usage_percent: 37.5 };

function host({ preview = false } = {}) {
  let sessions = 0;
  invoke.mockImplementation(async (cmd: string) => {
    switch (cmd) {
      case "ping":
        if (preview) throw new Error("sem host Tauri");
        return "pong";
      case "get_runtime_status":
        return { core_version: "0.6.0", app_version: "0.6.1-alpha.50", host_os: "windows", is_isolated: true, python_path: "C:\\app\\python.exe" };
      case "profile_get_active":
        return { authorized_projects: ["D:\\proj"] };
      case "terminal_create_tab":
      case "terminal_resize":
      case "terminal_write":
      case "terminal_close_session":
        return { ok: true };
      case "terminal_create_session":
        sessions++;
        return { session_id: `sess-${sessions}` };
      case "terminal_get_output":
        return { raw: "", next_seq: 0 };
      case "get_process_tree_metrics":
        return METRICS;
      case "updater_get_channel_info":
        return { current_version: "0.6.1-alpha.50", channel: "alpha" };
      case "updater_check":
        return { available: false };
      default:
        throw new Error(`comando não simulado: ${cmd}`);
    }
  });
}

const count = (cmd: string) => invoke.mock.calls.filter((c) => c[0] === cmd).length;

function renderApp() {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  return render(<App />, { container: root });
}

async function connected() {
  await screen.findByText("Core conectado");
  await waitFor(() => expect(xtermLog.created).toHaveLength(1));
}

const bar = () => screen.getByRole("contentinfo", { name: "Barra de status" });

beforeEach(() => {
  invoke.mockReset();
  resetXterm();
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App views", () => {
  it.each([1, 4])(
    "keeps %i terminal(s) through three round trips across the views and Configurações",
    async (n) => {
      const user = userEvent.setup();
      host();
      renderApp();
      await connected();
      for (let i = 1; i < n; i++) {
        await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
        await waitFor(() => expect(xtermLog.created).toHaveLength(i + 1));
      }
      const mounts = Array.from(document.querySelectorAll(".terminal-pane__mount"));
      expect(mounts).toHaveLength(n);
      const resizes = count("terminal_resize");
      const conversas = document.getElementById("main-view-conversas")!;

      for (let round = 0; round < 3; round++) {
        await user.click(screen.getByRole("tab", { name: /^Monitoramento/ }));
        expect(conversas).toHaveAttribute("inert");
        expect(conversas).toHaveClass("is-invisible");
        expect(conversas).not.toHaveAttribute("hidden");
        await user.keyboard("{ArrowRight}");
        expect(screen.getByRole("tab", { name: /^Missões/ })).toHaveAttribute("aria-selected", "true");
        await user.keyboard("{ArrowRight}");
        expect(conversas).not.toHaveAttribute("inert");
      }
      await user.click(screen.getByRole("button", { name: "Configurações" }));
      expect(await screen.findByRole("dialog")).toBeInTheDocument();
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

      expect(count("ping")).toBe(1);
      expect(count("terminal_create_tab")).toBe(1);
      expect(count("terminal_create_session")).toBe(n);
      expect(count("terminal_close_session")).toBe(0);
      expect(count("terminal_resize")).toBe(resizes);
      expect(xtermLog.created).toHaveLength(n);
      expect(xtermLog.disposed).toBe(0);
      const after = Array.from(document.querySelectorAll(".terminal-pane__mount"));
      after.forEach((node, i) => expect(node).toBe(mounts[i]));
    },
    15000,
  );

  it("announces the connection change once, and not again on view switches", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    const notice = screen.getByTestId("connection-notice");
    expect(notice).toHaveAttribute("role", "status");
    await waitFor(() => expect(notice).toHaveTextContent("Conexão: Core conectado"));
    await user.click(screen.getByRole("tab", { name: /^Missões/ }));
    await user.click(screen.getByRole("tab", { name: /^Conversas/ }));
    expect(notice).toHaveTextContent(/^Conexão: Core conectado$/);
  });

  it(
    "shows the measured RAM and CPU 'indisponível' even when the host sends a CPU number",
    async () => {
      host();
      renderApp();
      await connected();
      expect(bar()).toHaveTextContent("RAM da árvore: aguardando");
      await waitFor(() => expect(bar()).toHaveTextContent("RAM da árvore: 128,4 MB (medida)"), { timeout: 3500 });
      expect(bar()).toHaveTextContent("CPU: indisponível");
      expect(bar()).toHaveTextContent("Core v0.6.0 · conectado");
      expect(bar()).toHaveTextContent("Terminais: 1 de 4");
      expect(document.body.textContent).not.toMatch(/37[,.]5/);
      expect(screen.getByText("v0.6.1-alpha.50")).toBeInTheDocument();
    },
    10000,
  );

  it(
    "Sobre shows the PID and RAM of the main process once measured, never the CPU",
    async () => {
      const user = userEvent.setup();
      host();
      renderApp();
      await connected();
      await waitFor(() => expect(bar()).toHaveTextContent("(medida)"), { timeout: 3500 });
      await user.click(screen.getByRole("button", { name: "Configurações" }));
      const dialog = await screen.findByRole("dialog");
      await user.click(within(dialog).getByRole("tab", { name: /^Sobre/ }));
      const pid = within(dialog).getByText("PID do processo principal");
      expect(pid.nextElementSibling).toHaveTextContent("4242");
      const ram = within(dialog).getByText("RAM do processo principal");
      expect(ram.nextElementSibling).toHaveTextContent("58,2 MB (medida)");
      expect(dialog.textContent).not.toMatch(/CPU|37[,.]5|\bHost\b/);
    },
    10000,
  );

  it("Tab, Esc, Shift+Tab and Ctrl+C typed in a terminal reach terminal_write with the exact bytes; F6 sends nothing", async () => {
    host();
    renderApp();
    await connected();
    const textarea = xtermLog.created[0].textarea!;
    expect(textarea).toHaveAttribute("aria-label", "Terminal 1 de 1. F6 sai do terminal");
    textarea.focus();
    const keys: KeyboardEventInit[] = [{ key: "Tab" }, { key: "Escape" }, { key: "Tab", shiftKey: true }, { key: "c", ctrlKey: true }];
    for (const [i, init] of keys.entries()) {
      fireEvent.keyDown(textarea, init);
      await waitFor(() => expect(count("terminal_write")).toBe(i + 1));
    }
    expect(document.activeElement).toBe(textarea);
    fireEvent.keyDown(textarea, { key: "F6" });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(count("terminal_write")).toBe(4);
    expect(invoke.mock.calls.filter((c) => c[0] === "terminal_write").map((c) => c[1])).toEqual([
      { sessionId: "sess-1", data: "\t" },
      { sessionId: "sess-1", data: "\x1b" },
      { sessionId: "sess-1", data: "\x1b[Z" },
      { sessionId: "sess-1", data: "\x03" },
    ]);
    expect(document.activeElement?.id).toBe("main-tab-conversas"); // F6 left the only terminal for the header
  });

  it("F6 on the real App: header → strip → each terminal → header; in Monitoramento: tab → temporary view", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
    await waitFor(() => expect(xtermLog.created).toHaveLength(2));
    (document.activeElement as HTMLElement | null)?.blur();

    const where = () => {
      const el = document.activeElement as HTMLElement;
      return el.id || el.getAttribute("aria-label") || el.textContent?.trim() || el.tagName;
    };
    const f6 = (shiftKey = false) => fireEvent.keyDown(document.activeElement ?? document.body, { key: "F6", shiftKey });
    const trail: string[] = [];
    for (let i = 0; i < 5; i++) {
      f6();
      trail.push(where());
    }
    expect(trail).toEqual(["main-tab-conversas", "Salvar sessões", "Terminal 1 de 2", "Terminal 2 de 2", "main-tab-conversas"]);
    f6(true);
    expect(where()).toBe("Terminal 2 de 2");

    await user.click(screen.getByRole("tab", { name: /^Monitoramento/ }));
    f6();
    expect(where()).toBe("main-view-monitoramento");
    f6();
    expect(where()).toBe("main-tab-monitoramento");
    f6(true);
    expect(where()).toBe("main-view-monitoramento");

    // Reloading would lose every terminal: F5 and Ctrl+R are cancelled.
    expect(fireEvent.keyDown(document.body, { key: "F5" })).toBe(false);
    expect(fireEvent.keyDown(document.body, { key: "r", code: "KeyR", ctrlKey: true })).toBe(false);
    expect(xtermLog.created).toHaveLength(2);
  });

  it("the preview claims no Core, no connection and no measurement", async () => {
    host({ preview: true });
    renderApp();
    await waitFor(() => expect(xtermLog.created).toHaveLength(1));
    const pill = document.querySelector(".status-pill")!;
    expect(pill).toHaveTextContent("Preview: sem Core");
    expect(bar()).toHaveTextContent("Preview: sem Core");
    expect(bar()).toHaveTextContent("RAM da árvore: indisponível (preview)");
    expect(bar()).toHaveTextContent("Terminais: 1 de 4 (preview)");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(document.body.textContent).not.toMatch(/conectad|v0\.6\.0/i);
  });
});
