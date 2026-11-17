/**
 * TerminalPane and the tab strip of TerminalGrid, with a stand-in xterm (jsdom has no canvas nor layout).
 * Etapa 5: theme switched in place (decision C), zero-area guard, one Tab stop per terminal, the pane focus region
 * (F6 entry, decision 9), the accessible name of the terminal input and the key filter.
 */
import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => {
  const state = {
    terms: [] as any[],
    observers: [] as any[],
    proposed: null as null | { cols: number; rows: number },
    fits: 0,
  };
  class FakeTerminal {
    cols = 80;
    rows = 24;
    options: Record<string, unknown>;
    themeAssignments = 0;
    element: HTMLElement | undefined;
    textarea: HTMLTextAreaElement | undefined;
    keyHandler: ((e: KeyboardEvent) => boolean) | undefined;
    keyHandlerCount = 0;
    dataListeners: Array<(d: string) => void> = [];
    disposed = false;
    focusCount = 0;
    constructor(options: Record<string, unknown>) {
      let theme = options.theme;
      this.options = { ...options };
      Object.defineProperty(this.options, "theme", {
        get: () => theme,
        set: (value) => {
          this.themeAssignments++;
          theme = value;
        },
      });
      state.terms.push(this);
    }
    loadAddon(addon: { activate(t: unknown): void }) {
      addon.activate(this);
    }
    attachCustomKeyEventHandler(fn: (e: KeyboardEvent) => boolean) {
      this.keyHandler = fn;
      this.keyHandlerCount++;
    }
    onData(fn: (d: string) => void) {
      this.dataListeners.push(fn);
      return { dispose: () => (this.dataListeners = this.dataListeners.filter((f) => f !== fn)) };
    }
    resize(cols: number, rows: number) {
      this.cols = cols;
      this.rows = rows;
    }
    open(parent: HTMLElement) {
      const root = document.createElement("div");
      root.className = "xterm";
      const viewport = document.createElement("div");
      viewport.className = "xterm-viewport";
      const screenEl = document.createElement("div");
      screenEl.className = "xterm-screen";
      const textarea = document.createElement("textarea");
      textarea.className = "xterm-helper-textarea";
      textarea.setAttribute("aria-label", "Terminal input");
      screenEl.appendChild(textarea);
      root.append(viewport, screenEl);
      parent.appendChild(root);
      this.element = root;
      this.textarea = textarea;
    }
    focus() {
      this.focusCount++;
      this.textarea?.focus();
    }
    write() {}
    writeln() {}
    dispose() {
      this.disposed = true;
    }
  }
  class FakeFitAddon {
    term: FakeTerminal | undefined;
    activate(t: FakeTerminal) {
      this.term = t;
    }
    dispose() {}
    fit() {
      state.fits++;
      if (state.proposed) this.term!.resize(state.proposed.cols, state.proposed.rows);
    }
  }
  return { state, FakeTerminal, FakeFitAddon };
});

vi.mock("xterm", () => ({ Terminal: h.FakeTerminal }));
vi.mock("xterm-addon-fit", () => ({ FitAddon: h.FakeFitAddon }));

import { hasArea, TerminalPane, type TerminalPaneProps } from "./TerminalPane";
import { TerminalGrid } from "./TerminalGrid";

class FakeResizeObserver {
  callback: ResizeObserverCallback;
  disconnected = false;
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    h.state.observers.push(this);
  }
  observe() {}
  unobserve() {}
  disconnect() {
    this.disconnected = true;
  }
  fire(width: number, height: number) {
    this.callback([{ contentRect: { width, height } } as ResizeObserverEntry], this as unknown as ResizeObserver);
  }
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** The tokens the page would resolve: standard or low stimulation. */
function stubTokens() {
  const real = window.getComputedStyle;
  vi.spyOn(window, "getComputedStyle").mockImplementation((el: Element) => {
    if (el !== document.documentElement) return real(el);
    const low = document.documentElement.dataset.stimulation === "low";
    return {
      getPropertyValue: (name: string) =>
        name === "--c-term-bg" ? (low ? "#151820" : "#0c0f16") : name.startsWith("--c-term-") && name !== "--c-term-selection-fg" ? "#d4d4d8" : "",
    } as unknown as CSSStyleDeclaration;
  });
}

