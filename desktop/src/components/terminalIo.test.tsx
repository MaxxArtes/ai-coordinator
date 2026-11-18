/**
 * Terminal traffic after L1/L2 (Etapa 8): keystrokes and resizes leave in order, one call in flight per session; the
 * output polling never overlaps, never writes the same output twice, backs off after a timeout and stops when the Core
 * closed the communication.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSerialSender,
  MAX_MERGED_WRITE,
  mergeWrites,
  OUTPUT_POLL_MS,
  OUTPUT_TIMEOUT_BACKOFF_MS,
  useTerminalOutput,
} from "./terminalIo";

interface Deferred {
  resolve: (value?: unknown) => void;
  reject: (err: unknown) => void;
}

/** A host whose answers the test releases one by one; `calls` keeps what arrived, in arrival order. */
function slowHost() {
  const calls: Array<{ key: string; value: any; answer: Deferred }> = [];
  const deliver = vi.fn((key: string, value: any) => {
    return new Promise((resolve, reject) => calls.push({ key, value, answer: { resolve, reject } }));
  });
  return { calls, deliver };
}

const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};

const TIMEOUT = "O Core não respondeu em 30 s. A operação pode ter sido concluída: confira antes de repetir.";
const DISCONNECTED = {
  code: -32007,
  message: "O Core encerrou a comunicação. Reinicie o aplicativo.",
  data: { field: "ipc", reason: "disconnected" },
};

describe("createSerialSender (terminal_write / terminal_resize)", () => {
  it("keeps one write in flight per session; what is typed meanwhile goes right after it, in order and in one piece", async () => {
    const { calls, deliver } = slowHost();
    const writer = createSerialSender<string>({ deliver, merge: mergeWrites, keepQueuedOnError: false });
    writer.send("s1", "l");
    writer.send("s1", "s");
    writer.send("s1", "\r");
    expect(calls.map((c) => c.value)).toEqual(["l"]); // never two at once: the host cannot reorder them
    calls[0].answer.resolve();
    await flush();
    expect(calls.map((c) => c.value)).toEqual(["l", "s\r"]);
    calls[1].answer.resolve();
    await flush();
    writer.send("s1", "x"); // idle again: sent at once
    expect(calls.map((c) => c.value)).toEqual(["l", "s\r", "x"]);
  });

  it("another session is not held behind a slow one", () => {
    const { calls, deliver } = slowHost();
    const writer = createSerialSender<string>({ deliver, merge: mergeWrites, keepQueuedOnError: false });
    writer.send("s1", "a");
    writer.send("s2", "b");
    expect(calls.map((c) => [c.key, c.value])).toEqual([
      ["s1", "a"],
      ["s2", "b"],
    ]);
  });

  it("a failed write drops what was typed after it and says so; the next keystroke is sent normally", async () => {
    const { calls, deliver } = slowHost();
    const onError = vi.fn();
    const writer = createSerialSender<string>({ deliver, merge: mergeWrites, keepQueuedOnError: false, onError });
    writer.send("s1", "rm ");
    writer.send("s1", "-r x\r");
    calls[0].answer.reject(TIMEOUT);
    await flush();
    expect(onError).toHaveBeenCalledWith("s1", TIMEOUT, true);
    expect(calls).toHaveLength(1); // the rest of the command never goes after a part that may not have arrived
    writer.send("s1", "\x03");
    expect(calls.map((c) => c.value)).toEqual(["rm ", "\x03"]);
    calls[1].answer.reject(TIMEOUT);
    await flush();
    expect(onError).toHaveBeenLastCalledWith("s1", TIMEOUT, false);
  });

  it("joined keystrokes never pass the host's frame limit: past it they wait as a separate write, in order", async () => {
    const { calls, deliver } = slowHost();
    const onError = vi.fn();
    const writer = createSerialSender<string>({ deliver, merge: mergeWrites, keepQueuedOnError: false, onError });
    const half = "a".repeat(MAX_MERGED_WRITE / 2);
    writer.send("s1", "first");
    writer.send("s1", half);
    writer.send("s1", half);
    writer.send("s1", "b");
    writer.send("s1", "c");
    calls[0].answer.resolve();
    await flush();
    calls[1].answer.resolve();
    await flush();
    expect(calls.map((c) => c.value)).toEqual(["first", half + half, "bc"]);
    expect(Math.max(...calls.map((c) => c.value.length))).toBe(MAX_MERGED_WRITE);
    // A failure drops every write still waiting, not only the next one.
    writer.send("s1", "x");
    writer.send("s1", "y");
    calls[2].answer.reject(TIMEOUT);
    await flush();
    expect(onError).toHaveBeenCalledWith("s1", TIMEOUT, true);
    expect(calls).toHaveLength(3);
  });

  it("resize: only the last size goes after the one in flight, even when that one failed", async () => {
    const { calls, deliver } = slowHost();
    const resizer = createSerialSender<{ cols: number; rows: number }>({ deliver, merge: (_a, b) => b, keepQueuedOnError: true });
    resizer.send("s1", { cols: 80, rows: 24 });
    resizer.send("s1", { cols: 100, rows: 30 });
    resizer.send("s1", { cols: 120, rows: 40 });
    calls[0].answer.reject(new Error("sessão fechando"));
    await flush();
    expect(calls.map((c) => c.value)).toEqual([
      { cols: 80, rows: 24 },
      { cols: 120, rows: 40 },
    ]);
  });
});

