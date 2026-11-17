/**
 * Terminal theme built from the design tokens (Marco 4D.3, Etapa 5).
 *
 * - The colors live in tokens.css (--c-term-*), so the terminal follows the standard mode, the low-stimulation mode
 *   and the Windows contrast themes, light or dark.
 * - In a contrast theme the tokens are system colors (Canvas, CanvasText, Highlight...): they are resolved to #rrggbb
 *   through a probe element, and xterm's minimumContrastRatio goes up to 7, so every cell stays readable whatever
 *   colors a program asks for.
 * - A change is applied to the open terminals in place (term.options), never by recreating them: the session, the
 *   buffer and the scrollback are kept. Each change hands over a NEW theme object, because xterm compares the
 *   option by reference.
 * - The terminal font (family and size) is not touched here.
 */
import type { ITheme, Terminal } from "xterm";

export type CssVarReader = (name: string) => string;

/** The 21 colors every mode defines. */
export const XTERM_THEME_TOKENS = {
  background: "--c-term-bg",
  foreground: "--c-term-fg",
  cursor: "--c-term-cursor",
  cursorAccent: "--c-term-cursor-accent",
  selectionBackground: "--c-term-selection",
  black: "--c-term-black",
  red: "--c-term-red",
  green: "--c-term-green",
  yellow: "--c-term-yellow",
  blue: "--c-term-blue",
  magenta: "--c-term-magenta",
  cyan: "--c-term-cyan",
  white: "--c-term-white",
  brightBlack: "--c-term-bright-black",
  brightRed: "--c-term-bright-red",
  brightGreen: "--c-term-bright-green",
  brightYellow: "--c-term-bright-yellow",
  brightBlue: "--c-term-bright-blue",
  brightMagenta: "--c-term-bright-magenta",
  brightCyan: "--c-term-bright-cyan",
  brightWhite: "--c-term-bright-white",
} as const;

/** Colors only some modes define (the selected text color exists only in the contrast themes). */
export const XTERM_OPTIONAL_THEME_TOKENS = {
  selectionForeground: "--c-term-selection-fg",
} as const;

export type XtermThemeKey = keyof typeof XTERM_THEME_TOKENS | keyof typeof XTERM_OPTIONAL_THEME_TOKENS;
export type XtermTheme = Pick<ITheme, XtermThemeKey>;

/** xterm's default: colors are drawn as the program asks. */
export const DEFAULT_MIN_CONTRAST = 1;
/** Windows contrast themes: xterm adjusts any cell below 7:1 (WCAG AAA) against its own background. */
export const FORCED_COLORS_MIN_CONTRAST = 7;

export interface XtermAppearance {
  theme: XtermTheme;
  minimumContrastRatio: number;
}

const HEX6 = /^#[0-9a-f]{6}$/;
const HEX3 = /^#[0-9a-f]{3}$/;
const RGB = /^rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})\s*(?:[,/]\s*([\d.]+)(%?)\s*)?\)$/;

/** "#abc", "#aabbcc", "rgb(…)" or an opaque "rgba(…)" → "#rrggbb"; anything else (empty, keyword, translucent) → "". */
export function normalizeColor(value: string): string {
  const v = value.trim().toLowerCase();
  if (HEX6.test(v)) return v;
  if (HEX3.test(v)) {
    return "#" + v.slice(1).split("").map((c) => c + c).join("");
  }
  const m = RGB.exec(v);
  if (!m) return "";
  if (m[4] !== undefined) {
    const alpha = Number(m[4]) / (m[5] === "%" ? 100 : 1);
    if (alpha < 1) return "";
  }
  return "#" + [m[1], m[2], m[3]].map((n) => Math.min(255, Number(n)).toString(16).padStart(2, "0")).join("");
}

type AnsiKey = Exclude<keyof typeof XTERM_THEME_TOKENS, "background" | "foreground" | "cursor" | "cursorAccent" | "selectionBackground">;

/**
 * Dark ANSI set for light contrast themes. Only the contrast themes define these tokens; they replace the ANSI colors
 * when the resolved background is light. The decision comes from the background itself, not from
 * prefers-color-scheme, which the host window theme may keep "dark" under a light contrast theme.
 */
export const XTERM_LIGHT_ANSI_TOKENS: Record<AnsiKey, string> = {
  black: "--c-term-light-black",
  red: "--c-term-light-red",
  green: "--c-term-light-green",
  yellow: "--c-term-light-yellow",
  blue: "--c-term-light-blue",
  magenta: "--c-term-light-magenta",
  cyan: "--c-term-light-cyan",
  white: "--c-term-light-white",
  brightBlack: "--c-term-light-bright-black",
  brightRed: "--c-term-light-bright-red",
  brightGreen: "--c-term-light-bright-green",
  brightYellow: "--c-term-light-bright-yellow",
  brightBlue: "--c-term-light-bright-blue",
  brightMagenta: "--c-term-light-bright-magenta",
  brightCyan: "--c-term-light-bright-cyan",
  brightWhite: "--c-term-light-bright-white",
};

