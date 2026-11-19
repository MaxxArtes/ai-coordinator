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

import { App, PREFERENCES_DEADLINE_MS } from "./App";
import { FakeResizeObserver, resetXterm, xtermLog } from "./test/fakeXterm";
import { CONFIRM_ARM_DELAY_MS, armingClock } from "./components/ui/InlineConfirmation";

const METRICS = { host_pid: 4242, host_ram_bytes: 61_027_123, total_tree_ram_bytes: 134_637_159, cpu_usage_percent: 37.5 };

function appSettings(overrides: Record<string, any> = {}): any {
  const values: Record<string, any> = {
    perfil: { display_name: null }, geral: { open_last_project_on_start: true },
    aparencia: { stimulation: "standard", density: "comfortable" },
    terminal: { font_size: 13, cursor_blink: true, scrollback_lines: 1000 }, coordinator: {},
  };
  for (const [kind, value] of Object.entries(overrides)) values[kind] = { ...values[kind], ...value };
  return { schema_version: 1, stored_schema_version: 1, unsupported_categories: {}, categories: Object.fromEntries(
    Object.entries(values).map(([kind, v]) => [kind, { values: v, saved_fields: Object.keys(v), validated: true, applied: false, application: kind === "coordinator" ? "unsupported" : "interface" }])
  ) };
}

type HostSession = { pane_id: number; alive: boolean; exit_code: number | null; tab?: string; cwd?: string };

/** Directories the simulated host treats as broad (L5): a disk root, a share root and a home folder. */
const BROAD = new Set(["C:\\", "\\\\servidor\\pasta", "C:\\Users\\ana", "C:\\Users\\10864"]);
const BROAD_MESSAGE = "Este diretório dá acesso amplo (raiz do disco ou pasta pessoal inteira). Confirme explicitamente para autorizar.";
/** The transport failures of the Tauri host (L2), as call_structured sends them. */
const TIMEOUT_30 = {
  code: -32007,
  message: "O Core não respondeu em 30 s. A operação pode ter sido concluída: confira antes de repetir.",
  data: { field: "ipc", reason: "timeout" },
};
const DISCONNECTED = {
  code: -32007,
  message: "O Core encerrou a comunicação. Reinicie o aplicativo.",
  data: { field: "ipc", reason: "disconnected" },
};

interface HostOptions {
  preview?: boolean;
  authorized?: boolean;
  metricsFail?: boolean;
  /** settings_get answer (appSettings() by default). */
  settings?: Record<string, any>;
  /** Fields merged into the profile_get_active answer. */
  profile?: Record<string, any>;
  /** Commands that reject with the given error. */
  fail?: Record<string, unknown>;
  /** Commands answered by a custom handler (before everything else). */
  extra?: Record<string, (args?: any) => unknown>;
}

/**
 * Simulated host. Like the fixed manager (F10): a taken slot is never reused; the lowest free one is used instead.
 * Like the real one, a tab id cannot be created twice, and a revocation is refused while a session is registered there.
 */