describe("useTerminalOutput (terminal_get_output)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function setup(ids: string[] = ["a"], seq: Record<string, number> = { a: 0 }) {
    const written: Record<string, string[]> = {};
    const terms = {
      current: new Map(
        ids.map((id) => {
          written[id] = [];
          return [id, { write: (d: string) => written[id].push(d), writeln: (d: string) => written[id].push(`${d}\r\n`) }] as const;
        })
      ),
    };
    const lastSeq = { current: new Map(Object.entries(seq)) };
    const pending: Array<{ args: any } & Deferred> = [];
    const call = vi.fn(
      (_cmd: string, args?: any) => new Promise((resolve, reject) => pending.push({ args, resolve, reject }))
    );
    const onDisconnected = vi.fn();
    const props = { enabled: true, callTauri: call, sessionIds: ids, terms, lastSeq, onDisconnected };
    const hook = renderHook((p: typeof props) => useTerminalOutput(p), { initialProps: props });
    return { written, terms, lastSeq, pending, call, onDisconnected, hook, props };
  }

  const tick = async (ms = OUTPUT_POLL_MS) => {
    await act(async () => {
      vi.advanceTimersByTime(ms);
    });
  };
  const answer = async (d: Deferred, value: unknown) => {
    await act(async () => {
      d.resolve(value);
    });
  };

  it("never starts a reading while one is in flight, and writes each answer once", async () => {
    const { written, lastSeq, pending, call } = setup();
    await tick();
    expect(call).toHaveBeenCalledTimes(1);
    await tick(OUTPUT_POLL_MS * 20); // a slow call (closing another terminal, ConPTY): no second reading of seq 0
    expect(call).toHaveBeenCalledTimes(1);
    await answer(pending[0], { raw: "C:\\proj>", next_seq: 3 });
    expect(written.a).toEqual(["C:\\proj>"]);
    expect(lastSeq.current.get("a")).toBe(3);
    await tick();
    expect(call).toHaveBeenCalledTimes(2);
    expect(pending[1].args).toEqual({ sessionId: "a", sinceSeq: 3 });
  });

  it("a change of panes while a reading is in flight does not start a second one from the same position", async () => {
    const { pending, call, hook, props, lastSeq, written } = setup(["a", "b"], { a: 0 });
    await tick();
    expect(call).toHaveBeenCalledTimes(1);
    hook.rerender({ ...props, sessionIds: ["a"] }); // b closed: the effect runs again
    await tick(OUTPUT_POLL_MS * 5);
    expect(call).toHaveBeenCalledTimes(1);
    await answer(pending[0], { raw: "x", next_seq: 1 });
    expect(written.a).toEqual(["x"]);
    expect(lastSeq.current.get("a")).toBe(1);
  });

  it("discards an answer for a position that is no longer the session's (closed or remounted meanwhile)", async () => {
    const { written, lastSeq, pending } = setup();
    await tick();
    lastSeq.current.delete("a"); // the pane was closed while the reading was in flight
    await answer(pending[0], { raw: "saída", next_seq: 9 });
    expect(written.a).toEqual([]);
    expect(lastSeq.current.has("a")).toBe(false); // the closed session is not brought back
  });

  it("does not read a session whose terminal is not mounted yet: its output waits in the host", async () => {
    const { call } = setup(["a"], {});
    await tick(OUTPUT_POLL_MS * 3);
    expect(call).not.toHaveBeenCalled();
  });

  it("after a timeout it waits before reading again, so the operator's commands are not queued behind readings", async () => {
    const { pending, call } = setup();
    await tick();
    await act(async () => {
      pending[0].reject(TIMEOUT);
    });
    await tick(OUTPUT_TIMEOUT_BACKOFF_MS - OUTPUT_POLL_MS);
    expect(call).toHaveBeenCalledTimes(1);
    await tick(OUTPUT_POLL_MS * 2);
    expect(call).toHaveBeenCalledTimes(2);
  });

  it("stops for good when the Core closed the communication, and reports it once", async () => {
    const { pending, call, onDisconnected, hook, props } = setup();
    await tick();
    await act(async () => {
      pending[0].reject(DISCONNECTED);
    });
    expect(onDisconnected).toHaveBeenCalledTimes(1);
    hook.rerender({ ...props, sessionIds: ["a", "b"] });
    await tick(OUTPUT_TIMEOUT_BACKOFF_MS * 3);
    expect(call).toHaveBeenCalledTimes(1);
    expect(onDisconnected).toHaveBeenCalledTimes(1);
  });

  it("another error (a session being closed) skips that session and keeps polling", async () => {
    const { pending, call } = setup();
    await tick();
    await act(async () => {
      pending[0].reject("Session 'a' not found");
    });
    await tick();
    expect(call).toHaveBeenCalledTimes(2);
  });

  it("does nothing without the host and stops on unmount", async () => {
    const { call, hook, props } = setup();
    hook.rerender({ ...props, enabled: false });
    await tick(OUTPUT_POLL_MS * 5);
    expect(call).not.toHaveBeenCalled();
    hook.rerender(props);
    await tick();
    expect(call).toHaveBeenCalledTimes(1);
    hook.unmount();
    await tick(OUTPUT_POLL_MS * 50);
    expect(call).toHaveBeenCalledTimes(1);
  });
});
