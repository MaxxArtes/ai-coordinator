export const TERMINAL_SETTINGS_EVENT = "desktop-terminal-settings";
export const validInt = (value: unknown, min: number, max: number, fallback: number): number =>
  typeof value === "number" && Number.isInteger(value) && value >= min && value <= max ? value : fallback;

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

/** True when the system asks for reduced motion (false where matchMedia does not exist). */
export function prefersReducedMotion(win: Window | null = typeof window === "undefined" ? null : window): boolean {
  try {
    return typeof win?.matchMedia === "function" && win.matchMedia(REDUCED_MOTION).matches;
  } catch {
    return false;
  }
}

/** The system preference as a MediaQueryList, so a pane can follow it live (null where it does not exist). */
export function reducedMotionQuery(win: Window = window): MediaQueryList | null {
  try {
    return typeof win.matchMedia === "function" ? win.matchMedia(REDUCED_MOTION) : null;
  } catch {
    return null;
  }
}

/**
 * Session-independent preferences. Reading/applying them never recreates xterm or a process.
 * The blinking cursor is an animation: low stimulation and the system's reduced motion keep it steady.
 */
export function terminalPreferences(root: HTMLElement = document.documentElement) {
  const steady = root.dataset.stimulation === "low" || prefersReducedMotion(root.ownerDocument.defaultView);
  return {
    fontSize: validInt(Number(root.dataset.terminalFontSize), 10, 24, 13),
    scrollback: validInt(Number(root.dataset.terminalScrollback), 100, 1000, 1000),
    cursorBlink: root.dataset.terminalCursorBlink !== "false" && !steady,
  };
}