/** WCAG relative luminance of a "#rrggbb" color. */
export function relativeLuminance(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  const lin = (c: number) => {
    const v = c / 255;
    return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
}

/** A background on which dark text reads better than light text. */
export const isLightBackground = (hex: string): boolean => relativeLuminance(hex) > 0.18;

/**
 * Pure: a NEW object on every call. A token that is missing or not a color is left out, so xterm keeps its own
 * default for that key instead of receiving an invalid value.
 */
export function buildXtermTheme(read: CssVarReader): XtermTheme {
  const theme: XtermTheme = {};
  const all: Record<string, string> = { ...XTERM_THEME_TOKENS, ...XTERM_OPTIONAL_THEME_TOKENS };
  for (const key of Object.keys(all) as XtermThemeKey[]) {
    const color = normalizeColor(read(all[key]));
    if (color) theme[key] = color;
  }
  if (theme.background && isLightBackground(theme.background)) {
    for (const key of Object.keys(XTERM_LIGHT_ANSI_TOKENS) as AnsiKey[]) {
      const color = normalizeColor(read(XTERM_LIGHT_ANSI_TOKENS[key]));
      if (color) theme[key] = color;
    }
  }
  return theme;
}

const LITERAL_COLOR = /^(#|rgba?\()/i;

/** Resolves any CSS color (a system color included) to its computed value, through a temporary probe element. */
export function resolveCssColor(value: string, root: HTMLElement = document.documentElement): string {
  const doc = root.ownerDocument;
  const probe = doc.createElement("span");
  probe.setAttribute("aria-hidden", "true");
  probe.style.setProperty("position", "absolute");
  probe.style.setProperty("visibility", "hidden");
  probe.style.setProperty("forced-color-adjust", "none");
  probe.style.setProperty("color", value);
  // An invalid value is ignored by the engine: the probe would just inherit a color that is not the token's.
  if (!probe.style.getPropertyValue("color")) return "";
  (doc.body ?? root).appendChild(probe);
  try {
    return getComputedStyle(probe).color;
  } finally {
    probe.remove();
  }
}

/** Reads the tokens resolved on <html> (data-stimulation and the contrast theme included). */
export function rootCssVarReader(root: HTMLElement = document.documentElement): CssVarReader {
  const style = getComputedStyle(root);
  return (name) => {
    const raw = style.getPropertyValue(name).trim();
    if (!raw || LITERAL_COLOR.test(raw)) return raw;
    return resolveCssColor(raw, root);
  };
}

export function readXtermTheme(read?: CssVarReader): XtermTheme {
  return buildXtermTheme(read ?? rootCssVarReader());
}

export function isForcedColors(win: Window = window): boolean {
  return typeof win.matchMedia === "function" && win.matchMedia("(forced-colors: active)").matches;
}

export function xtermMinimumContrastRatio(win: Window = window): number {
  return isForcedColors(win) ? FORCED_COLORS_MIN_CONTRAST : DEFAULT_MIN_CONTRAST;
}

export function readXtermAppearance(read?: CssVarReader, win: Window = window): XtermAppearance {
  return { theme: readXtermTheme(read), minimumContrastRatio: xtermMinimumContrastRatio(win) };
}

/** Re-themes an open terminal in place: same instance, same session, same buffer. */
export function applyXtermAppearance(term: Pick<Terminal, "options">, appearance: XtermAppearance): void {
  term.options.theme = appearance.theme;
  term.options.minimumContrastRatio = appearance.minimumContrastRatio;
}

function signature(appearance: XtermAppearance): string {
  return JSON.stringify(appearance);
}

/**
 * Calls onChange with a new appearance whenever the effective terminal colors change:
 * - <html data-stimulation> (low-stimulation mode);
 * - the Windows contrast theme turned on or off (forced-colors) or switched between light and dark;
 * - the window regaining focus, which covers a switch between two contrast themes of the same lightness
 *   (made in the Windows settings, outside the app).
 * Nothing is called when the colors did not really change. Returns the function that stops watching.
 */
export function watchXtermTheme(
  onChange: (appearance: XtermAppearance) => void,
  root: HTMLElement = document.documentElement,
  read?: CssVarReader,
  win: Window = window,
): () => void {
  const current = () => readXtermAppearance(read ?? rootCssVarReader(root), win);
  let last = signature(current());
  const check = () => {
    const next = current();
    const sig = signature(next);
    if (sig === last) return;
    last = sig;
    onChange(next);
  };

  const observer = new MutationObserver(check);
  observer.observe(root, { attributes: true, attributeFilter: ["data-stimulation"] });
  const queries =
    typeof win.matchMedia === "function"
      ? ["(forced-colors: active)", "(prefers-color-scheme: dark)"].map((q) => win.matchMedia(q))
      : [];
  for (const query of queries) query.addEventListener("change", check);
  win.addEventListener("focus", check);

  return () => {
    observer.disconnect();
    for (const query of queries) query.removeEventListener("change", check);
    win.removeEventListener("focus", check);
  };
}
