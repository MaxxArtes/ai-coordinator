// @vitest-environment node
/**
 * Rules of the visual layer (Etapa 5): colors only from tokens, text in rem with a 12 px floor, no zoom or scale,
 * focus by outline (visible in the Windows contrast themes), views hidden without changing their size, and every
 * class used by a component defined in the CSS.
 */
import { describe, expect, it } from "vitest";
import { declarations, listSrc, parseRules, readSrc, stripComments, tokensFor } from "./testing/cssSource";

const CSS_FILES = listSrc((rel) => rel.endsWith(".css"));
const PROD_TS = listSrc(
  (rel) =>
    /\.(ts|tsx)$/.test(rel) && !/\.test\.(ts|tsx)$/.test(rel) && !rel.startsWith("test/") && !rel.startsWith("styles/testing/"),
);
const PROD_TSX = PROD_TS.filter((rel) => rel.endsWith(".tsx"));
const css = (rel: string) => readSrc(rel);
const ALL_RULES = CSS_FILES.flatMap((file) => parseRules(css(file)).map((rule) => ({ file, ...rule })));
const COMPONENT_RULES = parseRules(css("styles/components.css"));
const BASE_RULES = parseRules(css("styles/base.css"));
const TOKENS = css("styles/tokens.css");

const COLOR_PROPS = /^(color|background|background-color|border|border-(top|right|bottom|left)(-color)?|border-color|outline|outline-color|box-shadow|fill|stroke|accent-color|caret-color|text-decoration-color|column-rule-color)$/;
const LITERAL_COLOR = /#[0-9a-f]{3,8}\b|\b(rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\(/i;
const NAMED_COLOR = /\b(black|white|red|green|blue|yellow|orange|purple|gray|grey|silver|navy|teal|maroon|lime|aqua|fuchsia|olive|canvas|canvastext|linktext|visitedtext|activetext|buttonface|buttontext|buttonborder|field|fieldtext|highlight|highlighttext|selecteditem|selecteditemtext|mark|marktext|graytext|accentcolor|accentcolortext)\b/i;

function find(rules: { selector: string; body: string; media: string | null }[], selector: string, media: RegExp | null = null) {
  return rules.filter((r) => r.selector === selector && (media === null ? r.media === null : media.test(r.media ?? "")));
}

function decl(rules: { body: string }[], prop: string): string[] {
  return rules.map((r) => declarations(r.body).get(prop)).filter((v): v is string => v !== undefined);
}