function host({ preview = false, authorized = true, metricsFail = false, settings, profile = {}, fail = {}, extra = {} }: HostOptions = {}) {
  let sessions = 0;
  const registry = new Map<string, HostSession>();
  const tabs = new Map<string, string>();
  const state = {
    settings: settings ?? appSettings(),
    authorized: (profile.authorized_projects as string[] | undefined) ?? (authorized ? ["C:\\Users\\10864", "D:\\proj"] : []),
  };
  invoke.mockImplementation(async (cmd: string, args?: any) => {
    if (cmd in extra) return extra[cmd](args);
    if (cmd in fail) throw fail[cmd];
    switch (cmd) {
      case "ping":
        if (preview) throw new Error("sem host Tauri");
        return "pong";
      case "get_runtime_status":
        return { core_version: "0.6.0", app_version: "0.6.1-alpha.50", host_os: "windows", is_isolated: true, python_path: "C:\\app\\python.exe" };
      case "terminal_list_interpreters":
        return { interpreters: [
          { id: "cmd", label: "CMD" },
          { id: "powershell", label: "PowerShell 7" },
          { id: "bash_git", label: "Bash (Git Bash)" },
        ] };
      case "terminal_validate_interpreter":
        if (!state.authorized.includes(args.cwd)) throw new Error("Project not authorized or session invalid");
        return { id: args.interpreterId, label: args.interpreterId === "cmd" ? "CMD" : "PowerShell 7" };
      case "profile_get_active":
        return { name: "default", display_name: null, last_project: null, ...profile, authorized_projects: [...state.authorized] };
      case "settings_get":
        return state.settings;
      case "settings_update": {
        const category = state.settings.categories[args.category];
        state.settings = {
          ...state.settings,
          categories: { ...state.settings.categories, [args.category]: { ...category, values: { ...category.values, ...args.values } } },
        };
        return state.settings;
      }
      case "profile_remember_project":
        return { remembered: true, last_project: args?.projectPath };
      case "profile_authorize_project":
        // Like the host (L5): a broad directory needs confirm_broad, which only true satisfies.
        if (BROAD.has(args.projectPath) && args.confirmBroad !== true) {
          throw { code: -32602, message: BROAD_MESSAGE, data: { field: "project_path", reason: "broad_scope" } };
        }
        if (!state.authorized.includes(args.projectPath)) state.authorized.push(args.projectPath);
        return { success: true, profile_name: "default", project_path: args.projectPath, authorized_projects: [...state.authorized] };
      case "security_status":
        return {
          authorized_projects: [...state.authorized],
          broad_projects: state.authorized.filter((p) => BROAD.has(p)),
          policy: { project_fencing: true, human_terminal: "operator_permissions" },
        };
      case "profile_revoke_project": {
        if (Array.from(registry.values()).some((s) => s.cwd === args.projectPath)) {
          throw { code: -32003, message: "Feche os terminais deste diretório antes de revogar a autorização.", data: { field: "project_path", reason: "active_sessions" } };
        }
        const before = state.authorized.length;
        state.authorized = state.authorized.filter((p) => p !== args.projectPath);
        return { revoked: state.authorized.length < before, authorized_projects: [...state.authorized], last_project: null };
      }
      case "terminal_create_tab":
        if (tabs.has(args.tabId)) throw new Error(`Tab with ID '${args.tabId}' already exists`);
        if (!state.authorized.includes(args.projectPath)) throw new Error("Project not authorized or session invalid");
        tabs.set(args.tabId, args.projectPath);
        return { tab_id: args.tabId };
      case "terminal_resize":
      case "terminal_write":
        return { ok: true };
      case "terminal_close_session":
        registry.delete(args?.sessionId);
        return { closed: true };
      case "terminal_create_session": {
        if (!tabs.has(args?.tabId)) throw new Error(`Tab '${args?.tabId}' does not exist`);
        const inTab = Array.from(registry.values()).filter((r) => (r.tab ?? "tab-main") === args.tabId);
        const taken = new Set(inTab.map((r) => r.pane_id));
        const slot = !taken.has(args?.paneId) ? args?.paneId : [0, 1, 2, 3].find((n) => !taken.has(n));
        if (slot === undefined) throw new Error("aba cheia");
        sessions++;
        const id = `sess-${sessions}`;
        registry.set(id, { pane_id: slot, alive: true, exit_code: null, tab: args.tabId, cwd: args.cwd });
        return {
          session_id: id,
          pane_id: slot,
          command: ["C:\\Windows\\System32\\cmd.exe", "/k", "SEGREDO"],
          interpreter_id: args.interpreterId ?? "powershell",
          interpreter_label: args.interpreterId === "cmd" ? "CMD" : args.interpreterId === "bash_git" ? "Bash (Git Bash)" : "PowerShell 7",
          cwd: args.cwd,
          pid: 1000 + sessions,
          is_alive: true,
          created_at: "2026-11-17T12:00:00Z",
        };
      }
      case "terminal_list_tabs":
        return {
          tabs: Array.from(new Set(["tab-main", ...tabs.keys()])).map((tabId) => ({
            tab_id: tabId,
            panes: Object.fromEntries(
              Array.from(registry.entries())
                .filter(([, r]) => (r.tab ?? "tab-main") === tabId)
                .map(([id, r]) => [String(r.pane_id), { session_id: id, is_alive: r.alive, exit_code: r.exit_code }]),
            ),
          })),
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
  it("trocar o interpretador encerra e recria somente a sessão selecionada no mesmo diretório", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
    await waitFor(() => expect(xtermLog.created).toHaveLength(2));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const selector = await screen.findByRole("combobox", { name: "Interpretador de Terminal 01" });
    await user.selectOptions(selector, "cmd");
    await waitFor(() => expect(callsOf("terminal_close_session")).toContainEqual({ sessionId: "sess-1" }));
    await waitFor(() => expect(callsOf("terminal_create_session")).toHaveLength(3));
    expect(callsOf("terminal_create_session")[2]).toMatchObject({
      tabId: "tab-main", paneId: 0, cwd: "C:\\Users\\10864", interpreterId: "cmd",
    });
    expect(invoke.mock.calls.findIndex(([cmd]) => cmd === "terminal_validate_interpreter"))
      .toBeLessThan(invoke.mock.calls.findIndex(([cmd]) => cmd === "terminal_close_session"));
    expect(callsOf("terminal_close_session")).toEqual([{ sessionId: "sess-1" }]);
    expect(screen.getByRole("region", { name: "Terminal 02" })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Terminal 03" })).toBeInTheDocument();
    expect(await screen.findByRole("combobox", { name: "Interpretador de Terminal 03" })).toHaveValue("cmd");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Interpretador de Terminal 03" }), "powershell");
    await waitFor(() => expect(callsOf("terminal_create_session")).toHaveLength(4));
    expect(callsOf("terminal_create_session")[3]).toMatchObject({ paneId: 0, cwd: "C:\\Users\\10864", interpreterId: "powershell" });
    expect(await screen.findByRole("combobox", { name: "Interpretador de Terminal 04" })).toHaveValue("powershell");
    await user.selectOptions(await screen.findByRole("combobox", { name: "Interpretador de Terminal 04" }), "bash_git");
    await waitFor(() => expect(callsOf("terminal_create_session")).toHaveLength(5));
    expect(callsOf("terminal_create_session")[4]).toMatchObject({ paneId: 0, cwd: "C:\\Users\\10864", interpreterId: "bash_git" });
    expect(callsOf("terminal_close_session")).toEqual([{ sessionId: "sess-1" }, { sessionId: "sess-3" }, { sessionId: "sess-4" }]);
    expect(screen.getByRole("region", { name: "Terminal 02" })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Terminal 05" })).toBeInTheDocument();
    expect(await screen.findByRole("combobox", { name: "Interpretador de Terminal 05" })).toHaveValue("bash_git");
    confirm.mockRestore();
  });

  it("falha na compatibilidade do novo shell mantém a sessão atual aberta", async () => {
    const user = userEvent.setup();
    host({ extra: { terminal_validate_interpreter: () => { throw new Error("Diretório incompatível com WSL"); } } });
    renderApp();
    await connected();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.selectOptions(await screen.findByRole("combobox", { name: "Interpretador de Terminal 01" }), "cmd");
    await waitFor(() => expect(screen.getByText(/Diretório incompatível com WSL/)).toBeInTheDocument());
    expect(callsOf("terminal_close_session")).toHaveLength(0);
    expect(screen.getByRole("region", { name: "Terminal 01" })).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("real App headers never invent quota or context, even when the host supplies bogus metrics", async () => {
    const user = userEvent.setup();
    const registry = host();
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      const result = await original(cmd, args);
      if (cmd === "terminal_list_tabs") {
        for (const tab of result.tabs) for (const pane of Object.values(tab.panes) as any[]) {
          Object.assign(pane, { context_percent: 90, quota_5h_percent: 81, quota_7d_percent: 75 });
        }
      }
      return result;
    });
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
    await waitFor(() => expect(xtermLog.created).toHaveLength(2));
    registry.set("sess-1", { pane_id: 0, alive: false, exit_code: 0 });
    await waitFor(() => expect(screen.getByRole("region", { name: "Terminal 01" })).toHaveTextContent("encerrado"), { timeout: 3500 });
    for (const header of document.querySelectorAll(".terminal-pane__header")) {
      expect(header.textContent).not.toContain("%");
      expect(header.querySelectorAll(".indicator-ring--unknown")).toHaveLength(3);
      expect(within(header as HTMLElement).getByRole("button", { name: /Indicadores.*indisponíveis/ })).toBeInTheDocument();
    }
    expect(document.querySelector(".pane-indicators__popover")).toBeNull();
  }, 10000);

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
    expect(trail).toEqual(["main-tab-conversas", "Novo terminal", "Terminal 01, 1 de 2", "Terminal 02, 2 de 2", "main-tab-conversas"]);
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
    expect(document.body.textContent).toMatch(/PowerShell 7/);
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

  it.each([false, undefined])("an unconfirmed close (%s) keeps the pane and allows a retry", async (closed) => {
    const user = userEvent.setup();
    const registry = host();
    renderApp();
    await connected();
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      if (cmd === "terminal_close_session") return { closed };
      return original(cmd, args);
    });
    await user.click(screen.getByRole("button", { name: "Fechar Terminal 01" }));
    expect(await screen.findByText(/O terminal continua aberto/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Terminal 01" })).toBeInTheDocument();
    expect(xtermLog.disposed).toBe(0);
    expect(registry.has("sess-1")).toBe(true);
    invoke.mockImplementation(original);
    await user.click(screen.getByRole("button", { name: "Fechar Terminal 01" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Terminal 01" })).toBeNull());
    expect(registry.has("sess-1")).toBe(false);
  });

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

const notices = () => screen.getByTestId("system-notices");
const callsOf = (cmd: string) => invoke.mock.calls.filter((c) => c[0] === cmd).map((c) => c[1]);
const indexOf = (cmd: string) => invoke.mock.calls.findIndex((c) => c[0] === cmd);

describe("App — inicialização que nunca trava (E2-6, K1, K2, K5)", () => {
  afterEach(() => {
    for (const key of ["terminalFontSize", "terminalScrollback", "terminalCursorBlink", "stimulation", "density"]) {
      delete document.documentElement.dataset[key];
    }
  });

  it("settings_get falhando: o terminal abre com os padrões e o motivo fica visível até carregar", async () => {
    const user = userEvent.setup();
    const reason = "O esquema salvo não é compatível com esta versão. Os dados foram preservados.";
    host({ fail: { settings_get: { code: -32602, message: reason, data: { field: "schema_version", reason: "incompatible_version" } } } });
    renderApp();
    await connected();
    expect(count("terminal_create_session")).toBe(1);
    expect(xtermLog.created[0].options).toMatchObject({ fontSize: 13, scrollback: 1000, cursorBlink: true });
    expect(notices()).toHaveTextContent("Preferências não carregadas: usando os padrões");
    expect(notices()).toHaveTextContent(reason);
    expect(document.body.textContent).not.toMatch(/Erro de inicialização/);
    // A transient message does not replace it, and it has no dismiss button: it lasts while the cause lasts.
    expect(within(notices()).queryByRole("button", { name: /Dispensar/ })).toBeNull();
    await user.click(within(notices()).getByRole("button", { name: "Abrir Configurações" }));
    expect(await screen.findByRole("dialog", { name: "Configurações" })).toBeInTheDocument();
  });

  it("profile_remember_project falhando não impede o terminal e é chamado só depois da sessão", async () => {
    host({ fail: { profile_remember_project: { code: -32602, message: "O diretório do projeto não está disponível.", data: { reason: "path_missing" } } } });
    renderApp();
    await connected();
    expect(count("terminal_create_session")).toBe(1);
    await waitFor(() => expect(count("profile_remember_project")).toBe(1));
    expect(indexOf("profile_remember_project")).toBeGreaterThan(indexOf("terminal_create_session"));
    expect(await screen.findByText(/O terminal abriu, mas o projeto não foi lembrado/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Terminal 01" })).toBeInTheDocument();
  });

  it("abertura automática desligada com projetos autorizados: a tela diz a verdade e abre um com um clique", async () => {
    const user = userEvent.setup();
    host({
      settings: appSettings({ geral: { open_last_project_on_start: false } }),
      profile: { authorized_projects: ["D:\\proj", "D:\\outro"] },
    });
    renderApp();
    expect(await screen.findByRole("heading", { name: "Escolha o projeto" })).toBeInTheDocument();
    expect(screen.getByText(/abertura automática está desligada/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Nenhum projeto está autorizado/);
    expect(count("terminal_create_tab")).toBe(0);
    // Authorizing another directory is still offered.
    expect(screen.getByLabelText("Caminho do projeto no host")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Abrir D:\\outro" }));
    await connected();
    expect(callsOf("terminal_create_tab")).toEqual([{ tabId: "tab-main", projectPath: "D:\\outro" }]);
    expect(count("profile_authorize_project")).toBe(0);
    expect(bar()).toHaveTextContent("outro");
  });

  it("abre a pasta padrão do usuário com PowerShell quando ela já está autorizada", async () => {
    host({ profile: { authorized_projects: ["D:\\proj", "C:\\Users\\10864", "D:\\outro"], last_project: "d:/OUTRO" } });
    renderApp();
    await connected();
    expect(callsOf("terminal_create_tab")).toEqual([{ tabId: "tab-main", projectPath: "C:\\Users\\10864" }]);
    expect(callsOf("terminal_create_session")[0]).toMatchObject({ cwd: "C:\\Users\\10864", interpreterId: "powershell" });
  });

  it("sem autorização para a pasta padrão, pede autorização explícita e não cria terminal", async () => {
    host({ profile: { authorized_projects: ["D:\\proj"] } });
    renderApp();
    expect(await screen.findByLabelText("Caminho do projeto no host")).toHaveValue("C:\\Users\\10864");
    expect(screen.getByText(/Autorize explicitamente C:\\Users\\10864/)).toBeInTheDocument();
    expect(count("profile_authorize_project")).toBe(0);
    expect(count("terminal_create_tab")).toBe(0);
  });

  it("abre manualmente outro projeto autorizado em um novo terminal PowerShell", async () => {
    const user = userEvent.setup();
    host({
      settings: appSettings({ geral: { open_last_project_on_start: false } }),
      profile: { authorized_projects: ["C:\\Users\\10864", "D:\\ai-coordinator"] },
    });
    renderApp();
    expect(await screen.findByRole("heading", { name: "Escolha o projeto" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Abrir D:\\ai-coordinator" }));
    await connected();
    await waitFor(() => expect(callsOf("terminal_create_tab")).toHaveLength(1));
    expect(callsOf("terminal_create_tab")[0]).toMatchObject({ tabId: "tab-main", projectPath: "D:\\ai-coordinator" });
    expect(callsOf("terminal_create_session")[0]).toMatchObject({ cwd: "D:\\ai-coordinator", interpreterId: "powershell" });
  });

  it("falha ao abrir a pasta padrão: mostra a tela de autorização com o motivo", async () => {
    host({ extra: { terminal_create_tab: () => { throw new Error("Project not authorized or session invalid"); } } });
    renderApp();
    expect(await screen.findByText(/Não foi possível abrir C:\\Users\\10864: Project not authorized/)).toBeInTheDocument();
    const open = screen.getByRole("button", { name: "Abrir C:\\Users\\10864" });
    expect(xtermLog.created).toHaveLength(0);
    // There is no notice banner on this path: the reason reaches a screen reader with the focus, as the description.
    await waitFor(() => expect(open).toHaveFocus());
    expect(open).toHaveAccessibleDescription(/Não foi possível abrir C:\\Users\\10864: Project not authorized/);
  });

  it("perfil recuperado: aviso persistente com o nome da cópia, sem caminho", async () => {
    host({
      authorized: false,
      profile: {
        recovery: {
          reason: "profile_unreadable",
          backup_file: "profile.json.corrupt-20261117T120000Z",
          message: "O perfil estava ilegível e foi recriado.",
        },
      },
    });
    renderApp();
    await screen.findByLabelText("Caminho do projeto no host");
    expect(notices()).toHaveTextContent("Perfil ilegível recriado: sem projetos autorizados e sem configurações");
    expect(notices()).toHaveTextContent("O perfil estava ilegível e foi recriado.");
    expect(within(notices()).getByText("profile.json.corrupt-20261117T120000Z").tagName).toBe("CODE");
  });

  it("configurações de outra versão: aviso, xterm, documento, cabeçalho e formulário usam os padrões, como o host diz", async () => {
    const user = userEvent.setup();
    // The real host's message for schema_version 2 (desktop/settings/store.py). The values are the other version's,
    // as the host sent them at a9e2462: whatever the values say, a protected snapshot means the defaults are in use.
    const message = "As configurações foram gravadas por outra versão e estão protegidas. Os padrões estão em uso e nada será alterado.";
    const settings = appSettings({
      perfil: { display_name: "Outra Versao" },
      geral: { open_last_project_on_start: false },
      aparencia: { stimulation: "low", density: "compact" },
      terminal: { font_size: 18, scrollback_lines: 200, cursor_blink: false },
    });
    for (const category of Object.values(settings.categories) as any[]) category.validated = false;
    host({
      settings: { ...settings, stored_schema_version: 2, read_only: true, stored_issue: { reason: "incompatible_version", message } },
      profile: { name: "trabalho" },
    });
    renderApp();
    await connected(); // the default opens the authorized user folder; the other version's preference is protected
    expect(xtermLog.created[0].options).toMatchObject({ fontSize: 13, scrollback: 1000, cursorBlink: true });
    expect(document.documentElement.dataset.density).toBe("comfortable");
    expect(document.documentElement.dataset.stimulation).toBeUndefined();
    expect(document.querySelector(".app-header__operator")).toHaveTextContent("Perfil: trabalho");
    expect(document.body.textContent).not.toMatch(/Outra Versao/);
    expect(notices()).toHaveTextContent("Configurações de outra versão: protegidas, somente leitura");
    expect(notices()).toHaveTextContent(message);
    expect(notices()).toHaveTextContent("Esta versão usa os próprios padrões e não grava sobre elas.");
    expect(notices().textContent).not.toMatch(/próprios valores/);

    await user.click(screen.getByRole("button", { name: "Configurações" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("tab", { name: /^Terminal/ }));
    const panel = within(dialog).getByRole("tabpanel", { name: /Terminal/ });
    expect(await within(panel).findByText(message)).toBeInTheDocument();
    expect(within(panel).getByText(/mostram os padrões, que são os valores que esta versão está usando/)).toBeInTheDocument();
    expect(within(panel).getByLabelText("Tamanho da fonte")).toHaveValue(13);
    expect(within(panel).getByLabelText("Linhas de histórico")).toHaveValue(1000);
    expect(within(panel).getByRole("checkbox", { name: "Cursor piscante" })).toBeChecked();
    // Reloading the snapshot in Configurações does not apply the other version's values either.
    expect(xtermLog.created[0].options).toMatchObject({ fontSize: 13, scrollback: 1000, cursorBlink: true });
  });

  it("campos salvos inválidos: o aviso diz onde, nunca o valor salvo", async () => {
    const settings = appSettings();
    settings.categories.terminal = { ...settings.categories.terminal, validated: false, invalid_fields: ["font_size"] };
    host({ settings });
    renderApp();
    await connected();
    expect(notices()).toHaveTextContent("Preferências salvas inválidas: usando o padrão nesses campos");
    expect(notices()).toHaveTextContent("Terminal (1 campo)");
  });

  it("falha de inicialização (perfil ocupado): Tentar novamente refaz a conexão", async () => {
    const user = userEvent.setup();
    let busy = true;
    host({
      extra: {
        profile_get_active: () => {
          if (busy) throw { code: -32603, message: "Outra janela está usando o perfil/cofre. Tente novamente.", data: { reason: "busy" } };
          return { name: "default", authorized_projects: ["C:\\Users\\10864", "D:\\proj"], last_project: null };
        },
      },
    });
    renderApp();
    const alert = await screen.findByText("Falha na inicialização da Alpha técnica");
    expect(alert.closest("[role=alert]")).toHaveTextContent("Outra janela está usando o perfil/cofre. Tente novamente.");
    busy = false;
    await user.click(screen.getByRole("button", { name: "Tentar novamente" }));
    await connected();
    expect(count("ping")).toBe(2);
    expect(count("terminal_create_tab")).toBe(1);
  });

  it("primeira pintura: a janela nunca fica em branco e o prazo libera a área dos terminais", async () => {
    host({ extra: { settings_get: () => new Promise(() => {}) } });
    renderApp();
    const app = document.querySelector(".app")!;
    expect(app).toHaveAttribute("data-preferences-loading", "true");
    expect(await screen.findByText(/Inicializando runtime do Core/)).toBeInTheDocument();
    expect(screen.getByRole("banner")).toBeInTheDocument();
    await waitFor(() => expect(app).not.toHaveAttribute("data-preferences-loading"), { timeout: PREFERENCES_DEADLINE_MS + 2500 });
  }, 10000);

  it("nome no cabeçalho: com contexto para leitor de tela; sem nome de exibição, o nome do perfil", async () => {
    host({ profile: { name: "trabalho" } });
    renderApp();
    await connected();
    const name = document.querySelector(".app-header__operator")!;
    expect(name).toHaveTextContent("Perfil: trabalho");
    expect(name).toHaveAttribute("title", "Perfil: trabalho");
    expect(name.querySelector(".sr-only")).toHaveTextContent("Perfil:");
  });

  it("com nome de exibição salvo, é ele que aparece", async () => {
    host({ settings: appSettings({ perfil: { display_name: "Ana" } }), profile: { name: "trabalho" } });
    renderApp();
    await connected();
    expect(document.querySelector(".app-header__operator")).toHaveTextContent("Perfil: Ana");
  });
});

describe("App — preferências, atalhos e revogação (Etapa 7)", () => {
  afterEach(() => {
    for (const key of ["terminalFontSize", "terminalScrollback", "terminalCursorBlink", "stimulation", "density"]) {
      delete document.documentElement.dataset[key];
    }
  });

  it("fonte, cursor e histórico salvos chegam ao xterm; mudar em Configurações aplica ao vivo sem recriar nada", async () => {
    const user = userEvent.setup();
    host({ settings: appSettings({ terminal: { font_size: 18, scrollback_lines: 500, cursor_blink: false } }) });
    renderApp();
    await connected();
    const term = xtermLog.created[0];
    expect(term.options).toMatchObject({ fontSize: 18, scrollback: 500, cursorBlink: false });

    await user.click(screen.getByRole("button", { name: "Configurações" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("tab", { name: /^Terminal/ }));
    const panel = within(dialog).getByRole("tabpanel", { name: /Terminal/ });
    const font = await within(panel).findByLabelText("Tamanho da fonte");
    await waitFor(() => expect(font).toHaveValue(18));
    await user.clear(font);
    await user.type(font, "20");
    await user.click(within(panel).getByRole("button", { name: "Salvar" }));
    await waitFor(() => expect(term.options.fontSize).toBe(20));
    expect(term.options).toMatchObject({ scrollback: 500, cursorBlink: false });
    expect(xtermLog.created).toHaveLength(1);
    expect(xtermLog.disposed).toBe(0);
    expect(count("terminal_create_session")).toBe(1);
    expect(count("terminal_close_session")).toBe(0);
  });

  it("baixa estimulação desliga o cursor piscante do terminal aberto", async () => {
    host({ settings: appSettings({ aparencia: { stimulation: "low" } }) });
    renderApp();
    await connected();
    expect(xtermLog.created[0].options.cursorBlink).toBe(false);
  });

  it("atalhos no App real: Ctrl+Shift+2 foca o painel 2, Ctrl+, abre Geral, PageDown troca a vista; nada vai ao shell", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
    await waitFor(() => expect(xtermLog.created).toHaveLength(2));

    const textarea = xtermLog.created[0].textarea!;
    textarea.focus();
    fireEvent.keyDown(textarea, { key: "@", code: "Digit2", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(document.activeElement).toHaveAttribute("aria-label", "Terminal 02, 2 de 2"));

    xtermLog.created[1].textarea!.focus();
    fireEvent.keyDown(xtermLog.created[1].textarea!, { key: ",", code: "Comma", ctrlKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Configurações" });
    expect(within(dialog).getByRole("tab", { name: /^Geral/ })).toHaveAttribute("aria-selected", "true");
    // With the dialog open the application shortcuts do nothing.
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "PageDown", code: "PageDown", ctrlKey: true, shiftKey: true });
    expect(screen.getByRole("tab", { name: /^Conversas/, hidden: true })).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.keyDown(document.activeElement ?? document.body, { key: "PageDown", code: "PageDown", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(screen.getByRole("tab", { name: /^Monitoramento/ })).toHaveAttribute("aria-selected", "true"));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("tab", { name: /^Monitoramento/ })));
    fireEvent.keyDown(document.activeElement!, { key: "PageUp", code: "PageUp", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(screen.getByRole("tab", { name: /^Conversas/ })).toHaveAttribute("aria-selected", "true"));

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(count("terminal_write")).toBe(0);
    expect(xtermLog.created).toHaveLength(2);
  });

  it("revogar o projeto aberto sem terminais volta à tela de projetos; o próximo projeto usa uma aba nova", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: "Fechar Terminal 01" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Terminal 01" })).toBeNull());

    await user.click(screen.getByRole("button", { name: "Configurações" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("tab", { name: /^Segurança/ }));
    const panel = within(dialog).getByRole("tabpanel", { name: /Segurança/ });
    await user.click(await within(panel).findByRole("button", { name: "Revogar C:\\Users\\10864" }));
    await user.click(within(panel).getByRole("button", { name: "Confirmar revogação" }));
    expect(await within(panel).findByText(/Autorização de C:\\Users\\10864 revogada/)).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(await screen.findByText(/A autorização de C:\\Users\\10864 foi revogada/)).toBeInTheDocument();
    expect(bar()).not.toHaveTextContent("10864");
    const input = screen.getByLabelText("Caminho do projeto no host");
    await user.clear(input);
    await user.type(input, "D:\\novo");
    await user.click(screen.getByRole("button", { name: "Autorizar diretório e iniciar terminal" }));
    await waitFor(() => expect(screen.getByRole("region", { name: /^Terminal \d+$/ })).toBeInTheDocument());
    const tabs = callsOf("terminal_create_tab");
    expect(tabs).toEqual([
      { tabId: "tab-main", projectPath: "C:\\Users\\10864" },
      { tabId: "tab-2", projectPath: "D:\\novo" },
    ]);
    expect(callsOf("terminal_create_session").at(-1)).toMatchObject({ tabId: "tab-2", cwd: "D:\\novo" });
    expect(document.body.textContent).not.toMatch(/Falha ao criar|already exists/);
  });

  it("Configurações aberto pelo atalho a partir da faixa: revogar o projeto aberto e fechar leva o foco à tela de projetos, nunca ao body", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: "Fechar Terminal 01" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Terminal 01" })).toBeNull());
    const opener = document.activeElement as HTMLElement;
    expect(opener.closest(".tab-strip")).not.toBeNull(); // F10: the last pane closed, the focus is on the strip

    fireEvent.keyDown(opener, { key: ",", code: "Comma", ctrlKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Configurações" });
    await user.click(within(dialog).getByRole("tab", { name: /^Segurança/ }));
    const panel = within(dialog).getByRole("tabpanel", { name: /Segurança/ });
    await user.click(await within(panel).findByRole("button", { name: "Revogar C:\\Users\\10864" }));
    await user.click(within(panel).getByRole("button", { name: "Confirmar revogação" }));
    expect(await within(panel).findByText(/Autorização de C:\\Users\\10864 revogada/)).toBeInTheDocument();
    expect(opener.isConnected).toBe(false); // the strip went away with the terminal grid
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    const firstAuthorizedProject = screen.getByRole("button", { name: "Abrir D:\\proj" });
    await waitFor(() => expect(document.activeElement).toBe(firstAuthorizedProject));
    expect(firstAuthorizedProject).toHaveAccessibleDescription(/A autorização de C:\\Users\\10864 foi revogada/);
  });

  it("sem 'Salvar sessões': o histórico não é gravado em arquivo, e Geral não promete exportá-lo", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    const strip = screen.getByRole("group", { name: "Aba do projeto" });
    expect(within(strip).queryByRole("button", { name: /Salvar/ })).toBeNull();
    expect(within(strip).getByRole("button", { name: "Novo terminal" })).toHaveAttribute("data-f6-entry");

    await user.click(screen.getByRole("button", { name: "Configurações" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("tab", { name: /^Geral/ }));
    const panel = within(dialog).getByRole("tabpanel", { name: /Geral/ });
    expect(within(panel).getByText("Exportar o histórico e restaurar sessões ainda não são suportados nesta versão.")).toBeInTheDocument();
    expect(dialog.textContent).not.toMatch(/pode ser exportado/);
    expect(document.body.textContent).not.toMatch(/arquivados|Sessão salva/);
    expect(count("terminal_save_sessions")).toBe(0);
  });

  it("com terminais abertos no projeto, a revogação é recusada e o App não muda", async () => {
    const user = userEvent.setup();
    host();
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: "Configurações" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("tab", { name: /^Segurança/ }));
    const panel = within(dialog).getByRole("tabpanel", { name: /Segurança/ });
    await user.click(await within(panel).findByRole("button", { name: "Revogar C:\\Users\\10864" }));
    await user.click(within(panel).getByRole("button", { name: "Confirmar revogação" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Feche os terminais deste diretório antes de revogar a autorização.");
    await user.keyboard("{Escape}");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByRole("region", { name: "Terminal 01" })).toBeInTheDocument();
    expect(bar()).toHaveTextContent("10864");
  });
});

describe("App — diretório amplo e falhas de transporte (Etapa 8, L2/L3/L7)", () => {
  // The arm delay's clock is under the test's control: a pointer click only answers after it.
  let now = 0;
  beforeEach(() => {
    now = 10_000;
    vi.spyOn(armingClock, "now").mockImplementation(() => now);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const authorizeCalls = () => callsOf("profile_authorize_project");

  async function askBroad(user: ReturnType<typeof userEvent.setup>, path = "C:\\") {
    const input = await screen.findByLabelText("Caminho do projeto no host");
    await user.clear(input);
    await user.type(input, path);
    await user.click(screen.getByRole("button", { name: "Autorizar diretório e iniciar terminal" }));
    return screen.findByRole("alertdialog", { name: `Autorizar ${path} com acesso amplo?` });
  }

  it("a pergunta é explícita, com a resposta segura em foco; Cancelar não autoriza e devolve o foco ao caminho", async () => {
    const user = userEvent.setup();
    host({ authorized: false });
    renderApp();
    const question = await askBroad(user);
    expect(question).toHaveAccessibleDescription(/raiz de um disco ou de um compartilhamento, ou a sua pasta pessoal inteira/);
    const cancel = within(question).getByRole("button", { name: "Cancelar" });
    await waitFor(() => expect(cancel).toHaveFocus());
    // The question is the message: no failure banner, and nothing was authorized.
    expect(document.body.textContent).not.toMatch(/Falha na autorização/);
    expect(authorizeCalls()).toEqual([{ projectPath: "C:\\" }]);
    // While it is open nothing else starts, and the path it is about cannot change under it.
    const input = screen.getByLabelText("Caminho do projeto no host");
    expect(input).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Autorizar diretório e iniciar terminal" })).toHaveAttribute("aria-disabled", "true");
    fireEvent.submit(screen.getByRole("form", { name: "Autorizar diretório" }));
    expect(authorizeCalls()).toHaveLength(1);

    await user.click(cancel);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(input).toHaveFocus();
    expect(input).not.toHaveAttribute("readonly");
    expect(authorizeCalls()).toEqual([{ projectPath: "C:\\" }]);
    expect(count("terminal_create_tab")).toBe(0);
    expect(xtermLog.created).toHaveLength(0);
  });

  it("Esc responde Cancelar: nada é autorizado e o foco volta ao caminho", async () => {
    const user = userEvent.setup();
    host({ authorized: false });
    renderApp();
    const question = await askBroad(user, "\\\\servidor\\pasta");
    await waitFor(() => expect(within(question).getByRole("button", { name: "Cancelar" })).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByLabelText("Caminho do projeto no host")).toHaveFocus();
    expect(authorizeCalls()).toEqual([{ projectPath: "\\\\servidor\\pasta" }]);
    expect(count("terminal_create_tab")).toBe(0);
  });

  it("confirmar pelo teclado repete o pedido com confirmBroad: true e abre o terminal no diretório", async () => {
    const user = userEvent.setup();
    host({ authorized: false });
    renderApp();
    const question = await askBroad(user, "C:\\Users\\ana");
    await waitFor(() => expect(within(question).getByRole("button", { name: "Cancelar" })).toHaveFocus());
    await user.tab(); // a deliberate move from the safe answer; keyboard activation is not delayed
    expect(within(question).getByRole("button", { name: "Autorizar acesso amplo" })).toHaveFocus();
    await user.keyboard("{Enter}");
    await connected();
    expect(authorizeCalls()).toEqual([{ projectPath: "C:\\Users\\ana" }, { projectPath: "C:\\Users\\ana", confirmBroad: true }]);
    expect(callsOf("terminal_create_tab")).toEqual([{ tabId: "tab-main", projectPath: "C:\\Users\\ana" }]);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("um clique de ponteiro antes do atraso de armar não responde; depois dele, responde uma vez só", async () => {
    const user = userEvent.setup();
    host({ authorized: false });
    renderApp();
    const question = await askBroad(user);
    const answer = within(question).getByRole("button", { name: "Autorizar acesso amplo" });
    expect(answer).toHaveClass("btn--arming");
    fireEvent.click(answer, { detail: 1 });
    fireEvent.click(answer, { detail: 2 });
    expect(authorizeCalls()).toHaveLength(1);
    expect(screen.getByRole("alertdialog")).toBe(question);

    now += CONFIRM_ARM_DELAY_MS + 1;
    act(() => {
      fireEvent.click(answer, { detail: 1 });
      fireEvent.click(answer, { detail: 1 }); // a second activation before React renders again
    });
    await connected();
    expect(authorizeCalls()).toEqual([{ projectPath: "C:\\" }, { projectPath: "C:\\", confirmBroad: true }]);
    expect(count("terminal_create_tab")).toBe(1);
  });

  it("Enter mantido pressionado no campo abre a pergunta uma vez e não a responde nem a cancela", async () => {
    const user = userEvent.setup();
    host({ authorized: false });
    renderApp();
    const input = await screen.findByLabelText("Caminho do projeto no host");
    await user.clear(input);
    await user.type(input, "C:\\");
    await user.keyboard("{Enter>8/}");
    const question = await screen.findByRole("alertdialog");
    await waitFor(() => expect(within(question).getByRole("button", { name: "Cancelar" })).toHaveFocus());
    await user.keyboard("{Enter>6/}"); // held on Cancelar: one real press answers it, the repeats do not resubmit
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(authorizeCalls()).toEqual([{ projectPath: "C:\\" }]);
  });

  it("um diretório comum não pergunta nada e não envia confirmBroad", async () => {
    const user = userEvent.setup();
    host({ authorized: false });
    renderApp();
    const input = await screen.findByLabelText("Caminho do projeto no host");
    await user.clear(input);
    await user.type(input, "D:\\novo");
    await user.click(screen.getByRole("button", { name: "Autorizar diretório e iniciar terminal" }));
    await connected();
    expect(authorizeCalls()).toEqual([{ projectPath: "D:\\novo" }]);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("erro estruturado na autorização (L3): a mensagem do host, sem pergunta", async () => {
    const user = userEvent.setup();
    host({
      authorized: false,
      fail: {
        profile_authorize_project: {
          code: -32602,
          message: "O diretório do projeto não existe ou não é uma pasta.",
          data: { field: "project_path", reason: "path_missing" },
        },
      },
    });
    renderApp();
    const input = await screen.findByLabelText("Caminho do projeto no host");
    await user.clear(input);
    await user.type(input, "D:\\sumiu");
    await user.click(screen.getByRole("button", { name: "Autorizar diretório e iniciar terminal" }));
    expect(await screen.findByText("Falha na autorização do projeto: O diretório do projeto não existe ou não é uma pasta.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(document.body.textContent).not.toMatch(/\[object Object\]/);
  });

  it("autorização sem resposta (timeout): não declara falha nem sucesso, e relê a lista para conferir", async () => {
    const user = userEvent.setup();
    let reads = 0;
    host({
      authorized: false,
      fail: { profile_authorize_project: TIMEOUT_30 },
      extra: {
        // The request went through on the Core after all: the next reading lists it.
        profile_get_active: () => ({ name: "default", display_name: null, last_project: null, authorized_projects: reads++ === 0 ? [] : ["D:\\novo"] }),
      },
    });
    renderApp();
    const input = await screen.findByLabelText("Caminho do projeto no host");
    await user.clear(input);
    await user.type(input, "D:\\novo");
    await user.click(screen.getByRole("button", { name: "Autorizar diretório e iniciar terminal" }));
    expect(
      await screen.findByText(
        "A autorização do projeto não foi confirmada: O Core não respondeu em 30 s. A operação pode ter sido concluída: confira antes de repetir."
      )
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Falha na autorização/);
    expect(await screen.findByRole("button", { name: "Abrir D:\\novo" })).toBeInTheDocument();
    expect(count("profile_get_active")).toBe(2);
    expect(count("terminal_create_tab")).toBe(0);
  });

  it("inicialização com profile_get_active sem resposta: a falha diz o motivo e Tentar novamente refaz a conexão", async () => {
    const user = userEvent.setup();
    let stuck = true;
    host({
      extra: {
        profile_get_active: () => {
          if (stuck) throw TIMEOUT_30;
          return { name: "default", authorized_projects: ["C:\\Users\\10864", "D:\\proj"], last_project: null };
        },
      },
    });
    renderApp();
    const alert = (await screen.findByText("Falha na inicialização da Alpha técnica")).closest("[role=alert]")!;
    expect(alert).toHaveTextContent("O Core não respondeu em 30 s. A operação pode ter sido concluída: confira antes de repetir.");
    expect(document.body.textContent).not.toMatch(/\[object Object\]/);
    stuck = false;
    await user.click(screen.getByRole("button", { name: "Tentar novamente" }));
    await connected();
  });

  it("inicialização com o Core encerrado (disconnected): pede reiniciar e não oferece Tentar novamente", async () => {
    host({ fail: { profile_get_active: DISCONNECTED } });
    renderApp();
    const alert = (await screen.findByText("Falha na inicialização da Alpha técnica")).closest("[role=alert]")!;
    expect(alert).toHaveTextContent("O Core encerrou a comunicação. Reinicie o aplicativo.");
    expect(screen.queryByRole("button", { name: "Tentar novamente" })).toBeNull();
  });

  it("o mesmo vale quando um comando de texto (call) devolve a mensagem fixa de canal encerrado", async () => {
    host({ fail: { get_runtime_status: "O Core encerrou a comunicação. Reinicie o aplicativo." } });
    renderApp();
    const alert = (await screen.findByText("Falha na inicialização da Alpha técnica")).closest("[role=alert]")!;
    expect(alert).toHaveTextContent("O Core encerrou a comunicação. Reinicie o aplicativo.");
    expect(screen.queryByRole("button", { name: "Tentar novamente" })).toBeNull();
    expect(count("profile_get_active")).toBe(0);
  });

  it("terminal sem resposta ao criar (texto de call): a mensagem do host aparece como está", async () => {
    const user = userEvent.setup();
    let created = 0;
    host();
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      if (cmd === "terminal_create_session" && created++ > 0) {
        throw "O Core não respondeu em 120 s. A operação pode ter sido concluída: confira antes de repetir.";
      }
      return original(cmd, args);
    });
    renderApp();
    await connected();
    await user.click(screen.getByRole("button", { name: /Novo terminal/ }));
    expect(
      await screen.findByText("Falha ao criar terminal: O Core não respondeu em 120 s. A operação pode ter sido concluída: confira antes de repetir.")
    ).toBeInTheDocument();
  });

  it("Segurança marca o diretório amplo autorizado com confirmação", async () => {
    const user = userEvent.setup();
    host({ authorized: false });
    renderApp();
    const question = await askBroad(user);
    await waitFor(() => expect(within(question).getByRole("button", { name: "Cancelar" })).toHaveFocus());
    await user.tab();
    await user.keyboard("{Enter}");
    await connected();

    await user.click(screen.getByRole("button", { name: "Configurações" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("tab", { name: /^Segurança/ }));
    const panel = within(dialog).getByRole("tabpanel", { name: /Segurança/ });
    const item = (await within(panel).findByRole("button", { name: "Revogar C:\\" })).closest("li")!;
    expect(within(item).getByText("Acesso amplo")).toBeInTheDocument();
    expect(within(panel).getByText(/1 diretório autorizado dá acesso amplo/)).toBeInTheDocument();
  });
});

describe("App — tráfego do terminal depois do L1/L2 (Etapa 8)", () => {
  const wait = (ms: number) =>
    act(async () => {
      await new Promise((resolve) => setTimeout(resolve, ms));
    });

  it("leitura de saída lenta: nunca duas ao mesmo tempo, e a saída aparece uma vez só", async () => {
    host();
    let release: (value: unknown) => void = () => undefined;
    let first = true;
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      if (cmd === "terminal_get_output") {
        if (first) {
          first = false;
          return new Promise((resolve) => (release = resolve));
        }
        return { raw: "", next_seq: args.sinceSeq };
      }
      return original(cmd, args);
    });
    renderApp();
    await connected();
    await waitFor(() => expect(count("terminal_get_output")).toBe(1));
    await wait(400); // five ticks: none starts while the first reading is in flight
    expect(callsOf("terminal_get_output")).toEqual([{ sessionId: "sess-1", sinceSeq: 0 }]);
    await act(async () => release({ raw: "C:\\proj>", next_seq: 2 }));
    await waitFor(() => expect(callsOf("terminal_get_output")).toContainEqual({ sessionId: "sess-1", sinceSeq: 2 }));
    await wait(200);
    expect(xtermLog.created[0].written.filter((chunk) => chunk === "C:\\proj>")).toHaveLength(1);
    expect(callsOf("terminal_get_output").filter((c) => c.sinceSeq === 0)).toHaveLength(1);
  });

  it("teclas digitadas com uma escrita pendente vão depois dela, na ordem e juntas, nunca duas escritas ao mesmo tempo", async () => {
    host();
    const releases: Array<() => void> = [];
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      if (cmd === "terminal_write") return new Promise((resolve) => releases.push(() => resolve({ ok: true })));
      return original(cmd, args);
    });
    renderApp();
    await connected();
    const term = xtermLog.created[0];
    act(() => {
      term.emit("l");
      term.emit("s");
      term.emit("\r");
    });
    await wait(50);
    expect(callsOf("terminal_write")).toEqual([{ sessionId: "sess-1", data: "l" }]);
    await act(async () => releases[0]());
    await waitFor(() => expect(count("terminal_write")).toBe(2));
    expect(callsOf("terminal_write")[1]).toEqual({ sessionId: "sess-1", data: "s\r" });
    await act(async () => releases[1]());
    act(() => term.emit("x"));
    await waitFor(() => expect(count("terminal_write")).toBe(3));
    expect(callsOf("terminal_write")[2]).toEqual({ sessionId: "sess-1", data: "x" });
  });

  it("escrita sem resposta: o que foi digitado depois não é enviado, e a mensagem diz isso", async () => {
    host();
    let fail: (err: unknown) => void = () => undefined;
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      if (cmd === "terminal_write") return new Promise((_resolve, reject) => (fail = reject));
      return original(cmd, args);
    });
    renderApp();
    await connected();
    const term = xtermLog.created[0];
    act(() => {
      term.emit("rm ");
      term.emit("-r x\r");
    });
    await waitFor(() => expect(count("terminal_write")).toBe(1)); // the invoke is reached after a dynamic import
    await act(async () => fail(TIMEOUT_30.message));
    expect(
      await screen.findByText(
        "Erro de escrita no terminal: O Core não respondeu em 30 s. A operação pode ter sido concluída: confira antes de repetir. O que foi digitado depois disso não foi enviado."
      )
    ).toBeInTheDocument();
    await wait(50);
    expect(callsOf("terminal_write")).toEqual([{ sessionId: "sess-1", data: "rm " }]);
  });

  it("Core encerrado durante a leitura de saída: a leitura para, o terminal fica e a mensagem pede reiniciar", async () => {
    host();
    const original = invoke.getMockImplementation()!;
    invoke.mockImplementation(async (cmd: string, args?: any) => {
      if (cmd === "terminal_get_output") throw "O Core encerrou a comunicação. Reinicie o aplicativo.";
      return original(cmd, args);
    });
    renderApp();
    await connected();
    expect(
      await screen.findByText("Os terminais pararam de atualizar: O Core encerrou a comunicação. Reinicie o aplicativo.")
    ).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("connection-notice")).toHaveTextContent("Conexão: Falha"));
    const reads = count("terminal_get_output");
    await wait(400);
    expect(count("terminal_get_output")).toBe(reads);
    expect(reads).toBe(1);
    expect(screen.getByRole("region", { name: "Terminal 01" })).toBeInTheDocument();
  });
});
