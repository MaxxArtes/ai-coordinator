/**
 * Terminal theme from the tokens (Etapa 5, decisions 2 and C): pure build, live switch on the same terminal instance,
 * Windows contrast themes (light and dark) with a higher minimum contrast.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyXtermAppearance,
  buildXtermTheme,
  DEFAULT_MIN_CONTRAST,
  FORCED_COLORS_MIN_CONTRAST,
  normalizeColor,
  readXtermTheme,
  resolveCssColor,
  watchXtermTheme,
  XTERM_THEME_TOKENS,
  type CssVarReader,
  type XtermAppearance,
} from "./xtermTheme";

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** A reader that answers like tokens.css: standard, low stimulation, or a contrast theme already resolved to hex. */
function fakeReader(state: { forced?: "light" | "dark" | null }): CssVarReader {
  return (name) => {
    if (state.forced === "light") {
      if (name === "--c-term-bg") return "rgb(255, 250, 239)";
      if (name === "--c-term-selection-fg") return "rgb(255, 245, 227)";
    }
    if (state.forced === "dark") {
      if (name === "--c-term-bg") return "rgb(0, 0, 0)";
      if (name === "--c-term-selection-fg") return "rgb(0, 0, 0)";
    }
    if (name === "--c-term-selection-fg") return "";
    const low = document.documentElement.dataset.stimulation === "low";
    if (name === "--c-term-bg") return low ? " #151820 " : "#0C0F16";
    return "#123456";
  };
}

/** window stand-in whose matchMedia answers and notifies like the engine. */
function fakeWindow(state: { forced: boolean; dark: boolean }) {
  const listeners = new Map<string, Set<() => void>>();
  const focusListeners = new Set<() => void>();
  const win = {
    matchMedia: (query: string) => ({
      get matches() {
        return query.includes("forced-colors") ? state.forced : state.dark;
      },
      addEventListener: (_type: string, fn: () => void) => {
        if (!listeners.has(query)) listeners.set(query, new Set());
        listeners.get(query)!.add(fn);
      },
      removeEventListener: (_type: string, fn: () => void) => listeners.get(query)?.delete(fn),
    }),
    addEventListener: (_type: string, fn: () => void) => focusListeners.add(fn),
    removeEventListener: (_type: string, fn: () => void) => focusListeners.delete(fn),
  };
  return {
    win: win as unknown as Window,
    fire: (match: string) => [...listeners].forEach(([q, fns]) => q.includes(match) && fns.forEach((fn) => fn())),
    focus: () => focusListeners.forEach((fn) => fn()),
    count: () => [...listeners.values()].reduce((n, s) => n + s.size, 0) + focusListeners.size,
  };
}

afterEach(() => {
  delete document.documentElement.dataset.stimulation;
});

describe("xterm theme from the design tokens", () => {
  it("builds the 21 colors from the --c-term-* tokens, trimmed and normalized", () => {
    const theme = buildXtermTheme(fakeReader({}));
    expect(Object.keys(theme).sort()).toEqual(Object.keys(XTERM_THEME_TOKENS).sort());
    expect(theme.background).toBe("#0c0f16");
    expect(theme.foreground).toBe("#123456");
  });

  it("normalizes the computed forms of a color and refuses keywords, translucent and invalid values", () => {
    expect(normalizeColor("rgb(255, 250, 239)")).toBe("#fffaef");
    expect(normalizeColor("rgba(0, 0, 0, 1)")).toBe("#000000");
    expect(normalizeColor("rgb(1 2 3)")).toBe("#010203");
    expect(normalizeColor("#ABC")).toBe("#aabbcc");
    expect(normalizeColor("rgba(0, 0, 0, 0)")).toBe("");
    expect(normalizeColor("Canvas")).toBe("");
    expect(normalizeColor("")).toBe("");
    const theme = buildXtermTheme(() => "Canvas");
    expect(theme).toEqual({});
  });

  it("every read hands over a new object (xterm compares the theme by reference)", () => {
    const read = fakeReader({});
    const a = readXtermTheme(read);
    const b = readXtermTheme(read);
    expect(a).not.toBe(b);
    expect(a).toEqual(b);
  });

  it("a contrast theme adds the selected text color", () => {
    const theme = buildXtermTheme(fakeReader({ forced: "light" }));
    expect(theme.background).toBe("#fffaef");
    expect(theme.selectionForeground).toBe("#fff5e3");
    expect(Object.keys(theme)).toHaveLength(22);
  });

  it("applies a new appearance to the same terminal instance (no new terminal, no lost session)", () => {
    let assignments = 0;
    let theme: unknown;
    const options = {
      get theme() {
        return theme;
      },
      set theme(value: unknown) {
        assignments++;
        theme = value;
      },
      minimumContrastRatio: 1,
    };
    const term = { options };
    const first: XtermAppearance = { theme: readXtermTheme(fakeReader({})), minimumContrastRatio: 1 };
    applyXtermAppearance(term as never, first);
    const second: XtermAppearance = { theme: readXtermTheme(fakeReader({ forced: "light" })), minimumContrastRatio: 7 };
    applyXtermAppearance(term as never, second);
    expect(assignments).toBe(2);
    expect(options.theme).toBe(second.theme);
    expect(options.minimumContrastRatio).toBe(7);
  });
});

