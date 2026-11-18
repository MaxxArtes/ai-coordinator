/**
 * F6 regions and the reload guard (Etapa 5, decision 9), on markup shaped like the real shell: header, tab strip and
 * 1 to 4 panes in Conversas, or the temporary view in Monitoramento. The real App is covered in App.views.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import {
  currentRegion,
  cycleRegion,
  findRegions,
  handleGlobalKeydown,
  installGlobalShortcuts,
  SHORTCUT_LABELS,
  shellShortcut,
  terminalKeyFilter,
} from "./shortcuts";

type View = "conversas" | "monitoramento";

function buildShell(panes: number, view: View = "conversas", { strip = true } = {}) {
  const paneHtml = Array.from({ length: panes }, (_, i) => {
    const k = i + 1;
    return `
      <div class="terminal-pane" data-f6-region="pane" id="pane-${k}">
        <div class="terminal-pane__header">
          <div class="terminal-pane__focus-region" role="group" aria-label="Terminal ${k} de ${panes}" tabindex="-1" data-f6-entry id="entry-${k}"></div>
          <button type="button" id="close-${k}">Fechar terminal</button>
        </div>
        <div class="terminal-pane__mount">
          <div class="xterm"><div class="xterm-viewport" tabindex="-1"></div>
          <div class="xterm-screen"><textarea class="xterm-helper-textarea" id="input-${k}"></textarea></div></div>
        </div>
      </div>`;
  }).join("");
  const stripHtml = strip
    ? `<div class="tab-strip" data-f6-region="strip">
         <button type="button" data-f6-entry id="save">Salvar sessões</button>
         ${panes < 4 ? '<button type="button" id="add">Novo terminal</button>' : ""}
       </div>`
    : `<div class="tab-strip" data-f6-region="strip"></div>`;
  document.body.innerHTML = `
    <div id="root">
      <header data-f6-region="header">
        <div role="tablist">
          <button role="tab" id="tab-conversas" ${view === "conversas" ? 'data-f6-entry tabindex="0"' : 'tabindex="-1"'}>Conversas</button>
          <button role="tab" id="tab-monitoramento" ${view === "monitoramento" ? 'data-f6-entry tabindex="0"' : 'tabindex="-1"'}>Monitoramento</button>
        </div>
        <button type="button" id="gear">Configurações</button>
      </header>
      <main>
        <div id="view-conversas" ${view === "conversas" ? "" : "inert"}>${stripHtml}${paneHtml}</div>
        <div id="view-monitoramento" role="tabpanel" tabindex="0" data-f6-region="view" data-f6-entry ${view === "monitoramento" ? "" : "hidden"}></div>
      </main>
    </div>`;
  // Like xterm: the custom handler first; Tab is then consumed by the terminal (preventDefault + stopPropagation).
  document.querySelectorAll<HTMLTextAreaElement>("textarea").forEach((textarea) => {
    textarea.addEventListener("keydown", (event) => {
      if (!terminalKeyFilter(event)) return;
      if (event.key === "Tab") {
        event.preventDefault();
        event.stopPropagation();
      }
    });
  });
}

function press(key: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  (document.activeElement ?? document.body).dispatchEvent(event);
  return event;
}

const active = () => document.activeElement as HTMLElement;
const regionName = (el: Element | null) => currentRegion(el)?.getAttribute("data-f6-region") ?? "body";

let uninstall: () => void = () => undefined;
beforeEach(() => {
  uninstall = installGlobalShortcuts(window);
});
afterEach(() => {
  uninstall();
  document.body.innerHTML = "";
});

describe("F6 regions", () => {
  it.each([1, 2, 3, 4])("F6 goes header → strip → panes 1..%i → header, always onto an entry", (n) => {
    buildShell(n);
    document.body.focus();
    const ids: string[] = [];
    for (let i = 0; i < n + 3; i++) {
      press("F6");
      expect(active()).not.toBe(document.body);
      expect(active()).toHaveAttribute("data-f6-entry");
      ids.push(active().id);
    }
    const panes = Array.from({ length: n }, (_, i) => `entry-${i + 1}`);
    expect(ids).toEqual(["tab-conversas", "save", ...panes, "tab-conversas"]);
  });

  it.each([1, 2, 3, 4])("Shift+F6 goes the other way and wraps around (%i panes)", (n) => {
    buildShell(n);
    document.body.focus();
    const ids: string[] = [];
    for (let i = 0; i < n + 2; i++) {
      press("F6", { shiftKey: true });
      ids.push(active().id);
    }
    const panes = Array.from({ length: n }, (_, i) => `entry-${n - i}`);
    expect(ids).toEqual([...panes, "save", "tab-conversas"]);
  });

  it.each([2, 3, 4])("from inside terminal k, F6 goes to pane k+1 and Shift+F6 back to the region of pane k (%i panes)", (n) => {
    buildShell(n);
    for (let k = 1; k <= n; k++) {
      (document.getElementById(`input-${k}`) as HTMLTextAreaElement).focus();
      expect(regionName(active())).toBe("pane");
      const event = press("F6");
      expect(event.defaultPrevented).toBe(true);
      expect(active().id).toBe(k === n ? "tab-conversas" : `entry-${k + 1}`);
      press("F6", { shiftKey: true });
      expect(active().id).toBe(`entry-${k}`);
    }
  });

  it.each([1, 2, 3, 4])("F6 and Tab together reach every control and every terminal (%i panes)", async (n) => {
    const user = userEvent.setup({ document });
    buildShell(n);
    document.body.focus();
    const visited = new Set<string>();
    for (let r = 0; r < n + 2; r++) {
      press("F6");
      const region = currentRegion(active());
      visited.add(active().id);
      for (let t = 0; t < 6; t++) {
        if (active().tagName === "TEXTAREA") break;
        await user.tab();
        if (currentRegion(active()) !== region) break;
        visited.add(active().id);
      }
      // Back to the region entry before the next jump (as a person would press F6 again from anywhere inside).
      region && (region.querySelector<HTMLElement>("[data-f6-entry]") ?? region).focus();
    }
    const expected = Array.from(document.querySelectorAll<HTMLElement>("#root *"))
      .filter((el) => !el.closest("[inert]") && !el.closest("[hidden]") && (el.tabIndex >= 0 || el.hasAttribute("data-f6-entry")))
      .map((el) => el.id)
      .filter((id) => id !== "tab-monitoramento"); // a roving tab: reached with the arrow keys, not Tab
    expect(expected.filter((id) => !visited.has(id))).toEqual([]);
    for (let k = 1; k <= n; k++) {
      expect(visited.has(`input-${k}`)).toBe(true);
      expect(visited.has(`close-${k}`)).toBe(true);
    }
  });

  it("with a dialog open (#root inert) F6 neither moves nor cancels", () => {
    buildShell(2);
    document.getElementById("save")!.focus();
    document.getElementById("root")!.setAttribute("inert", "");
    const event = press("F6");
    expect(event.defaultPrevented).toBe(false);
    expect(active().id).toBe("save");
  });

  it("outside Conversas no pane is a target", () => {
    buildShell(4, "monitoramento");
    expect(findRegions().map((r) => r.getAttribute("data-f6-region"))).toEqual(["header", "view"]);
    document.body.focus();
    for (let i = 0; i < 5; i++) {
      press("F6");
      expect(active().closest(".terminal-pane")).toBeNull();
    }
    expect(active().id).toBe("tab-monitoramento");
  });

  it("a key held down does not repeat the jump", () => {
    buildShell(2);
    document.getElementById("save")!.focus();
    const event = press("F6", { repeat: true });
    expect(event.defaultPrevented).toBe(true);
    expect(active().id).toBe("save");
  });

  it.each([{ ctrlKey: true }, { altKey: true }, { metaKey: true }])("F6 with %j does not change regions", (init) => {
    buildShell(2);
    document.getElementById("save")!.focus();
    press("F6", init);
    expect(active().id).toBe("save");
  });

  it("an F6 already cancelled by the terminal filter still changes regions", () => {
    buildShell(2);
    document.getElementById("input-1")!.focus();
    const event = press("F6");
    expect(event.defaultPrevented).toBe(true);
    expect(active().id).toBe("entry-2");
  });

  it("from a control outside every region (a notice), F6 follows the screen order", () => {
    buildShell(2);
    const notice = document.createElement("button");
    notice.id = "dismiss-notice";
    document.querySelector("header")!.after(notice);
    notice.focus();
    press("F6");
    expect(active().id).toBe("save");
    notice.focus();
    press("F6", { shiftKey: true });
    expect(active().id).toBe("tab-conversas");
    document.querySelector("main")!.after(notice); // after every region: F6 wraps to the first one
    notice.focus();
    press("F6");
    expect(active().id).toBe("tab-conversas");
  });

  it("a region without an available entry is skipped", () => {
    buildShell(2, "conversas", { strip: false });
    document.body.focus();
    press("F6");
    press("F6");
    expect(active().id).toBe("entry-1");
  });

  it("cycleRegion reports when there is nowhere to go", () => {
    document.body.innerHTML = "<div id='root'><p>sem regiões</p></div>";
    expect(cycleRegion(1)).toBe(false);
  });

  it("the removal returned by installGlobalShortcuts stops the listener", () => {
    buildShell(1);
    uninstall();
    document.body.focus();
    const event = press("F6");
    expect(event.defaultPrevented).toBe(false);
    expect(active()).toBe(document.body);
    uninstall = installGlobalShortcuts(window);
  });
});

describe("reload guard", () => {
  it.each([
    ["F5", {}],
    ["F5", { ctrlKey: true }],
    ["r", { ctrlKey: true, code: "KeyR" }],
    ["R", { ctrlKey: true, shiftKey: true, code: "KeyR" }],
  ])("%s %j is cancelled, with or without a dialog open", (key, init) => {
    buildShell(1);
    document.getElementById("save")!.focus();
    expect(press(key, init).defaultPrevented).toBe(true);
    document.getElementById("root")!.setAttribute("inert", "");
    expect(press(key, init).defaultPrevented).toBe(true);
  });

  it("Ctrl+Shift+R typed inside the terminal is cancelled too (xterm lets it through)", () => {
    buildShell(1);
    document.getElementById("input-1")!.focus();
    expect(press("R", { ctrlKey: true, shiftKey: true, code: "KeyR" }).defaultPrevented).toBe(true);
  });

  it.each([
    ["r", {}],
    ["F5", { altKey: true }],
  ])("%s %j is left alone", (key, init) => {
    buildShell(1);
    document.getElementById("save")!.focus();
    expect(press(key, init).defaultPrevented).toBe(false);
  });
});

/** A keydown as the browser builds it; `code` is the physical key, `key` what the layout types. */
const keydown = (key: string, code: string, init: KeyboardEventInit = {}) =>
  new KeyboardEvent("keydown", { key, code, bubbles: true, cancelable: true, ...init });

