/**
 * TerminalPane with a stand-in xterm (jsdom has no canvas nor layout).
 * Etapa 5, decision C: a theme change (low stimulation, Windows contrast theme) re-themes the open terminal in place;
 * the terminal, its session and its listeners are never recreated.
 */
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const xterm = vi.hoisted(() => {
  const instances: FakeTerminal[] = [];
  class FakeTerminal {
    options: Record<string, unknown>;
    themeAssignments = 0;
    disposed = false;
    cols = 80;
    rows = 24;
    dataListeners: Array<(data: string) => void> = [];
    element: HTMLElement | undefined;
    textarea: HTMLTextAreaElement | undefined;
    constructor(options: Record<string, unknown>) {
      const self = this;
      let theme = options.theme;
      this.options = { ...options };
      Object.defineProperty(this.options, "theme", {
        get: () => theme,
        set: (value) => {
          self.themeAssignments++;
          theme = value;
        },
      });
      instances.push(this);
    }
    loadAddon() {}
    open(parent: HTMLElement) {
      this.element = document.createElement("div");
      this.element.className = "xterm";
      this.textarea = document.createElement("textarea");
      this.textarea.className = "xterm-helper-textarea";
      this.element.appendChild(this.textarea);
      parent.appendChild(this.element);
    }
    onData(fn: (data: string) => void) {
      this.dataListeners.push(fn);
      return { dispose: () => (this.dataListeners = this.dataListeners.filter((f) => f !== fn)) };
    }
    attachCustomKeyEventHandler() {}
    focus() {
      this.textarea?.focus();
    }
    dispose() {
      this.disposed = true;
    }
  }
  return { instances, FakeTerminal };
});

vi.mock("xterm", () => ({ Terminal: xterm.FakeTerminal }));
vi.mock("xterm-addon-fit", () => ({
  FitAddon: class {
    fit() {}
  },
}));

import { TerminalPane } from "./TerminalPane";

class FakeResizeObserver {
  observe() {}
  disconnect() {}
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** The tokens the jsdom page would resolve: standard or low stimulation. */
function stubTokens() {
  const real = window.getComputedStyle;
  vi.spyOn(window, "getComputedStyle").mockImplementation((el: Element) => {
    if (el !== document.documentElement) return real(el);
    const low = document.documentElement.dataset.stimulation === "low";
    return { getPropertyValue: (name: string) => (name === "--c-term-bg" ? (low ? "#151820" : "#0c0f16") : name.startsWith("--c-term-") && name !== "--c-term-selection-fg" ? "#d4d4d8" : "") } as unknown as CSSStyleDeclaration;
  });
}

function renderPane(onWrite = vi.fn()) {
  return render(
    <TerminalPane sessionId="session-1" tabId="tab-1" paneId={0} kind="human_terminal" onWrite={onWrite} onResize={vi.fn()} onClose={vi.fn()} />,
  );
}

beforeEach(() => {
  xterm.instances.length = 0;
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  stubTokens();
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.stimulation;
});

describe("TerminalPane theme", () => {
  it("starts with the token palette (no literal colors) and xterm's own font", () => {
    renderPane();
    expect(xterm.instances).toHaveLength(1);
    const options = xterm.instances[0].options as { theme: Record<string, string>; fontSize: number; fontFamily: string; minimumContrastRatio: number };
    expect(options.theme.background).toBe("#0c0f16");
    expect(Object.keys(options.theme)).toHaveLength(21);
    expect(options.fontSize).toBe(13);
    expect(options.fontFamily).toBe("'Cascadia Code', 'Fira Code', 'Consolas', monospace");
    expect(options.minimumContrastRatio).toBe(1);
  });

  it("switching to low stimulation re-themes the same terminal: no new instance, no dispose, input still flows", async () => {
    const onWrite = vi.fn();
    renderPane(onWrite);
    const term = xterm.instances[0];
    const before = term.options.theme;

    await act(async () => {
      document.documentElement.dataset.stimulation = "low";
      await flush();
    });

    expect(xterm.instances).toHaveLength(1);
    expect(term.disposed).toBe(false);
    expect(term.themeAssignments).toBe(1);
    expect(term.options.theme).not.toBe(before);
    expect((term.options.theme as Record<string, string>).background).toBe("#151820");

    term.dataListeners.forEach((fn) => fn("dir\r"));
    expect(onWrite).toHaveBeenCalledWith("session-1", "dir\r");

    await act(async () => {
      delete document.documentElement.dataset.stimulation;
      await flush();
    });
    expect(term.themeAssignments).toBe(2);
    expect((term.options.theme as Record<string, string>).background).toBe("#0c0f16");
    expect(xterm.instances).toHaveLength(1);
  });

  it("stops watching the theme when the pane closes", async () => {
    const view = renderPane();
    const term = xterm.instances[0];
    view.unmount();
    expect(term.disposed).toBe(true);
    await act(async () => {
      document.documentElement.dataset.stimulation = "low";
      await flush();
    });
    expect(term.themeAssignments).toBe(0);
  });
});
