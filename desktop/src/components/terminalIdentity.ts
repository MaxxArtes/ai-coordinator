/**
 * Identity of the terminals of a tab (Marco 4D.3, Etapa 6, F10).
 *
 * - The identity is the session_id. The name uses a number that never repeats while the tab exists ("Terminal 05"),
 *   whatever the position on screen; the position ("2 de 4") is only said alongside it.
 * - The slot (0-3) is reported by the host, which never reuses a slot still held by a registered session.
 * - Only the executable name of the command is ever shown, never its arguments (they may carry secrets).
 */
import type { TerminalPaneData } from "./TerminalPane";

export const MAX_PANES = 4;

/** Lowest slot no pane holds; null when the tab is full. */
export function lowestFreeSlot(panes: readonly Pick<TerminalPaneData, "paneId">[]): number | null {
  for (let slot = 0; slot < MAX_PANES; slot++) {
    if (!panes.some((p) => p.paneId === slot)) return slot;
  }
  return null;
}

/** "5" → "05". */
export const formatTerminalNumber = (n: number): string => String(n).padStart(2, "0");

/** The executable name only: ["C:\\x\\cmd.exe", "/k", "SEGREDO"] → "cmd.exe". */
export function programName(command: unknown): string | undefined {
  const first = Array.isArray(command) ? command[0] : undefined;
  if (typeof first !== "string" || !first.trim()) return undefined;
  const name = first.trim().split(/[\\/]+/).filter(Boolean).pop();
  return name || undefined;
}

/** Pane data from the answer of terminal_create_session. */
export function paneFromSession(
  session: any,
  number: number,
  tabId: string,
  requestedSlot: number,
  kind: TerminalPaneData["kind"] = "human_terminal"
): TerminalPaneData {
  return {
    sessionId: session.session_id,
    tabId,
    paneId: Number.isInteger(session.pane_id) ? session.pane_id : requestedSlot,
    kind,
    number,
    program: programName(session.command),
    interpreterId: typeof session.interpreter_id === "string" ? session.interpreter_id : undefined,
    interpreterLabel: typeof session.interpreter_label === "string" ? session.interpreter_label : undefined,
    cwd: typeof session.cwd === "string" && session.cwd ? session.cwd : undefined,
    pid: Number.isInteger(session.pid) && session.pid > 0 ? session.pid : null,
    createdAt: typeof session.created_at === "string" ? session.created_at : undefined,
    startedAlive: session.is_alive === true,
  };
}
