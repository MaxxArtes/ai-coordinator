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

type HostSession = { pane_id: number; alive: boolean; exit_code: number | null };

/** Simulated host. Like the fixed manager (F10): a taken slot is never reused; the lowest free one is used instead. */
function host({ preview = false, authorized = true, metricsFail = false } = {}) {
  let sessions = 0;
  const registry = new Map<string, HostSession>();
  invoke.mockImplementation(async (cmd: string, args?: any) => {
    switch (cmd) {
      case "ping":
        if (preview) throw new Error("sem host Tauri");
        return "pong";
      case "get_runtime_status":
        return { core_version: "0.6.0", app_version: "0.6.1-alpha.50", host_os: "windows", is_isolated: true, python_path: "C:\\app\\python.exe" };
      case "profile_get_active":
        return { authorized_projects: authorized ? ["D:\\proj"] : [] };
      case "terminal_create_tab":
      case "terminal_resize":
      case "terminal_write":
        return { ok: true };
      case "terminal_close_session":
        registry.delete(args?.sessionId);
        return { ok: true };
      case "terminal_create_session": {
        const taken = new Set(Array.from(registry.values()).map((r) => r.pane_id));
        const slot = !taken.has(args?.paneId) ? args?.paneId : [0, 1, 2, 3].find((n) => !taken.has(n));
        if (slot === undefined) throw new Error("aba cheia");
        sessions++;
        const id = `sess-${sessions}`;
        registry.set(id, { pane_id: slot, alive: true, exit_code: null });
        return {
          session_id: id,
          pane_id: slot,
          command: ["C:\\Windows\\System32\\cmd.exe", "/k", "SEGREDO"],
          cwd: "D:\\proj",
          pid: 1000 + sessions,
          is_alive: true,
          created_at: "2026-11-17T12:00:00Z",
        };
      }
      case "terminal_list_tabs":
        return {
          tabs: [
            {
              tab_id: "tab-main",
              panes: Object.fromEntries(
                Array.from(registry.entries()).map(([id, r]) => [String(r.pane_id), { session_id: id, is_alive: r.alive, exit_code: r.exit_code }]),
              ),
            },
          ],
        };
      case "terminal_get_output":
        return { raw: "", next_seq: 0 };
      case "get_process_tree_metrics":
        if (metricsFail) throw new Error("processo não encontrado");
        return METRICS;
      case "updater_get_channel_info":
        return { current_version: "0.6.1-alpha.50", channel: "alpha" };
      case "updater_check":
        return { available: false };
      default:
        throw new Error(`comando não simulado: ${cmd}`);
    }
  });
  return registry;
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
    expect(textarea).toHaveAttribute("aria-label", "Terminal 01, 1 de 1. F6 sai do terminal");
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
    expect(trail).toEqual(["main-tab-conversas", "Salvar sessões", "Terminal 01, 1 de 2", "Terminal 02, 2 de 2", "main-tab-conversas"]);
    f6(true);
    expect(where()).toBe("Terminal 02, 2 de 2");

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

  it("F6 reaches the authorization form and comes back to the navigation", async () => {
    host({ authorized: false });
    renderApp();
    const input = await screen.findByLabelText("Caminho do projeto no host");
    (document.activeElement as HTMLElement | null)?.blur();
    const f6 = () => fireEvent.keyDown(document.activeElement ?? document.body, { key: "F6" });
    f6();
    expect(document.activeElement?.id).toBe("main-tab-conversas");
    f6();
    expect(document.activeElement).toBe(input);
    f6();
    expect(document.activeElement?.id).toBe("main-tab-conversas");
  });

  it(
    "a failed metrics reading is shown as 'indisponível', never as the last measurement",
    async () => {
      host({ metricsFail: true });
      renderApp();
      await connected();
      await waitFor(() => expect(bar()).toHaveTextContent("RAM da árvore: indisponível"), { timeout: 3500 });
      expect(bar().textContent).not.toMatch(/\(medida\)/);
    },
    10000,
  );

  it("F10 in the interface: open 3, close the middle one, open 1; unique names, focus never on the body", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    for (let i = 2; i <= 3; i++) {
      await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
      await waitFor(() => expect(xtermLog.created).toHaveLength(i));
    }
    await user.click(screen.getByRole("button", { name: "Fechar Terminal 02" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Terminal 02" })).toBeNull());
    expect(document.activeElement).toHaveAttribute("aria-label", "Terminal 03, 2 de 2");

    await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
    await waitFor(() => expect(screen.getByRole("region", { name: "Terminal 04" })).toBeInTheDocument());
    const creates = invoke.mock.calls.filter((c) => c[0] === "terminal_create_session");
    expect(creates[creates.length - 1][1]).toMatchObject({ paneId: 1 }); // the free slot, not panes.length
    expect(document.activeElement).toHaveAttribute("aria-label", "Terminal 04, 3 de 3");
    const names = screen.getAllByRole("region").map((r) => r.getAttribute("aria-label")).filter((n) => /^Terminal \d/.test(n ?? ""));
    expect(names).toEqual(["Terminal 01", "Terminal 03", "Terminal 04"]);
    expect(bar()).toHaveTextContent("Terminais: 3 de 4");
    expect(document.body.textContent).not.toMatch(/Falha ao criar|SEGREDO/);
    expect(document.body.textContent).toMatch(/cmd\.exe/);
  }, 10000);

  it(
    "F10 in the interface: an exited terminal is shown as ended, keeps its slot, and closing it touches no other",
    async () => {
      const user = userEvent.setup();
      const registry = host();
      renderApp();
      await connected();
      for (let i = 2; i <= 3; i++) {
        await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
        await waitFor(() => expect(xtermLog.created).toHaveLength(i));
      }
      registry.set("sess-3", { pane_id: 2, alive: false, exit_code: 0 }); // `exit` typed in the 3rd terminal
      const third = () => screen.getByRole("region", { name: "Terminal 03" });
      await waitFor(() => expect(third()).toHaveTextContent("encerrado, código 0"), { timeout: 3500 });
      expect(bar()).toHaveTextContent("Terminais: 3 de 4 (2 ativos)");

      const writes = count("terminal_write");
      fireEvent.keyDown(xtermLog.created[2].textarea!, { key: "Enter" });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(count("terminal_write")).toBe(writes); // nothing is sent to an ended process

      await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
      await waitFor(() => expect(screen.getByRole("region", { name: "Terminal 04" })).toBeInTheDocument());
      const creates = invoke.mock.calls.filter((c) => c[0] === "terminal_create_session");
      expect(creates[creates.length - 1][1]).toMatchObject({ paneId: 3 }); // slot 2 is still held by the ended one

      await user.click(screen.getByRole("button", { name: "Fechar Terminal 03" }));
      await waitFor(() => expect(screen.queryByRole("region", { name: "Terminal 03" })).toBeNull());
      expect(invoke.mock.calls.filter((c) => c[0] === "terminal_close_session").map((c) => c[1])).toEqual([{ sessionId: "sess-3" }]);
      expect(Array.from(registry.keys()).sort()).toEqual(["sess-1", "sess-2", "sess-4"]);
      await waitFor(() => expect(bar()).toHaveTextContent("Terminais: 3 de 4 (3 ativos)"), { timeout: 3500 });
    },
    12000,
  );

  it("a failed close keeps the pane: no process is left without a pane to close it", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      if (cmd === "terminal_close_session") throw new Error("IPC parado");
      return original(cmd, args);
    });
    await user.click(screen.getByRole("button", { name: "Fechar Terminal 01" }));
    expect(await screen.findByText(/O terminal continua aberto/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Terminal 01" })).toBeInTheDocument();
    expect(xtermLog.disposed).toBe(0);
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
