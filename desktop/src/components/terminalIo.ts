import { useEffect, useRef } from "react";
import { toStructuredError } from "./settings/api";

/**
 * Terminal traffic between the interface and the host (Etapa 8, after L1/L2).
 *
 * Since L1 every IPC command is its own task on the host: two invokes sent microseconds apart can reach the Core in
 * either order (the host's FIFO serves them in the order they reach its lock, not the order the interface sent them).
 * Order and overlap are therefore decided here:
 *
 * - createSerialSender: one call in flight per session; what arrives meanwhile waits and goes right after it, merged
 *   (keystrokes concatenated, only the last size of a resize).
 * - useTerminalOutput: terminal_get_output polling that never overlaps, writes an answer only when it is still for the
 *   position the session is at, backs off after a timeout and stops when the Core closed the communication.
 */

type CallTauri = (cmd: string, args?: Record<string, any>) => Promise<any>;

export interface SerialSender<T> {
  /** Sends now when nothing is in flight for `key`; otherwise queues it (merged) to go right after the current one. */
  send(key: string, value: T): void;
}

export interface SerialSenderOptions<T> {
  deliver: (key: string, value: T) => Promise<unknown>;
  /**
   * The last queued value plus what just arrived, as one value; null keeps them as two calls, in order (e.g. a merged
   * write would pass the host's frame limit).
   */
  merge: (queued: T, next: T) => T | null;
  /**
   * After a failed delivery, what is queued behind it is dropped (false) or still sent (true). Keystrokes are dropped:
   * the rest of a command is never sent after a part that may not have arrived.
   */
  keepQueuedOnError: boolean;
  /** `dropped` says whether something queued behind the failed call was discarded. */
  onError?: (key: string, err: unknown, dropped: boolean) => void;
}

interface Lane<T> {
  queued: T[];
}

export function createSerialSender<T>({ deliver, merge, keepQueuedOnError, onError }: SerialSenderOptions<T>): SerialSender<T> {
  const lanes = new Map<string, Lane<T>>();

  const run = async (key: string, lane: Lane<T>, first: T) => {
    let value = first;
    try {
      for (;;) {
        try {
          await deliver(key, value);
        } catch (err) {
          const dropped = lane.queued.length > 0 && !keepQueuedOnError;
          if (dropped) lane.queued = [];
          try {
            onError?.(key, err, dropped);
          } catch {
            // A failing report never stops the lane.
          }
        }
        const next = lane.queued.shift();
        if (next === undefined) break;
        value = next;
      }
    } finally {
      // Nothing can be queued between the last check and here (no await): an idle lane is removed.
      if (lanes.get(key) === lane) lanes.delete(key);
    }
  };

  return {
    send(key, value) {
      const busy = lanes.get(key);
      if (busy) {
        const last = busy.queued.length - 1;
        const merged = last >= 0 ? merge(busy.queued[last], value) : null;
        if (merged !== null) busy.queued[last] = merged;
        else busy.queued.push(value);
        return;
      }
      const lane: Lane<T> = { queued: [] };
      lanes.set(key, lane);
      void run(key, lane, value);
    },
  };
}

/**
 * Largest terminal_write built by joining keystrokes (UTF-16 units). Even fully escaped in JSON (6 bytes per unit) it
 * stays well under the host's 1 MB frame; a single larger paste is sent as it came, as before.
 */
export const MAX_MERGED_WRITE = 64 * 1024;

/** Keystrokes typed while a write is in flight, joined while the result stays under MAX_MERGED_WRITE. */
export const mergeWrites = (queued: string, next: string): string | null =>
  queued.length + next.length <= MAX_MERGED_WRITE ? queued + next : null;

/** One output reading per this interval, never two at the same time. */
export const OUTPUT_POLL_MS = 80;
/** After a reading that timed out (the Core did not answer), the next one waits this long. */
export const OUTPUT_TIMEOUT_BACKOFF_MS = 5000;

export interface TerminalOutputSink {
  write(data: string): void;
  writeln(data: string): void;
}

export interface TerminalOutputOptions {
  enabled: boolean;
  callTauri: CallTauri;
  sessionIds: readonly string[];
  /** The mounted terminals by session id. */
  terms: { readonly current: ReadonlyMap<string, TerminalOutputSink> };
  /**
   * Last position read per session. A session is read only once it has an entry (its terminal is mounted); an answer
   * is written only when the entry still holds the position it was asked from.
   */
  lastSeq: { readonly current: Map<string, number> };
  /** The Core closed the communication: the polling stopped for good. Called once. */
  onDisconnected: (err: unknown) => void;
}

export function useTerminalOutput({ enabled, callTauri, sessionIds, terms, lastSeq, onDisconnected }: TerminalOutputOptions): void {
  const idsRef = useRef<readonly string[]>(sessionIds);
  idsRef.current = sessionIds;
  const onDisconnectedRef = useRef(onDisconnected);
  onDisconnectedRef.current = onDisconnected;
  // Shared by every run of the effect: a reading still in flight when the panes change keeps the next one waiting.
  const inFlightRef = useRef(false);
  const resumeAtRef = useRef(0);
  const stoppedRef = useRef(false);
  const hasPanes = sessionIds.length > 0;

  useEffect(() => {
    if (!enabled || !hasPanes || stoppedRef.current) return;
    let active = true;
    const round = async () => {
      if (inFlightRef.current || stoppedRef.current) return; // never overlapping
      if (Date.now() < resumeAtRef.current) return; // backing off after a timeout
      inFlightRef.current = true;
      try {
        for (const sessionId of idsRef.current) {
          if (!active) return;
          const sinceSeq = lastSeq.current.get(sessionId);
          if (sinceSeq === undefined) continue; // not mounted (or already closed): its output waits in the host
          let out: any;
          try {
            out = await callTauri("terminal_get_output", { sessionId, sinceSeq });
          } catch (err) {
            const reason = toStructuredError(err).reason;
            if (reason === "disconnected") {
              stoppedRef.current = true;
              clearInterval(timer);
              onDisconnectedRef.current(err);
              return;
            }
            if (reason === "timeout") {
              // The Core is not answering: the other sessions would wait the same; the operator's commands go first.
              resumeAtRef.current = Date.now() + OUTPUT_TIMEOUT_BACKOFF_MS;
              return;
            }
            continue; // e.g. a session being closed
          }
          // Asked from a position that is no longer the session's (closed, remounted or already read): discarded, so
          // the same output is never written twice.
          if (!out || lastSeq.current.get(sessionId) !== sinceSeq) continue;
          const term = terms.current.get(sessionId);
          if (term) {
            // Raw PTY chunk stream preserves exact CR, LF, ANSI escapes, prompts
            if (typeof out.raw === "string" && out.raw.length > 0) {
              term.write(out.raw);
            } else if (Array.isArray(out.lines) && out.lines.length > 0) {
              for (const line of out.lines) term.writeln(line);
            }
          }
          if (typeof out.next_seq === "number") lastSeq.current.set(sessionId, out.next_seq);
        }
      } finally {
        inFlightRef.current = false;
      }
    };
    const timer = setInterval(round, OUTPUT_POLL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [enabled, hasPanes, callTauri, terms, lastSeq]);
}