describe("watchXtermTheme", () => {
  it("re-themes when the low-stimulation mode changes, and only when the colors really change", async () => {
    const { win } = fakeWindow({ forced: false, dark: true });
    const seen: XtermAppearance[] = [];
    const stop = watchXtermTheme((a) => seen.push(a), document.documentElement, fakeReader({}), win);

    document.documentElement.dataset.stimulation = "low";
    await flush();
    expect(seen).toHaveLength(1);
    expect(seen[0].theme.background).toBe("#151820");
    expect(seen[0].minimumContrastRatio).toBe(DEFAULT_MIN_CONTRAST);

    document.documentElement.dataset.stimulation = "low"; // same value
    document.documentElement.setAttribute("data-other", "x"); // another attribute
    await flush();
    expect(seen).toHaveLength(1);

    delete document.documentElement.dataset.stimulation;
    await flush();
    expect(seen).toHaveLength(2);
    expect(seen[1].theme.background).toBe("#0c0f16");
    expect(seen[1].theme).not.toBe(seen[0].theme);

    stop();
    document.documentElement.dataset.stimulation = "low";
    await flush();
    expect(seen).toHaveLength(2);
    document.documentElement.removeAttribute("data-other");
  });

  it("follows the Windows contrast theme: on, light/dark switch, a switch made outside the app, and off", () => {
    const media = { forced: false, dark: true };
    const colors: { forced: "light" | "dark" | null } = { forced: null };
    const fake = fakeWindow(media);
    const seen: XtermAppearance[] = [];
    const stop = watchXtermTheme((a) => seen.push(a), document.documentElement, fakeReader(colors), fake.win);
    expect(fake.count()).toBe(3);

    media.forced = true;
    colors.forced = "dark";
    fake.fire("forced-colors");
    expect(seen).toHaveLength(1);
    expect(seen[0].minimumContrastRatio).toBe(FORCED_COLORS_MIN_CONTRAST);
    expect(seen[0].theme.background).toBe("#000000");

    media.dark = false;
    colors.forced = "light";
    fake.fire("prefers-color-scheme");
    expect(seen).toHaveLength(2);
    expect(seen[1].theme.background).toBe("#fffaef");
    expect(seen[1].theme.selectionForeground).toBe("#fff5e3");

    fake.focus(); // nothing changed: no re-theme
    expect(seen).toHaveLength(2);

    colors.forced = "dark"; // switched to another contrast theme in the Windows settings, then back to the app
    fake.focus();
    expect(seen).toHaveLength(3);

    media.forced = false;
    colors.forced = null;
    fake.fire("forced-colors");
    expect(seen).toHaveLength(4);
    expect(seen[3].minimumContrastRatio).toBe(DEFAULT_MIN_CONTRAST);
    expect(seen[3].theme).not.toHaveProperty("selectionForeground");

    stop();
    expect(fake.count()).toBe(0);
  });
});

describe("resolveCssColor", () => {
  it("resolves through a probe element that is removed afterwards", () => {
    const spy = vi.spyOn(window, "getComputedStyle").mockReturnValue({ color: "rgb(255, 250, 239)" } as CSSStyleDeclaration);
    const before = document.body.childElementCount;
    expect(resolveCssColor("red")).toBe("rgb(255, 250, 239)");
    expect(spy).toHaveBeenCalledTimes(1);
    const probe = spy.mock.calls[0][0] as HTMLElement;
    expect(probe.style.getPropertyValue("forced-color-adjust")).toBe("none");
    expect(probe.isConnected).toBe(false);
    expect(document.body.childElementCount).toBe(before);
  });

  it("does not guess when the engine refuses the value", () => {
    const spy = vi.spyOn(window, "getComputedStyle");
    expect(resolveCssColor("not-a-color")).toBe("");
    expect(spy).not.toHaveBeenCalled();
  });
});
