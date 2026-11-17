/**
 * F6 regions and the reload guard (Etapa 5, decision 9), on markup shaped like the real shell: header, tab strip and
 * 1 to 4 panes in Conversas, or the temporary view in Monitoramento. The real App is covered in App.views.test.tsx.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import userEvent from "@testing-library/user-event";
import { currentRegion, cycleRegion, findRegions, installGlobalShortcuts, terminalKeyFilter } from "./shortcuts";

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