function props(over: Partial<TerminalPaneProps> = {}): TerminalPaneProps {
  return {
    sessionId: "session-1",
    tabId: "tab-1",
    paneId: 1,
    kind: "human_terminal",
    number: 7,
    program: "cmd.exe",
    cwd: "C:/work/proj",
    pid: 4321,
    position: 2,
    total: 3,
    onWrite: vi.fn(),
    onResize: vi.fn(),
    onClose: vi.fn(),
    onMount: vi.fn(),
    ...over,
  };
}

const term = () => h.state.terms[0];

beforeEach(() => {
  h.state.terms.length = 0;
  h.state.observers.length = 0;
  h.state.proposed = null;
  h.state.fits = 0;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  stubTokens();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.stimulation;
});

describe("TerminalPane theme (decision C)", () => {
  it("starts with the token palette and keeps xterm's own font", () => {
    render(<TerminalPane {...props()} />);
    const options = term().options as { theme: Record<string, string>; fontSize: number; fontFamily: string; minimumContrastRatio: number };
    expect(options.theme.background).toBe("#0c0f16");
    expect(Object.keys(options.theme)).toHaveLength(21);
    expect(options.fontSize).toBe(13);
    expect(options.fontFamily).toBe("'Cascadia Code', 'Fira Code', 'Consolas', monospace");
    expect(options.minimumContrastRatio).toBe(1);
  });

  it("a theme change re-themes the same terminal: no new instance, no dispose, input still flows", async () => {
    const p = props();
    render(<TerminalPane {...p} />);
    const before = term().options.theme;
    await act(async () => {
      document.documentElement.dataset.stimulation = "low";
      await flush();
    });
    expect(h.state.terms).toHaveLength(1);
    expect(term().disposed).toBe(false);
    expect(term().themeAssignments).toBe(1);
    expect(term().options.theme).not.toBe(before);
    expect(term().options.theme.background).toBe("#151820");
    term().dataListeners.forEach((fn: (d: string) => void) => fn("dir\r"));
    expect(p.onWrite).toHaveBeenCalledWith("session-1", "dir\r");
    await act(async () => {
      delete document.documentElement.dataset.stimulation;
      await flush();
    });
    expect(term().themeAssignments).toBe(2);
    expect(h.state.terms).toHaveLength(1);
  });

  it("closing the pane stops the theme watch and cleans up", async () => {
    const p = props();
    const view = render(<TerminalPane {...p} />);
    const t = term();
    view.unmount();
    expect(t.disposed).toBe(true);
    expect(h.state.observers[0].disconnected).toBe(true);
    expect(p.onMount).toHaveBeenLastCalledWith("session-1", null);
    await act(async () => {
      document.documentElement.dataset.stimulation = "low";
      await flush();
    });
    expect(t.themeAssignments).toBe(0);
  });
});

describe("TerminalPane size", () => {
  it("hasArea refuses boxes under 1 px", () => {
    expect(hasArea(0, 0)).toBe(false);
    expect(hasArea(0.5, 300)).toBe(false);
    expect(hasArea(300, 0.5)).toBe(false);
    expect(hasArea(1, 1)).toBe(true);
  });

  it("keeps the initial sync: one resize 80×24 on mount, even without area (jsdom)", () => {
    const p = props();
    render(<TerminalPane {...p} />);
    expect(h.state.fits).toBe(0);
    expect(p.onResize).toHaveBeenCalledTimes(1);
    expect(p.onResize).toHaveBeenCalledWith("session-1", 80, 24);
  });

  it("with area on mount: fits once and syncs the fitted size", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 800, height: 600 } as DOMRect);
    h.state.proposed = { cols: 100, rows: 30 };
    const p = props();
    render(<TerminalPane {...p} />);
    expect(h.state.fits).toBe(1);
    expect(p.onResize).toHaveBeenCalledTimes(1);
    expect(p.onResize).toHaveBeenCalledWith("session-1", 100, 30);
  });

  it("zero area (hidden view, minimized window): no fit and no PTY resize", () => {
    const p = props();
    render(<TerminalPane {...p} />);
    const observer = h.state.observers[0];
    act(() => {
      observer.fire(0, 0);
      observer.fire(0.5, 300);
    });
    expect(h.state.fits).toBe(0);
    expect(p.onResize).toHaveBeenCalledTimes(1);
  });

  it("the PTY hears only real changes of columns or rows", () => {
    const p = props();
    render(<TerminalPane {...p} />);
    const observer = h.state.observers[0];
    act(() => observer.fire(800, 600)); // same 80×24
    expect(h.state.fits).toBe(1);
    expect(p.onResize).toHaveBeenCalledTimes(1);
    h.state.proposed = { cols: 100, rows: 30 };
    act(() => observer.fire(1000, 750));
    expect(p.onResize).toHaveBeenCalledTimes(2);
    expect(p.onResize).toHaveBeenLastCalledWith("session-1", 100, 30);
    act(() => observer.fire(1001, 751));
    expect(p.onResize).toHaveBeenCalledTimes(2);
  });
});

