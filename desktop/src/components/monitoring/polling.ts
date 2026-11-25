import { useEffect, useMemo, useRef } from "react";
import type { ProvidersTelemetrySnapshot } from "./telemetryContract";

/**
 * Adaptive polling of the monitoring readings (Fase 4D.4 r2, Bloco 5).
 *
 * - One reading at a time: the next one is scheduled only when the current one finishes.
 * - Nothing runs while the window is hidden (minimized or covered: document.visibilityState). When it is visible
 *   again, a reading that came due meanwhile runs at once.
 * - After a failure the wait doubles (interval · 2^n), up to maxBackoffMs; one success restores the interval.
 * - A reading may return its own wait in ms: the provider telemetry follows the TTL of its source, not a fixed tick.
 */

/** Monitoramento visible: CPU, RAM, system counters and process list. */
export const METRICS_DETAIL_INTERVAL_MS = 2_000;
/** Only the status bar is visible: the tree CPU and RAM, without the system counters and the process list. */
export const METRICS_SUMMARY_INTERVAL_MS = 5_000;
/** Longest wait between metrics readings after repeated failures. */
export const METRICS_MAX_BACKOFF_MS = 30_000;
export const DEFAULT_MAX_BACKOFF_MS = 60_000;

/** Provider telemetry without a local source: refreshed at the TTL of the snapshot. */
export const DEFAULT_PROVIDERS_TTL_MS = 300_000;
/** Base of the provider backoff (a failed request has no TTL to follow). */
export const PROVIDERS_FALLBACK_INTERVAL_MS = 30_000;
/** Lower bound of the provider refresh, whatever the source says. */
export const PROVIDERS_MIN_REFRESH_MS = 5_000;
/** Margin after the local reader's cache expires, so the next request gets a new reading. */
const PROVIDERS_CACHE_MARGIN_MS = 1_000;

export interface PollerSettings {
  enabled: boolean;
  intervalMs: number;
  /** Wait before the first reading after `enabled` turns true (0: at once). */
  initialDelayMs: number;
  maxBackoffMs: number;
}

/** Resolves true, or the wait before the next reading in ms, on success; false (or a rejection) is a failure. */
export type PollRead = () => Promise<boolean | number | void>;

const isHidden = (): boolean => typeof document !== "undefined" && document.visibilityState === "hidden";

const positiveMs = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;

export function backoffDelayMs(intervalMs: number, failures: number, maxBackoffMs: number): number {
  if (failures <= 0) return intervalMs;
  return Math.min(Math.max(intervalMs, maxBackoffMs), intervalMs * 2 ** Math.min(failures, 16));
}

/**
 * Wait until the next provider telemetry request. With local sources, just after the reader's cache expires (a
 * request before that returns the same reading); without them, the TTL of the snapshot. Never above that TTL.
 */
export function providersRefreshDelayMs(snapshot: ProvidersTelemetrySnapshot | null | undefined, nowMs: number): number {
  const ceiling = Math.max(PROVIDERS_MIN_REFRESH_MS, positiveMs(snapshot?.ttl_ms) ?? DEFAULT_PROVIDERS_TTL_MS);
  const sources = snapshot?.sources;
  const cacheTtl = sources?.local_sources_checked ? positiveMs(sources.local_cache_ttl_ms) : null;
  if (cacheTtl === null) return ceiling;
  const readAt = positiveMs(sources?.local_read_at_unix_ms);
  const untilExpiry = readAt === null ? cacheTtl : readAt + cacheTtl - nowMs;
  return Math.min(ceiling, Math.max(PROVIDERS_MIN_REFRESH_MS, untilExpiry + PROVIDERS_CACHE_MARGIN_MS));
}

export class Poller {
  private settings: PollerSettings = { enabled: false, intervalMs: 1_000, initialDelayMs: 0, maxBackoffMs: DEFAULT_MAX_BACKOFF_MS };
  private attached = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private inFlight = false;
  private pendingNow = false;
  private failures = 0;
  private sourceWaitMs: number | null = null;
  private lastEndedAt: number | null = null;
  /** When the next reading is due; null until the first one is scheduled. */
  private dueAt: number | null = null;

