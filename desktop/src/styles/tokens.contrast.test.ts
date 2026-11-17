// @vitest-environment node
/**
 * Contrast of the design tokens, recomputed from tokens.css itself (Etapa 5, decision 2).
 * Text pairs need 4.5:1; borders, focus rings and the cursor need 3:1 (WCAG 2.x, non-text contrast).
 */
import { describe, expect, it } from "vitest";
import { composite, contrastRatio, parseColor, readSrc, resolveToken, tokensFor, type TokenMap } from "./testing/cssSource";
import { buildXtermTheme, XTERM_THEME_TOKENS } from "./xtermTheme";

const CSS = readSrc("styles/tokens.css");

const SURFACES = [
  "--c-bg",
  "--c-chrome",
  "--c-surface",
  "--c-surface-raised",
  "--c-surface-overlay",
  "--c-surface-hover",
];
const STATUS = ["--c-ok", "--c-warn", "--c-err", "--c-info", "--c-agent", "--c-channel"];
const TINTED = ["ok", "warn", "err", "info"];
const ANSI_TEXT = Object.values(XTERM_THEME_TOKENS).filter(
  (t) => /^--c-term-(bright-)?(black|red|green|yellow|blue|magenta|cyan|white)$/.test(t) && t !== "--c-term-black",
);

/** [foreground, background, minimum, opaque base under a translucent background] */
type Pair = [string, string, number, string?];

function pairs(): Pair[] {
  const out: Pair[] = [];
  for (const fg of ["--c-fg", "--c-fg-muted", "--c-fg-subtle", "--c-accent"]) {
    for (const bg of SURFACES) out.push([fg, bg, 4.5]);
  }
  out.push(["--c-fg", "--c-nav-inset", 4.5], ["--c-fg-muted", "--c-nav-inset", 4.5], ["--c-accent", "--c-brand-bg", 4.5]);
  for (const bg of ["--c-accent", "--c-accent-hover", "--c-accent-strong", "--c-err"]) out.push(["--c-on-accent", bg, 4.5]);
  for (const fg of STATUS) {
    for (const bg of ["--c-chrome", "--c-surface", "--c-surface-raised", "--c-surface-overlay"]) out.push([fg, bg, 4.5]);
  }
  for (const name of TINTED) {
    for (const base of ["--c-chrome", "--c-surface", "--c-surface-raised"]) {
      out.push([`--c-${name}`, `--c-${name}-bg`, 4.5, base], ["--c-fg", `--c-${name}-bg`, 4.5, base]);
    }
  }
  out.push(["--c-ok", "--c-pill-ok-bg", 4.5]);
  out.push(["--c-term-fg", "--c-term-bg", 4.5], ["--c-term-fg", "--c-term-selection", 4.5]);
  for (const fg of ANSI_TEXT) out.push([fg, "--c-term-bg", 4.5]);
  for (const fg of ["--c-border-control", "--c-focus"]) {
    for (const bg of SURFACES) out.push([fg, bg, 3]);
  }
  out.push(["--c-pane-focus", "--c-bg", 3], ["--c-pane-focus", "--c-surface", 3]);
  for (const bg of ["--c-chrome", "--c-nav-inset", "--c-surface"]) out.push(["--c-accent-strong", bg, 3]);
  out.push(["--c-term-cursor", "--c-term-bg", 3]);
  return out;
}

function failures(tokens: TokenMap): string[] {
  const out: string[] = [];
  for (const [fg, bg, min, base] of pairs()) {
    let ratio: number;
    try {
      const back = base ? composite(parseColor(resolveToken(tokens, bg)), parseColor(resolveToken(tokens, base))) : parseColor(resolveToken(tokens, bg));
      ratio = contrastRatio(parseColor(resolveToken(tokens, fg)), back);
    } catch (err) {
      out.push(`${fg} / ${bg}${base ? ` over ${base}` : ""}: ${(err as Error).message}`);
      continue;
    }
    if (ratio < min) out.push(`${fg} / ${bg}${base ? ` over ${base}` : ""}: ${ratio.toFixed(2)} < ${min}`);
  }
  return out;
}

const MODES = {
  standard: tokensFor(CSS),
  low: tokensFor(CSS, { low: true }),
};