describe("TerminalPane keyboard", () => {
  it("one Tab stop per terminal: the mount has no tabindex and the viewport is out of the Tab order", () => {
    const { container } = render(<TerminalPane {...props()} />);
    const mount = container.querySelector(".terminal-pane__mount")!;
    expect(mount).not.toHaveAttribute("tabindex");
    expect(mount.querySelector(".xterm-viewport")).toHaveAttribute("tabindex", "-1");
    const stops = Array.from(mount.querySelectorAll<HTMLElement>("*")).filter((el) => el.tabIndex >= 0);
    expect(stops).toEqual([term().textarea]);
  });

  it("the pane focus region: a named group, out of the Tab order, the F6 entry of the pane region", () => {
    const { container } = render(<TerminalPane {...props()} />);
    const region = screen.getByRole("group", { name: "Terminal 07, 2 de 3" });
    expect(region).toHaveClass("terminal-pane__focus-region");
    expect(region).toHaveAttribute("tabindex", "-1");
    expect(region).toHaveAttribute("data-f6-entry");
    expect(region).toHaveAccessibleDescription("Enter: digitar no terminal. Tab: indicadores e botão fechar. F6: próxima região.");
    expect(container.querySelector(".terminal-pane")).toHaveAttribute("data-f6-region", "pane");
  });

  it("Enter and Space enter the terminal without sending anything; other keys do nothing", () => {
    const p = props();
    render(<TerminalPane {...p} />);
    const region = screen.getByRole("group", { name: "Terminal 07, 2 de 3" });
    region.focus();
    fireEvent.keyDown(region, { key: "a" });
    expect(term().focusCount).toBe(0);
    expect(fireEvent.keyDown(region, { key: "Enter" })).toBe(false); // default prevented
    expect(term().focusCount).toBe(1);
    expect(document.activeElement).toBe(term().textarea);
    region.focus();
    fireEvent.keyDown(region, { key: " " });
    expect(term().focusCount).toBe(2);
    expect(p.onWrite).not.toHaveBeenCalled();
  });

  it("a click on the focus region enters the terminal (screen readers in browse mode send a click)", async () => {
    const user = userEvent.setup();
    const p = props();
    render(<TerminalPane {...p} />);
    await user.click(screen.getByRole("group", { name: "Terminal 07, 2 de 3" }));
    expect(document.activeElement).toBe(term().textarea);
    expect(p.onWrite).not.toHaveBeenCalled();
  });

  it("Tab from the focus region goes to indicators, close, then the terminal", async () => {
    const user = userEvent.setup();
    render(<TerminalPane {...props()} />);
    screen.getByRole("group", { name: "Terminal 07, 2 de 3" }).focus();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /^Indicadores do Terminal 07:/ }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Fechar Terminal 07" }));
    await user.tab();
    expect(document.activeElement).toBe(term().textarea);
  });

  it("names the terminal input after its position and keeps it in step", () => {
    const { rerender, container } = render(<TerminalPane {...props()} />);
    expect(term().textarea).toHaveAttribute("aria-label", "Terminal 07, 2 de 3. F6 sai do terminal");
    rerender(<TerminalPane {...props({ position: 1, total: 2 })} />);
    expect(term().textarea).toHaveAttribute("aria-label", "Terminal 07, 1 de 2. F6 sai do terminal");
    expect(container.innerHTML).not.toContain("Terminal input");
  });

  it("F6 and Shift+F6 never reach the shell; the browser action is cancelled on keydown only", () => {
    render(<TerminalPane {...props()} />);
    expect(term().keyHandlerCount).toBe(1);
    for (const shiftKey of [false, true]) {
      const down = new KeyboardEvent("keydown", { key: "F6", shiftKey, cancelable: true });
      expect(term().keyHandler(down)).toBe(false);
      expect(down.defaultPrevented).toBe(true);
      for (const type of ["keypress", "keyup"]) {
        const other = new KeyboardEvent(type, { key: "F6", shiftKey, cancelable: true });
        expect(term().keyHandler(other)).toBe(false);
        expect(other.defaultPrevented).toBe(false);
      }
    }
  });

  it.each([
    ["Tab", {}],
    ["Tab", { shiftKey: true }],
    ["Escape", {}],
    ["c", { ctrlKey: true }],
    ["3", { ctrlKey: true }],
    ["1", { altKey: true }],
    ["F6", { altKey: true }],
    ["F6", { ctrlKey: true }],
  ])("%s %j goes to the shell untouched", (key, init) => {
    render(<TerminalPane {...props()} />);
    const event = new KeyboardEvent("keydown", { key, cancelable: true, ...init });
    expect(term().keyHandler(event)).toBe(true);
    expect(event.defaultPrevented).toBe(false);
  });

  it("Tab, Esc, Shift+Tab and Ctrl+C bytes reach onWrite exactly", () => {
    const p = props();
    render(<TerminalPane {...p} />);
    for (const data of ["\t", "\x1b", "\x1b[Z", "\x03"]) term().dataListeners.forEach((fn: (d: string) => void) => fn(data));
    expect((p.onWrite as ReturnType<typeof vi.fn>).mock.calls).toEqual([
      ["session-1", "\t"],
      ["session-1", "\x1b"],
      ["session-1", "\x1b[Z"],
      ["session-1", "\x03"],
    ]);
  });
});

