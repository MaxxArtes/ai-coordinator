/**
 * Keyboard regions (Marco 4D.3, Etapa 5, decision 9).
 *
 * - F6 and Shift+F6 move through the regions in screen order, wrapping around, so that every control and every
 *   terminal can be reached with the keyboard alone, without depending on the close button.
 * - Region = [data-f6-region] ("header" | "strip" | "pane" | "view"); entry = the [data-f6-entry] element inside it
 *   (or the region itself). A region inside [inert] or [hidden], or without an available entry, is skipped.
 * - Inside a terminal, F6 and Shift+F6 are held back from the shell; Tab, Shift+Tab, Esc, Ctrl+C and every other key
 *   still go to the shell.
 * - The same listener cancels F5, Ctrl+R and Ctrl+Shift+R: reloading the page would lose every terminal.
 */
export const F6_REGION_ATTRIBUTE = "data-f6-region";
export const F6_ENTRY_ATTRIBUTE = "data-f6-entry";
export type F6Region = "header" | "strip" | "pane" | "view";

const REGION = `[${F6_REGION_ATTRIBUTE}]`;
const ENTRY = `[${F6_ENTRY_ATTRIBUTE}]`;

const isAvailable = (el: Element): boolean =>
  !el.closest("[inert]") && !el.closest("[hidden]") && !(el as HTMLButtonElement).disabled;

/** F6 alone or with Shift. Ctrl+F6, Alt+F6 and Meta+F6 stay with the terminal. */
export function isRegionKey(event: KeyboardEvent): boolean {
  return event.key === "F6" && !event.ctrlKey && !event.altKey && !event.metaKey;
}

/** F5 (with or without Ctrl/Shift), Ctrl+R and Ctrl+Shift+R. */
export function isReloadKey(event: KeyboardEvent): boolean {
  if (event.altKey || event.metaKey) return false;
  if (event.key === "F5") return true;
  return event.ctrlKey && (event.code === "KeyR" || event.key.toLowerCase() === "r");
}

/**
 * xterm custom key handler (attachCustomKeyEventHandler). Returns false for F6/Shift+F6 on keydown, keypress and
 * keyup: on keyup xterm would otherwise pull the focus back (xterm Terminal.ts:1086-1088). The browser action is
 * cancelled on keydown only, because xterm does not cancel an event its handler refused (Terminal.ts:985-986). The
 * propagation is not stopped: the event reaches the window listener, which moves to the next region.
 */
export function terminalKeyFilter(event: KeyboardEvent): boolean {
  if (!isRegionKey(event)) return true;
  if (event.type === "keydown") event.preventDefault();
  return false;
}

/** The entry of a region: the first available [data-f6-entry] that belongs to it (not to a nested region). */
export function findEntry(region: Element): HTMLElement | null {
  const candidates = [region, ...Array.from(region.querySelectorAll(ENTRY))];
  for (const el of candidates) {
    if (el instanceof HTMLElement && el.matches(ENTRY) && el.closest(REGION) === region && isAvailable(el)) return el;
  }
  return null;
}

/** Reachable regions, in document (screen) order. */
export function findRegions(root: ParentNode = document): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(REGION)).filter((r) => isAvailable(r) && findEntry(r) !== null);
}

/** Region of an element. From inside terminal k (its textarea), it is pane k. */
export function currentRegion(active: Element | null): HTMLElement | null {
  return active?.closest<HTMLElement>(REGION) ?? null;
}

/** Moves the focus to the entry of the next (1) or previous (-1) region. True when the focus moved. */
export function cycleRegion(direction: 1 | -1, root: Document | HTMLElement = document): boolean {
  const doc = root instanceof Document ? root : root.ownerDocument;
  const regions = findRegions(root);
  const count = regions.length;
  if (count === 0) return false;
  const active = doc.activeElement;
  const here = regions.indexOf(currentRegion(active) as HTMLElement);
  let start: number;
  if (here >= 0) start = here;
  else if (!active || active === doc.body) start = direction === 1 ? -1 : count; // page body: first or last region
  else {
    // A control outside every region (a notice between the header and the views): follow the screen order from it.
    const next = regions.findIndex((r) => !!(active.compareDocumentPosition(r) & Node.DOCUMENT_POSITION_FOLLOWING));
    const after = next < 0 ? count : next;
    start = direction === 1 ? after - 1 : after;
  }
  for (let step = 1; step <= count; step++) {
    const entry = findEntry(regions[(((start + direction * step) % count) + count) % count]);
    entry?.focus();
    if (entry && doc.activeElement === entry) return true; // an entry that refuses the focus: try the next one
  }
  return false;
}

export function handleGlobalKeydown(event: KeyboardEvent, appRootId = "root"): void {
  if (isReloadKey(event)) {
    event.preventDefault(); // also with a dialog open and while the key repeats
    return;
  }
  // defaultPrevented is not checked: inside a terminal the filter above has already cancelled the F6.
  if (!isRegionKey(event)) return;
  if (document.getElementById(appRootId)?.hasAttribute("inert")) return; // a dialog is open (Dialog.tsx)
  event.preventDefault();
  if (event.repeat) return; // a key held down does not repeat the jump
  cycleRegion(event.shiftKey ? -1 : 1);
}

/** One listener, in the bubbling phase of the window, installed once by the App. Returns its removal. */
export function installGlobalShortcuts(target: Window = window): () => void {
  const listener = (event: KeyboardEvent) => handleGlobalKeydown(event);
  target.addEventListener("keydown", listener);
  return () => target.removeEventListener("keydown", listener);
}