  constructor(
    private readonly read: PollRead,
    private readonly now: () => number = Date.now
  ) {}

  attach(): void {
    this.attached = true;
    this.schedule();
  }

  detach(): void {
    this.attached = false;
    this.clearTimer();
  }

  configure(settings: PollerSettings): void {
    const wasEnabled = this.settings.enabled;
    this.settings = settings;
    if (!settings.enabled) {
      this.clearTimer();
      return;
    }
    if (this.lastEndedAt === null) {
      if (!wasEnabled) this.dueAt = null;
    } else if (!wasEnabled && this.failures > 0) {
      // Shown again after a failure: retry at once instead of leaving the error up for the rest of the backoff.
      this.dueAt = this.now();
    } else {
      this.dueAt = this.lastEndedAt + this.waitMs();
    }
    this.schedule();
  }

  /** Reads at once, or right after the reading in flight. */
  runNow(): void {
    if (!this.attached || !this.settings.enabled) return;
    if (this.inFlight) {
      this.pendingNow = true;
      return;
    }
    this.dueAt = this.now();
    this.schedule();
  }

  visibilityChanged(): void {
    this.schedule();
  }

  private waitMs(): number {
    const { intervalMs, maxBackoffMs } = this.settings;
    if (this.failures > 0) return backoffDelayMs(intervalMs, this.failures, maxBackoffMs);
    return this.sourceWaitMs ?? intervalMs;
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private schedule(): void {
    this.clearTimer();
    if (!this.attached || !this.settings.enabled || this.inFlight || isHidden()) return;
    if (this.dueAt === null) this.dueAt = this.now() + Math.max(0, this.settings.initialDelayMs);
    const delay = Math.max(0, this.dueAt - this.now());
    if (delay === 0) {
      void this.run();
      return;
    }
    this.timer = setTimeout(() => {
      this.timer = null;
      this.schedule();
    }, delay);
  }

  private async run(): Promise<void> {
    this.inFlight = true;
    this.pendingNow = false;
    let ok = false;
    let wait: number | null = null;
    try {
      const result = await this.read();
      ok = result !== false;
      wait = typeof result === "number" && Number.isFinite(result) && result >= 0 ? result : null;
    } catch {
      ok = false;
    }
    this.inFlight = false;
    this.lastEndedAt = this.now();
    if (ok) {
      this.failures = 0;
      this.sourceWaitMs = wait;
    } else {
      this.failures += 1;
    }
    this.dueAt = this.pendingNow ? this.lastEndedAt : this.lastEndedAt + this.waitMs();
    this.pendingNow = false;
    this.schedule();
  }
}

export interface PollingOptions extends Partial<Pick<PollerSettings, "initialDelayMs" | "maxBackoffMs">> {
  enabled: boolean;
  intervalMs: number;
  read: PollRead;
}

export interface PollingControl {
  /** Reads at once (or right after the reading in flight) and keeps the cadence from there. */
  runNow: () => void;
}

export function usePolling({
  enabled,
  intervalMs,
  read,
  initialDelayMs = 0,
  maxBackoffMs = DEFAULT_MAX_BACKOFF_MS,
}: PollingOptions): PollingControl {
  const readRef = useRef(read);
  readRef.current = read;
  const pollerRef = useRef<Poller | null>(null);
  if (pollerRef.current === null) pollerRef.current = new Poller(() => readRef.current());
  const poller = pollerRef.current;

  useEffect(() => {
    poller.attach();
    const onVisibility = () => poller.visibilityChanged();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      poller.detach();
    };
  }, [poller]);

  useEffect(() => {
    poller.configure({ enabled, intervalMs, initialDelayMs, maxBackoffMs });
  }, [poller, enabled, intervalMs, initialDelayMs, maxBackoffMs]);

  return useMemo(() => ({ runNow: () => poller.runNow() }), [poller]);
}
