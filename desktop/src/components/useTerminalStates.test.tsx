/**
 * Live state of the terminals (Etapa 6, F9): every 2 s, never overlapping, only with the window visible, never in the
 * preview, stopped on unmount; a failure makes every state unknown.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  activeCountText,
  paneStateText,
  resolvePaneState,
  STATE_POLL_MS,
  useTerminalStates,
  type TerminalStatesSnapshot,
} from "./useTerminalStates";

const answer = {
  tabs: [
    {
      tab_id: "tab-main",
      panes: {
        "0": { session_id: "a", is_alive: true, exit_code: null },
        "2": { session_id: "b", is_alive: false, exit_code: 0 },
        "3": { session_id: "c", is_alive: false, exit_code: null },
      },
    },
    { tab_id: "outra", panes: { "0": { session_id: "x", is_alive: true } } },
  ],
};

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
  });
};

let visibility: DocumentVisibilityState = "visible";
beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("useTerminalStates", () => {
  it("matches the panes of the tab by session_id", async () => {
    const call = vi.fn(async () => answer);
    const { result } = renderHook(() => useTerminalStates(true, call, "tab-main", ["a", "b", "c"]));
    await flush();
    expect(call).toHaveBeenCalledWith("terminal_list_tabs");
    expect(result.current.reported).toEqual({
      a: { kind: "alive" },
      b: { kind: "ended", exitCode: 0 },
      c: { kind: "ended", exitCode: null },
    });
    expect(result.current.failed).toBe(false);
  });

  it("asks every 2 s, never while a query is still running, and stops on unmount", async () => {
    let release: (v: unknown) => void = () => undefined;
    const call = vi.fn(() => new Promise((resolve) => (release = resolve)));
    const { unmount } = renderHook(() => useTerminalStates(true, call, "tab-main", ["a"]));
    expect(call).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(STATE_POLL_MS * 3);
    });
    expect(call).toHaveBeenCalledTimes(1); // the first one has not answered yet
    await act(async () => {
      release(answer);
    });
    await act(async () => {
      vi.advanceTimersByTime(STATE_POLL_MS);
    });
    expect(call).toHaveBeenCalledTimes(2);
    unmount();
    await act(async () => {
      vi.advanceTimersByTime(STATE_POLL_MS * 5);
    });
    expect(call).toHaveBeenCalledTimes(2);
  });

  it("pauses while the window is hidden", async () => {
    const call = vi.fn(async () => answer);
    visibility = "hidden";
    renderHook(() => useTerminalStates(true, call, "tab-main", ["a"]));
    await act(async () => {
      vi.advanceTimersByTime(STATE_POLL_MS * 3);
    });
    expect(call).not.toHaveBeenCalled();
    visibility = "visible";
    await act(async () => {
      vi.advanceTimersByTime(STATE_POLL_MS);
    });
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("never asks in the preview", async () => {
    const call = vi.fn(async () => answer);
    renderHook(() => useTerminalStates(false, call, "tab-main", ["a"]));
    await act(async () => {
      vi.advanceTimersByTime(STATE_POLL_MS * 3);
    });
    expect(call).not.toHaveBeenCalled();
  });

  it("a failed query makes the states unknown without breaking the screen", async () => {
    const call = vi.fn(async () => {
      throw new Error("IPC parado");
    });
    const { result } = renderHook(() => useTerminalStates(true, call, "tab-main", ["a"]));
    await flush();
    expect(result.current.failed).toBe(true);
    expect(resolvePaneState(true, result.current, "a", true)).toEqual({ kind: "unknown" });
  });
});

describe("resolvePaneState and texts", () => {
  const snap = (over: Partial<TerminalStatesSnapshot>): TerminalStatesSnapshot => ({
    reported: null,
    askedFor: new Set(),
    failed: false,
    ...over,
  });

  it("before any answer, only what the creation reported; never alive without data", () => {
    expect(resolvePaneState(true, snap({}), "a", true)).toEqual({ kind: "alive" });
    expect(resolvePaneState(true, snap({}), "a", undefined)).toEqual({ kind: "unknown" });
    expect(resolvePaneState(false, snap({}), "a", true)).toEqual({ kind: "preview" });
  });

  it("a pane opened after the query started keeps its creation state; one the host no longer lists is unknown", () => {
    const s = snap({ reported: { a: { kind: "alive" } }, askedFor: new Set(["a", "gone"]) });
    expect(resolvePaneState(true, s, "new", true)).toEqual({ kind: "alive" });
    expect(resolvePaneState(true, s, "gone", true)).toEqual({ kind: "unknown" });
  });

  it("texts and the active count", () => {
    expect(paneStateText({ kind: "ended", exitCode: 3 })).toBe("encerrado, código 3");
    expect(activeCountText([{ kind: "alive" }, { kind: "alive" }, { kind: "ended", exitCode: 0 }])).toBe("(2 ativos)");
    expect(activeCountText([{ kind: "alive" }])).toBe("(1 ativo)");
    expect(activeCountText([{ kind: "alive" }, { kind: "unknown" }])).toBe("(ativos: desconhecido)");
    expect(activeCountText([{ kind: "preview" }])).toBeNull();
  });
});
