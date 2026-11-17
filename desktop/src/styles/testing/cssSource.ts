/**
 * Test-only helpers: read the sources of desktop/src and analyse the CSS (rules, tokens, colors, WCAG contrast).
 * Used by tokens.contrast.test.ts and css.rules.test.ts (both run in the node environment). Never imported by the app.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

/** desktop/src */
export const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

export function readSrc(rel: string): string {
  return readFileSync(join(SRC_DIR, rel), "utf8");
}

/** Every file under desktop/src whose posix relative path satisfies pred, sorted. */
export function listSrc(pred: (rel: string) => boolean): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else {
        const rel = relative(SRC_DIR, full).split(sep).join("/");
        if (pred(rel)) out.push(rel);
      }
    }
  };
  walk(SRC_DIR);
  return out.sort();
}

export function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

export interface CssRule {
  selector: string;
  body: string;
  /** Prelude of the enclosing at-rule ("@media (forced-colors: active)"), or null at the top level. */
  media: string | null;
}

/** Brace scanner: top-level rules and rules nested in @media / @supports / @keyframes. */
export function parseRules(css: string): CssRule[] {
  const rules: CssRule[] = [];
  const walk = (src: string, media: string | null) => {
    let i = 0;
    while (i < src.length) {
      const open = src.indexOf("{", i);
      if (open < 0) break;
      let prelude = src.slice(i, open);
      prelude = prelude.slice(prelude.lastIndexOf(";") + 1).trim(); // drop @import and other statements
      let depth = 1;
      let j = open + 1;
      while (j < src.length && depth > 0) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}") depth--;
        j++;
      }
      const body = src.slice(open + 1, j - 1);
      if (prelude.startsWith("@")) walk(body, prelude);
      else rules.push({ selector: prelude.replace(/\s+/g, " "), body: body.trim(), media });
      i = j;
    }
  };
  walk(stripComments(css), null);
  return rules;
}

/** "a: b; c: d(e; f)" → Map {a → b, c → d(e; f)}. Later declarations win. */
export function declarations(body: string): Map<string, string> {
  const out = new Map<string, string>();
  let depth = 0;
  let start = 0;
  const push = (chunk: string) => {
    const colon = chunk.indexOf(":");
    if (colon < 0) return;
    const prop = chunk.slice(0, colon).trim();
    const value = chunk.slice(colon + 1).trim();
    if (prop) out.set(prop, value);
  };
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (ch === "(") depth++;
    else if (ch === ")") depth--;
    else if (ch === ";" && depth === 0) {
      push(body.slice(start, i));
      start = i + 1;
    }
  }
  push(body.slice(start));
  return out;
}

export type TokenMap = Map<string, string>;

export interface TokenModeOptions {
  low?: boolean;
  forced?: boolean;
  /** Windows contrast theme with a light background (prefers-color-scheme: light inside forced colors). */
  light?: boolean;
}

const ROOT = ":root";
const ROOT_LOW = ':root[data-stimulation="low"]';

function selectorList(selector: string): string[] {
  return selector.split(",").map((s) => s.trim());
}

/** The custom properties in force on <html> for one mode, in cascade order. */
export function tokensFor(css: string, opts: TokenModeOptions = {}): TokenMap {
  const rules = parseRules(css);
  const map: TokenMap = new Map();
  const apply = (rule: CssRule) => {
    declarations(rule.body).forEach((value, prop) => {
      if (prop.startsWith("--")) map.set(prop, value);
    });
  };
  for (const rule of rules) {
    const sels = selectorList(rule.selector);
    const media = rule.media ?? "";
    const forcedBlock = media.includes("forced-colors: active");
    const lightBlock = media.includes("prefers-color-scheme: light");
    const darkBlock = media.includes("prefers-color-scheme: dark");
    let applies: boolean;
    if (rule.media === null) {
      applies = sels.includes(ROOT) || (!!opts.low && sels.includes(ROOT_LOW));
    } else if (forcedBlock) {
      if (!opts.forced) continue;
      if (lightBlock && !opts.light) continue;
      if (darkBlock && opts.light) continue;
      applies = sels.includes(ROOT) || sels.includes(ROOT_LOW);
    } else {
      applies = false; // prefers-reduced-motion and other environment blocks are not color modes
    }
    if (applies) apply(rule);
  }
  return map;
}

/** Follows var(--x) references; throws on a missing token or a cycle. */
export function resolveToken(tokens: TokenMap, name: string, seen: string[] = []): string {
  if (seen.includes(name)) throw new Error(`cycle: ${seen.concat(name).join(" -> ")}`);
  const value = tokens.get(name);
  if (value === undefined) throw new Error(`missing token ${name}`);
  return value.replace(/var\((--[\w-]+)\)/g, (_m, ref: string) => resolveToken(tokens, ref, seen.concat(name)));
}

export type Rgba = [number, number, number, number];

/** #rgb, #rrggbb, rgb(), rgba(); a system color only through the given palette. Anything else throws. */
export function parseColor(value: string, system: Record<string, string> = {}): Rgba {
  const v = value.trim();
  const sys = Object.keys(system).find((k) => k.toLowerCase() === v.toLowerCase());
  if (sys) return parseColor(system[sys]);
  let m = /^#([0-9a-f]{3})$/i.exec(v);
  if (m) {
    const [r, g, b] = m[1].split("").map((c) => parseInt(c + c, 16));
    return [r, g, b, 1];
  }
  m = /^#([0-9a-f]{6})$/i.exec(v);
  if (m) {
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
  }
  m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(v);
  if (m) return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] === undefined ? 1 : Number(m[4])];
  throw new Error(`not a color: ${value}`);
}

/** fg drawn over an opaque bg. */
export function composite(fg: Rgba, bg: Rgba): Rgba {
  const a = fg[3];
  return [0, 1, 2].map((i) => Math.round(fg[i] * a + bg[i] * (1 - a))).concat(1) as Rgba;
}

function luminance([r, g, b]: Rgba): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** WCAG 2.x contrast ratio of two opaque colors. */
export function contrastRatio(a: Rgba, b: Rgba): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