describe("TerminalGrid tab strip", () => {
  const panes = (n: number) =>
    Array.from({ length: n }, (_, i) => ({ sessionId: `s-${i + 1}`, tabId: "tab-main", paneId: i, kind: "human_terminal" as const, number: [4, 9, 12, 13][i] }));
  const grid = (n: number, over: Record<string, unknown> = {}) => (
    <TerminalGrid
      tabId="tab-main"
      projectPath={"D:\\work\\ai-coordinator"}
      panes={panes(n)}
      onAddPane={vi.fn()}
      onSaveSessions={vi.fn()}
      onWrite={vi.fn()}
      onResize={vi.fn()}
      onClose={vi.fn()}
      {...over}
    />
  );

  it("shows the project folder (full path in the title) and the layout in words, not 'Aba:' or 'Raiz:'", () => {
    const { container, rerender } = render(grid(1));
    const label = container.querySelector(".tab-strip__label")!;
    expect(label).toHaveAttribute("title", "D:\\work\\ai-coordinator");
    expect(label).toHaveTextContent("ai-coordinator");
    const layouts: string[] = [];
    for (const n of [1, 2, 3, 4]) {
      rerender(grid(n));
      layouts.push(container.querySelector(".tab-strip__layout")!.textContent ?? "");
    }
    expect(layouts).toEqual(["Painel único", "Lado a lado", "Grade 2×2", "Grade 2×2"]);
    expect(container.textContent).not.toMatch(/Aba:|Raiz:|painéis/);
  });

  it("is the F6 strip region; its entry is Salvar sessões, or Novo terminal without it", () => {
    const { rerender } = render(grid(2));
    const strip = screen.getByRole("group", { name: "Aba do projeto" });
    expect(strip).toHaveAttribute("data-f6-region", "strip");
    expect(screen.getByRole("button", { name: "Salvar sessões" })).toHaveAttribute("data-f6-entry");
    expect(screen.getByRole("button", { name: "Novo terminal" })).not.toHaveAttribute("data-f6-entry");
    rerender(grid(2, { onSaveSessions: undefined }));
    expect(screen.getByRole("button", { name: "Novo terminal" })).toHaveAttribute("data-f6-entry");
  });

  it("the buttons call back with the tab, and Novo terminal disappears at 4 panes", async () => {
    const user = userEvent.setup();
    const onAddPane = vi.fn();
    const onSaveSessions = vi.fn();
    const { rerender } = render(grid(3, { onAddPane, onSaveSessions }));
    await user.click(screen.getByRole("button", { name: "Salvar sessões" }));
    await user.click(screen.getByRole("button", { name: "Novo terminal" }));
    expect(onSaveSessions).toHaveBeenCalledWith("tab-main");
    expect(onAddPane).toHaveBeenCalledWith("tab-main");
    rerender(grid(4, { onAddPane, onSaveSessions }));
    expect(screen.queryByRole("button", { name: "Novo terminal" })).toBeNull();
  });

  it("names every pane by its stable number, with its position alongside", () => {
    render(grid(3));
    for (const [i, n] of ["04", "09", "12"].entries()) {
      expect(screen.getByRole("group", { name: `Terminal ${n}, ${i + 1} de 3` })).toBeInTheDocument();
      expect(screen.getByRole("region", { name: `Terminal ${n}` })).toBeInTheDocument();
    }
  });

  it("passes each pane its live state, by session", () => {
    render(grid(2, { states: { "s-1": { kind: "alive" }, "s-2": { kind: "ended", exitCode: 0 } } }));
    expect(screen.getByRole("region", { name: "Terminal 04" })).toHaveClass("terminal-pane--alive");
    expect(screen.getByRole("region", { name: "Terminal 09" })).toHaveTextContent("encerrado, código 0");
  });
});