describe("design tokens", () => {
  it("reads the real tokens.css", () => {
    const colors = [...MODES.standard.keys()].filter((k) => k.startsWith("--c-"));
    expect(colors.length).toBeGreaterThanOrEqual(55);
    expect(CSS).toContain(':root[data-stimulation="low"]');
    expect(pairs().length).toBeGreaterThanOrEqual(110);
  });

  it("low stimulation redefines every --c-* and removes glow and motion", () => {
    const low = tokensFor(CSS, { low: true });
    const lowOnly = new Map([...low].filter(([k, v]) => MODES.standard.get(k) !== v));
    const notRedefined = [...MODES.standard.keys()].filter((k) => k.startsWith("--c-") && !lowOnly.has(k));
    expect(notRedefined).toEqual([]);
    expect(low.get("--shadow-dialog")).toBe("none");
    expect(low.get("--shadow-pane-focus")).not.toMatch(/rgba|\d+px \d+px \d+px/);
    for (const motion of ["--motion-fast", "--motion", "--motion-spin"]) expect(low.get(motion)).toBe("0ms");
  });

  it.each(Object.keys(MODES))("contrast in the %s mode: no failure and no missing pair", (mode) => {
    expect(failures(MODES[mode as keyof typeof MODES])).toEqual([]);
  });

  it("--c-term-* are literal #rrggbb and the terminal background is --c-surface in both modes", () => {
    for (const tokens of Object.values(MODES)) {
      for (const token of Object.values(XTERM_THEME_TOKENS)) expect(tokens.get(token)).toMatch(/^#[0-9a-f]{6}$/);
      expect(tokens.get("--c-term-bg")).toBe(tokens.get("--c-surface"));
    }
  });

  it("buildXtermTheme with the real tokens gives the 21 colors in both modes", () => {
    const themes = Object.values(MODES).map((tokens) => buildXtermTheme((name) => tokens.get(name) ?? ""));
    for (const theme of themes) {
      expect(Object.keys(theme)).toHaveLength(21);
      for (const color of Object.values(theme)) expect(color).toMatch(/^#[0-9a-f]{6}$/);
      expect(theme).not.toHaveProperty("selectionForeground");
    }
    expect(themes[0].background).not.toBe(themes[1].background);
  });
});

describe("Windows contrast themes (forced colors)", () => {
  /** Representative system palettes: "Desert"-like and plain white (light); "Night sky", "Aquatic" and "Dusk"-like (dark). */
  const LIGHT = [
    { Canvas: "#fffaef", CanvasText: "#3d3d3d", Highlight: "#903909", HighlightText: "#fff5e3", ButtonBorder: "#3d3d3d" },
    { Canvas: "#ffffff", CanvasText: "#000000", Highlight: "#37006e", HighlightText: "#ffffff", ButtonBorder: "#000000" },
  ];
  const DARK = [
    { Canvas: "#000000", CanvasText: "#ffffff", Highlight: "#8080ff", HighlightText: "#000000", ButtonBorder: "#ffffff" },
    { Canvas: "#202020", CanvasText: "#ffffff", Highlight: "#8ee3f0", HighlightText: "#263b50", ButtonBorder: "#ffffff" },
    { Canvas: "#2d3236", CanvasText: "#b6f6f0", Highlight: "#a6e7f8", HighlightText: "#2d3236", ButtonBorder: "#b6f6f0" },
  ];

  it("focus, pane focus, accent line and control borders become system colors in both stimulation modes", () => {
    for (const low of [false, true]) {
      const t = tokensFor(CSS, { forced: true, low });
      expect(t.get("--c-focus")).toBe("Highlight");
      expect(t.get("--c-pane-focus")).toBe("Highlight");
      expect(t.get("--c-accent-strong")).toBe("Highlight");
      expect(t.get("--c-border-control")).toBe("ButtonBorder");
      expect(t.get("--shadow-pane-focus")).toBe("none");
    }
  });

  it("the terminal follows the system palette: background, text, cursor and selection are system colors", () => {
    for (const light of [false, true]) {
      const t = tokensFor(CSS, { forced: true, light });
      expect(t.get("--c-term-bg")).toBe("Canvas");
      expect(t.get("--c-term-fg")).toBe("CanvasText");
      expect(t.get("--c-term-cursor")).toBe("CanvasText");
      expect(t.get("--c-term-cursor-accent")).toBe("Canvas");
      expect(t.get("--c-term-selection")).toBe("Highlight");
      expect(t.get("--c-term-selection-fg")).toBe("HighlightText");
    }
  });

  it("light contrast themes get dark ANSI colors: all 16 readable (4.5:1) on light system backgrounds", () => {
    const t = tokensFor(CSS, { forced: true, light: true });
    const ansi = Object.values(XTERM_THEME_TOKENS).filter((k) => /black|red|green|yellow|blue|magenta|cyan|white/.test(k));
    expect(ansi).toHaveLength(16);
    const bad: string[] = [];
    for (const palette of LIGHT) {
      const bg = parseColor(resolveToken(t, "--c-term-bg"), palette);
      for (const token of ansi) {
        const ratio = contrastRatio(parseColor(resolveToken(t, token), palette), bg);
        if (ratio < 4.5) bad.push(`${token} on ${palette.Canvas}: ${ratio.toFixed(2)}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("dark contrast themes get bright ANSI colors: the 15 text colors readable on dark system backgrounds", () => {
    for (const low of [false, true]) {
      const t = tokensFor(CSS, { forced: true, low });
      const bad: string[] = [];
      for (const palette of DARK) {
        const bg = parseColor(resolveToken(t, "--c-term-bg"), palette);
        for (const token of ANSI_TEXT) {
          const ratio = contrastRatio(parseColor(resolveToken(t, token), palette), bg);
          if (ratio < 4.5) bad.push(`${token} on ${palette.Canvas}: ${ratio.toFixed(2)}`);
        }
      }
      expect(bad).toEqual([]);
    }
  });

  it("the contrast palettes apply only inside forced colors, the light one only with a light system background", () => {
    expect(MODES.standard.get("--c-term-white")).toBe("#d4d4d8");
    expect(MODES.low.get("--c-term-white")).toBe("#c9ccd4");
    expect(tokensFor(CSS, { forced: true }).get("--c-term-white")).toBe("#e5e5e5");
    expect(tokensFor(CSS, { forced: true, low: true }).get("--c-term-white")).toBe("#e5e5e5");
    expect(tokensFor(CSS, { forced: true, light: true }).get("--c-term-white")).toBe("#3d3d3d");
    expect(tokensFor(CSS, { forced: true, light: true, low: true }).get("--c-term-white")).toBe("#3d3d3d");
    expect(MODES.standard.has("--c-term-selection-fg")).toBe(false);
  });
});
