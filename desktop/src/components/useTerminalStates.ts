import { useEffect, useRef, useState } from "react";

/**
 * Live state of every terminal (Marco 4D.3, Etapa 6, F9).
 *
 * - terminal_list_tabs every 2 s, only while the window is visible, never overlapping, stopped on unmount and never in
 *   the browser preview (there is no host to ask).
 * - The panes are matched by session_id. A failed query makes every state "unknown": a terminal is never shown as
 *   alive without data.
 * - A pane opened after a query started is not judged by that query: it keeps the state its creation reported until
 *   the next answer.
 */
export type PaneState =
  | { kind: "alive" }
  | { kind: "ended"; exitCode: number | null }
  | { kind: "unknown" }
  | { kind: "preview" };

export const STATE_POLL_MS = 2000;

export interface TerminalStatesSnapshot {
  /** session_id → state reported by the last answer; null before the first answer. */
  reported: Record<string, PaneState> | null;
  /** session ids the UI had when the last answered query started. */
  askedFor: ReadonlySet<string>;
  failed: boolean;
}

const EMPTY: TerminalStatesSnapshot = { reported: null, askedFor: new Set(), failed: false };

type CallTauri = (cmd: string, args?: Record<string, any>) => Promise<any>;

function readTab(answer: any, tabId: string): Record<string, PaneState> {
  const tabs: any[] = Array.isArray(answer?.tabs) ? answer.tabs : [];
  const tab = tabs.find((t) => t?.tab_id === tabId);
  const out: Record<string, PaneState> = {};
  const panes = tab?.panes && typeof tab.panes === "object" ? Object.values(tab.panes) : [];
  for (const pane of panes as any[]) {
    if (!pane || typeof pane.session_id !== "string") continue;
    if (pane.is_alive === true) out[pane.session_id] = { kind: "alive" };
    else if (pane.is_alive === false) {
      out[pane.session_id] = { kind: "ended", exitCode: Number.isInteger(pane.exit_code) ? pane.exit_code : null };
    } else out[pane.session_id] = { kind: "unknown" };
  }
  return out;
}

export function useTerminalStates(
  isTauri: boolean,
  callTauri: CallTauri,
  tabId: string,
  sessionIds: readonly string[]
): TerminalStatesSnapshot {
  const [snapshot, setSnapshot] = useState<TerminalStatesSnapshot>(EMPTY);
  const idsRef = useRef<readonly string[]>(sessionIds);
  idsRef.current = sessionIds;
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!isTauri) {
      setSnapshot(EMPTY);
      return;
    }
    let mounted = true;
    const poll = async () => {
      if (inFlightRef.current) return; // never overlapping
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      inFlightRef.current = true;
      const askedFor = new Set(idsRef.current);
      try {
        const answer = await callTauri("terminal_list_tabs");
        if (mounted) setSnapshot({ reported: readTab(answer, tabId), askedFor, failed: false });
      } catch {
        if (mounted) setSnapshot({ reported: null, askedFor, failed: true });
      } finally {
        inFlightRef.current = false;
      }
    };
    poll();
    const timer = setInterval(poll, STATE_POLL_MS);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [isTauri, callTauri, tabId]);

  return snapshot;
}

/** The state shown for one pane. `startedAlive` is what terminal_create_session reported. */
export function resolvePaneState(
  isTauri: boolean,
  snapshot: TerminalStatesSnapshot,
  sessionId: string,
  startedAlive: boolean | undefined
): PaneState {
  if (!isTauri) return { kind: "preview" };
  if (snapshot.failed) return { kind: "unknown" };
  const reported = snapshot.reported?.[sessionId];
  if (reported) return reported;
  // Opened after the last query started (or before any answer): the creation is the only data there is.
  if (!snapshot.askedFor.has(sessionId) || snapshot.reported === null) {
    return startedAlive === true ? { kind: "alive" } : { kind: "unknown" };
  }
  return { kind: "unknown" }; // asked for, but the host no longer lists it
}

export function paneStateText(state: PaneState): string {
  switch (state.kind) {
    case "alive":
      return "ativo";
    case "ended":
      return state.exitCode === null ? "encerrado, código desconhecido" : `encerrado, código ${state.exitCode}`;
    case "preview":
      return "preview, sem processo";
    default:
      return "estado desconhecido";
  }
}

/** "(3 ativos)", "(1 ativo)", or that the count is unknown; null in the preview. */
export function activeCountText(states: PaneState[]): string | null {
  if (states.some((s) => s.kind === "preview")) return null;
  if (states.some((s) => s.kind === "unknown")) return "(ativos: desconhecido)";
  const alive = states.filter((s) => s.kind === "alive").length;
  return alive === 1 ? "(1 ativo)" : `(${alive} ativos)`;
}