describe("TerminalPane identity, state and footer (Etapa 6)", () => {
  it("header: the stable number, the program and the live state; no percentage anywhere", () => {
    const { container } = render(<TerminalPane {...props({ state: { kind: "alive" } })} />);
    const header = container.querySelector(".terminal-pane__header")!;
    expect(header).toHaveTextContent("07 · Terminal");
    expect(header.querySelector(".terminal-pane__program")).toHaveTextContent("cmd.exe");
    expect(header.querySelector(".terminal-pane__state--alive .sr-only")).toHaveTextContent("ativo");
    expect(screen.getByRole("region", { name: "Terminal 07" })).toHaveClass("terminal-pane--slot-2");
    expect(container.textContent).not.toMatch(/%/);
  });

  it.each([
    [{ kind: "ended", exitCode: 0 }, "encerrado, código 0"],
    [{ kind: "ended", exitCode: null }, "encerrado, código desconhecido"],
    [{ kind: "unknown" }, "estado desconhecido"],
    [{ kind: "preview" }, "preview, sem processo"],
  ] as const)("a state other than alive is written out: %j", (state, text) => {
    const { container } = render(<TerminalPane {...props({ state })} />);
    const shown = container.querySelector(".terminal-pane__state")!;
    expect(shown).toHaveTextContent(text);
    expect(shown.querySelector(".sr-only")).toBeNull();
    expect(container.textContent).not.toMatch(/\bativo\b/);
  });

  it("no state given: unknown, never alive", () => {
    const { container } = render(<TerminalPane {...props()} />);
    expect(container.querySelector(".terminal-pane__state")).toHaveTextContent("estado desconhecido");
  });

  it("footer: program, Local, the folder (full path in the title), the PID and the kind", () => {
    const { container } = render(<TerminalPane {...props({ state: { kind: "alive" } })} />);
    const footer = container.querySelector(".terminal-pane__footer")!;
    expect(footer.querySelector(".terminal-pane__footer-program")).toHaveTextContent("cmd.exe");
    expect(footer).toHaveTextContent("Local");
    expect(footer.querySelector(".terminal-pane__footer-folder")).toHaveAttribute("title", "C:/work/proj");
    expect(footer.querySelector(".terminal-pane__footer-folder")).toHaveTextContent(/^proj$/);
    expect(footer).toHaveTextContent("PID 4321");
    expect(footer.querySelector(".terminal-pane__footer-end")).toHaveTextContent("Terminal humano");
  });

  it("what the host did not report is said to be unknown", () => {
    const { container } = render(<TerminalPane {...props({ program: undefined, pid: null, cwd: undefined })} />);
    const footer = container.querySelector(".terminal-pane__footer")!;
    expect(footer).toHaveTextContent("programa desconhecido");
    expect(footer).toHaveTextContent("PID indisponível");
    expect(container.querySelector(".terminal-pane__program")).toBeNull();
  });

  it("the preview has no process to describe", () => {
    const { container } = render(<TerminalPane {...props({ state: { kind: "preview" }, program: undefined, pid: null })} />);
    expect(container.querySelector(".terminal-pane__footer")).toHaveTextContent("Preview: sem processo");
    expect(container.querySelector(".terminal-pane__footer")).not.toHaveTextContent("PID");
  });

  it("a governed agent is named as such", () => {
    render(<TerminalPane {...props({ kind: "governed_agent" })} />);
    expect(screen.getByRole("region", { name: "Agente 07" })).toHaveTextContent("07 · Agente");
    expect(screen.getByRole("region", { name: "Agente 07" })).toHaveTextContent("Agente governado");
  });
});