describe("CSS and component rules", () => {
  it("finds the sources", () => {
    expect(CSS_FILES.length).toBeGreaterThanOrEqual(4);
    expect(PROD_TS.length).toBeGreaterThanOrEqual(20);
    expect(COMPONENT_RULES.length).toBeGreaterThan(150);
    expect(PROD_TS).toContain("dev/VisualCheck.tsx");
  });

  it("no literal color in the CSS outside tokens.css", () => {
    const bad: string[] = [];
    for (const rule of ALL_RULES.filter((r) => r.file !== "styles/tokens.css")) {
      declarations(rule.body).forEach((value, prop) => {
        if (prop.startsWith("--")) {
          bad.push(`${rule.file}: ${rule.selector} defines token ${prop}`);
          return;
        }
        const withoutVars = value.replace(/var\([^)]*\)/g, "");
        if (LITERAL_COLOR.test(withoutVars)) bad.push(`${rule.file}: ${rule.selector} { ${prop}: ${value} }`);
        else if (COLOR_PROPS.test(prop) && NAMED_COLOR.test(withoutVars)) bad.push(`${rule.file}: ${rule.selector} { ${prop}: ${value} }`);
      });
    }
    expect(bad).toEqual([]);
  });

  it("no literal color in the production TS/TSX (the visual check page included)", () => {
    const bad: string[] = [];
    for (const file of PROD_TS) {
      stripComments(css(file))
        .split("\n")
        .forEach((line, i) => {
          if (/(?<![&\w])#[0-9a-f]{3,8}\b/i.test(line) || /\b(rgb|rgba|hsl|hsla)\(\s*\d/i.test(line)) bad.push(`${file}:${i + 1}: ${line.trim()}`);
          const attr = /\b(fill|stroke|color|background|backgroundColor)\s*[=:]\s*["'{]\s*["']?([a-z]+)/gi;
          let m: RegExpExecArray | null;
          while ((m = attr.exec(line))) {
            if (!/^(none|currentcolor|inherit|transparent)$/i.test(m[2]) && NAMED_COLOR.test(m[2])) bad.push(`${file}:${i + 1}: ${line.trim()}`);
          }
        });
    }
    expect(bad).toEqual([]);
  });

  it("no url( in the CSS and no imported image or font", () => {
    for (const file of CSS_FILES) expect(stripComments(css(file))).not.toMatch(/\burl\(/i);
    const asset = /\.(svg|png|jpe?g|gif|webp|avif|ico|woff2?|ttf|otf)["'`]/i;
    const bad = PROD_TS.filter((file) =>
      css(file)
        .split("\n")
        .some((line) => /\b(import|require|new URL)\b/.test(line) && asset.test(line)),
    );
    expect(bad).toEqual([]);
  });

  it("font sizes only through var(--fs-*), and every --fs-* is rem and at least 0.75rem (12 px)", () => {
    const bad: string[] = [];
    for (const rule of ALL_RULES) {
      declarations(rule.body).forEach((value, prop) => {
        if (prop === "font-size" && !/^var\(--fs-[a-z]+\)$|^inherit$/.test(value)) bad.push(`${rule.file}: ${rule.selector} { font-size: ${value} }`);
        if (prop === "font" && value !== "inherit") bad.push(`${rule.file}: ${rule.selector} { font: ${value} }`);
      });
    }
    expect(bad).toEqual([]);
    const sizes = [...tokensFor(TOKENS)].filter(([k]) => k.startsWith("--fs-"));
    expect(sizes.length).toBeGreaterThanOrEqual(5);
    for (const [name, value] of sizes) {
      const m = /^([\d.]+)rem$/.exec(value);
      expect(m, name).not.toBeNull();
      expect(Number(m![1]), name).toBeGreaterThanOrEqual(0.75);
    }
    // Inline styles never set a font size (the xterm fontSize option is not a style).
    for (const file of PROD_TSX) expect(css(file), file).not.toMatch(/style=\{\{[^}]*fontSize/);
  });

  it("no zoom and no scale anywhere in the shell (the Windows scale is applied by WebView2)", () => {
    for (const file of CSS_FILES) {
      const text = stripComments(css(file));
      expect(text, file).not.toMatch(/\bzoom\s*:/i);
      expect(text, file).not.toMatch(/\bscale\s*[:(]/i);
    }
    for (const file of PROD_TS) {
      const text = stripComments(css(file));
      expect(text, file).not.toMatch(/\bzoom\b/i);
      expect(text, file).not.toMatch(/\bscale\(/i);
    }
  });

  it("every var(--x) used in the CSS and in the components exists in tokens.css", () => {
    const defined = new Set(
      [...tokensFor(TOKENS), ...tokensFor(TOKENS, { low: true }), ...tokensFor(TOKENS, { forced: true })].map(([k]) => k),
    );
    const used = new Set<string>();
    for (const file of [...CSS_FILES, ...PROD_TS]) {
      for (const m of stripComments(css(file)).matchAll(/var\((--[\w-]+)/g)) used.add(m[1]);
    }
    expect(used.size).toBeGreaterThan(40);
    expect([...used].filter((name) => !defined.has(name))).toEqual([]);
  });

  it("focus is an outline (not a box-shadow) and survives the Windows contrast themes", () => {
    const top = find(BASE_RULES, ":focus-visible");
    expect(decl(top, "outline")).toEqual(["var(--focus-width) solid var(--c-focus)"]);
    expect(decl(top, "box-shadow")).toEqual([]);
    const forced = /forced-colors: active/;
    expect(decl(find(BASE_RULES, ":focus-visible", forced), "outline")[0]).toMatch(/solid/);
    expect(decl(find(COMPONENT_RULES, ".terminal-pane:focus-within", forced), "outline")[0]).toMatch(/solid/);
    expect(decl(find(COMPONENT_RULES, ".terminal-pane__mount", forced), "forced-color-adjust")).toEqual(["none"]);
    expect(decl(find(COMPONENT_RULES, ".terminal-pane__focus-region:focus-visible"), "outline-offset")[0]).toMatch(/-1/);
  });

  it("inactive views keep their box: hidden by visibility, all views in one grid cell", () => {
    const invisible = find(BASE_RULES, ".is-invisible");
    expect(decl(invisible, "visibility")).toEqual(["hidden !important"]);
    expect(decl(invisible, "display")).toEqual([]);
    expect(decl(find(COMPONENT_RULES, ".app__main"), "display")).toEqual(["grid"]);
    expect(decl(find(COMPONENT_RULES, ".app__main > *"), "grid-area")).toEqual(["1 / 1"]);
  });

  it("the terminal padding is inside .xterm, where FitAddon accounts for it", () => {
    expect(decl(find(COMPONENT_RULES, ".terminal-pane__mount"), "padding")).toEqual(["0"]);
    expect(decl(find(COMPONENT_RULES, ".terminal-pane__mount > .xterm"), "padding")).toEqual(["var(--term-pad)"]);
  });

  it("the reduced-motion block still stops animations", () => {
    const reduced = find(BASE_RULES, "*, *::before, *::after", /prefers-reduced-motion: reduce/);
    expect(decl(reduced, "animation-iteration-count")).toEqual(["1 !important"]);
    expect(decl(reduced, "transition-duration")).toEqual(["0.01ms !important"]);
    const low = BASE_RULES.filter((r) => r.selector.startsWith(':root[data-stimulation="low"] *'));
    expect(decl(low, "animation")).toEqual(["none !important"]);
  });

  it("no component reads the CPU percentage: CPU stays 'indisponível' until a reliable reading (4D.4)", () => {
    const readers = PROD_TS.flatMap((file) =>
      stripComments(css(file))
        .split("\n")
        .map((line, i) => ({ file, line: i + 1, text: line.trim() }))
        .filter((l) => l.text.includes("cpu_usage_percent")),
    );
    expect(readers.map((r) => `${r.file}: ${r.text}`)).toEqual(["components/shell/AppHeader.tsx: cpu_usage_percent?: number;"]);
  });

  it("every class a component uses is defined in the CSS", () => {
    const defined = new Set<string>();
    for (const rule of ALL_RULES) {
      for (const m of rule.selector.matchAll(/\.([a-zA-Z][\w-]*)/g)) defined.add(m[1]);
    }
    const used = new Set<string>();
    const classLike = /^[a-z][a-z0-9]*(-[a-z0-9]+)*(__[a-z0-9]+(-[a-z0-9]+)*)?(--[a-z0-9]+(-[a-z0-9]+)*)?$/;
    for (const file of PROD_TSX) {
      const text = stripComments(css(file));
      // Every string inside a className={…} or className="…" attribute.
      for (const m of text.matchAll(/className=(?:"([^"]*)"|\{([^}]*(?:\$\{[^}]*\}[^}]*)*)\})/g)) {
        // Comparisons (size === "sm") hold values, not classes.
        const expr = m[1] !== undefined ? `"${m[1]}"` : m[2].replace(/[!=]==\s*("[^"]*"|'[^']*')/g, "");
        for (const lit of expr.matchAll(/"([^"]*)"|'([^']*)'|`([^`]*)`/g)) {
          const content = (lit[1] ?? lit[2] ?? lit[3] ?? "").replace(/\$\{[^}]*\}/g, " ");
          for (const word of content.split(/\s+/)) if (classLike.test(word)) used.add(word);
        }
      }
      // Any BEM element or modifier written as a plain string (helpers that return a class name).
      for (const m of text.matchAll(/["'`]([a-z][a-z0-9-]*(?:__|--)[a-z0-9_-]+)["'`]/g)) if (classLike.test(m[1])) used.add(m[1]);
    }
    expect(used.size).toBeGreaterThan(40);
    expect([...used].filter((name) => !defined.has(name)).sort()).toEqual([]);
  });
});