describe("application shortcuts (Etapa 7)", () => {
  const INTERCEPTED: Array<[string, KeyboardEvent, string]> = [
    ["Ctrl+Shift+1", keydown("!", "Digit1", { ctrlKey: true, shiftKey: true }), "Digit1"],
    ["Ctrl+Shift+2", keydown("@", "Digit2", { ctrlKey: true, shiftKey: true }), "Digit2"],
    ["Ctrl+Shift+3", keydown("#", "Digit3", { ctrlKey: true, shiftKey: true }), "Digit3"],
    ["Ctrl+Shift+4", keydown("$", "Digit4", { ctrlKey: true, shiftKey: true }), "Digit4"],
    ["Ctrl+Shift+PageUp", keydown("PageUp", "PageUp", { ctrlKey: true, shiftKey: true }), "PageUp"],
    ["Ctrl+Shift+PageDown", keydown("PageDown", "PageDown", { ctrlKey: true, shiftKey: true }), "PageDown"],
    ["Ctrl+, (vírgula da linha de letras)", keydown(",", "Comma", { ctrlKey: true }), "settings"],
  ];
  const SHELL_KEYS: Array<[string, KeyboardEvent]> = [
    ["Tab", keydown("Tab", "Tab")],
    ["Shift+Tab", keydown("Tab", "Tab", { shiftKey: true })],
    ["Esc", keydown("Escape", "Escape")],
    ["Ctrl+C", keydown("c", "KeyC", { ctrlKey: true })],
    ["Ctrl+3", keydown("3", "Digit3", { ctrlKey: true })],
    ["Alt+1", keydown("1", "Digit1", { altKey: true })],
    ["AltGr+2 (Ctrl+Alt no Windows)", keydown("²", "Digit2", { ctrlKey: true, altKey: true })],
    ["AltGr+Shift+1", keydown("¹", "Digit1", { ctrlKey: true, altKey: true, shiftKey: true })],
    ["Ctrl+vírgula do teclado numérico (ABNT2)", keydown(",", "NumpadDecimal", { ctrlKey: true })],
    ["Ctrl+M no AZERTY (digita vírgula)", keydown(",", "KeyM", { ctrlKey: true })],
    ["Ctrl+Shift+5", keydown("%", "Digit5", { ctrlKey: true, shiftKey: true })],
    ["Meta+Ctrl+Shift+1", keydown("!", "Digit1", { ctrlKey: true, shiftKey: true, metaKey: true })],
    ["PageUp sozinho", keydown("PageUp", "PageUp")],
    ["Shift+PageUp (rolagem do terminal)", keydown("PageUp", "PageUp", { shiftKey: true })],
  ];

  it.each(INTERCEPTED)("%s is an application shortcut and never reaches the shell", (_, event, id) => {
    expect(shellShortcut(event)).toBe(id);
    expect(terminalKeyFilter(event)).toBe(false);
    expect(event.defaultPrevented).toBe(true);
  });

  it.each(SHELL_KEYS)("%s stays with the shell", (_, event) => {
    expect(shellShortcut(event)).toBeNull();
    expect(terminalKeyFilter(event)).toBe(true);
    expect(event.defaultPrevented).toBe(false);
  });

  it("the filter refuses the shortcut on keyup too, but cancels the browser action on keydown only", () => {
    const up = new KeyboardEvent("keyup", { key: ",", code: "Comma", ctrlKey: true, cancelable: true });
    expect(terminalKeyFilter(up)).toBe(false);
    expect(up.defaultPrevented).toBe(false);
  });

  function actions() {
    return { openSettings: vi.fn(), focusPane: vi.fn(), cycleView: vi.fn() };
  }

  it("each shortcut calls its action once and cancels the browser action", () => {
    buildShell(2);
    const a = actions();
    const fire = (event: KeyboardEvent) => {
      handleGlobalKeydown(event, "root", a);
      return event.defaultPrevented;
    };
    expect(fire(keydown(",", "Comma", { ctrlKey: true }))).toBe(true);
    expect(fire(keydown("@", "Digit2", { ctrlKey: true, shiftKey: true }))).toBe(true);
    expect(fire(keydown("PageUp", "PageUp", { ctrlKey: true, shiftKey: true }))).toBe(true);
    expect(fire(keydown("PageDown", "PageDown", { ctrlKey: true, shiftKey: true }))).toBe(true);
    expect(a.openSettings).toHaveBeenCalledTimes(1);
    expect(a.focusPane).toHaveBeenCalledWith(1);
    expect(a.cycleView.mock.calls).toEqual([[-1], [1]]);
  });

  it("with a dialog open (#root inert) no shortcut runs and the key is left alone", () => {
    buildShell(2);
    document.getElementById("root")!.setAttribute("inert", "");
    const a = actions();
    for (const event of INTERCEPTED.map(([, e]) => keydown(e.key, e.code, { ctrlKey: e.ctrlKey, shiftKey: e.shiftKey }))) {
      handleGlobalKeydown(event, "root", a);
      expect(event.defaultPrevented).toBe(false);
    }
    expect(a.openSettings).not.toHaveBeenCalled();
    expect(a.focusPane).not.toHaveBeenCalled();
    expect(a.cycleView).not.toHaveBeenCalled();
  });

  it("a shortcut held down runs once: the repeats are cancelled and ignored", () => {
    buildShell(2);
    const a = actions();
    const repeat = keydown(",", "Comma", { ctrlKey: true, repeat: true });
    handleGlobalKeydown(repeat, "root", a);
    expect(repeat.defaultPrevented).toBe(true);
    expect(a.openSettings).not.toHaveBeenCalled();
    handleGlobalKeydown(keydown("@", "Digit2", { ctrlKey: true, shiftKey: true, repeat: true }), "root", a);
    expect(a.focusPane).not.toHaveBeenCalled();
  });

  it("the keys that stay with the shell call no action", () => {
    buildShell(2);
    const a = actions();
    for (const [, event] of SHELL_KEYS) handleGlobalKeydown(keydown(event.key, event.code, event), "root", a);
    expect(a.openSettings).not.toHaveBeenCalled();
    expect(a.focusPane).not.toHaveBeenCalled();
    expect(a.cycleView).not.toHaveBeenCalled();
  });

  it("the keyboard map lists what exists and says that project tabs have no shortcut yet", () => {
    const keys = SHORTCUT_LABELS.map(([k]) => k);
    expect(keys).toEqual(expect.arrayContaining(["Ctrl+,", "Ctrl+Shift+1…4", "Ctrl+Shift+PageUp / PageDown", "F6 / Shift+F6", "F5 / Ctrl+R"]));
    const tabs = SHORTCUT_LABELS.find(([k]) => k === "Abas de projeto");
    expect(tabs?.[1]).toMatch(/Uma aba por janela/);
  });
});
