/**
 * Adaptive polling of the monitoring (4D.4 r2, Bloco 5): interval by visible view, pause while the window is hidden and
 * an immediate reading when it comes back, never two readings at once, a growing wait after failures, and the provider
 * telemetry at the pace of its own TTL.
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  backoffDelayMs,
  DEFAULT_PROVIDERS_TTL_MS,
  METRICS_DETAIL_INTERVAL_MS,
  METRICS_SUMMARY_INTERVAL_MS,
  Poller,
  type PollerSettings,
  PROVIDERS_MIN_REFRESH_MS,
  providersRefreshDelayMs,
  usePolling,
} from "./polling";
import type { ProvidersTelemetrySnapshot } from "./telemetryContract";

let visibility: DocumentVisibilityState = "visible";
const setVisibility = (state: DocumentVisibilityState) => {
  visibility = state;
  document.dispatchEvent(new Event("visibilitychange"));
};

beforeEach(() => {
  vi.useFakeTimers({ now: 1_000_000 });
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
});

afterEach(() => {
  vi.useRealTimers();
});

const settings = (over: Partial<PollerSettings> = {}): PollerSettings => ({
  enabled: true,
  intervalMs: METRICS_DETAIL_INTERVAL_MS,
  initialDelayMs: 0,
  maxBackoffMs: 30_000,
  ...over,
});

/** A reading that stays in flight until the test resolves it. */
function deferredReads() {
  const pending: Array<(value: boolean | number) => void> = [];
  const read = vi.fn(
    () =>
      new Promise<boolean | number>((resolve) => {
        pending.push(resolve);
      })
  );
  return { read, resolveNext: (value: boolean | number = true) => pending.shift()?.(value), pending };
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("Poller", () => {
  it("reads at the interval of the visible view: 2 s with Monitoramento, 5 s with the status bar alone", async () => {
    const read = vi.fn(async () => true);
    const poller = new Poller(read);
    poller.attach();
    poller.configure(settings({ intervalMs: METRICS_SUMMARY_INTERVAL_MS, initialDelayMs: METRICS_DETAIL_INTERVAL_MS }));
    await advance(1_999);
    expect(read).toHaveBeenCalledTimes(0);
    await advance(1);
    expect(read).toHaveBeenCalledTimes(1);
    await advance(20_000);
    expect(read).toHaveBeenCalledTimes(5); // 2 s, then every 5 s: 7, 12, 17, 22

    poller.configure(settings({ intervalMs: METRICS_DETAIL_INTERVAL_MS }));
    await advance(20_000);
    expect(read).toHaveBeenCalledTimes(15); // every 2 s from the last reading
    poller.detach();
  });

  it("never starts a reading while the previous one is in flight", async () => {
    const { read, resolveNext } = deferredReads();
    const poller = new Poller(read);
    poller.attach();
    poller.configure(settings());
    await advance(0);
    expect(read).toHaveBeenCalledTimes(1);
    await advance(30_000);
    poller.runNow();
    expect(read).toHaveBeenCalledTimes(1);
    resolveNext();
    await advance(0);
    // runNow during the reading: the next one starts right after it, then the cadence continues.
    expect(read).toHaveBeenCalledTimes(2);
    resolveNext();
    await advance(METRICS_DETAIL_INTERVAL_MS - 1);
    expect(read).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(read).toHaveBeenCalledTimes(3);
    poller.detach();
  });

  it("pauses while the window is hidden and reads at once when it is visible again", async () => {
    const read = vi.fn(async () => true);
    const poller = new Poller(read);
    poller.attach();
    const onVisibility = () => poller.visibilityChanged();
    document.addEventListener("visibilitychange", onVisibility);
    poller.configure(settings());
    await advance(0);
    expect(read).toHaveBeenCalledTimes(1);

    setVisibility("hidden");
    await advance(60_000);
    expect(read).toHaveBeenCalledTimes(1);

    setVisibility("visible");
    await advance(0);
    expect(read).toHaveBeenCalledTimes(2);
    await advance(METRICS_DETAIL_INTERVAL_MS);
    expect(read).toHaveBeenCalledTimes(3);
    document.removeEventListener("visibilitychange", onVisibility);
    poller.detach();
  });

  it("doubles the wait after each failure up to the ceiling and restores the interval after a success", async () => {
    let fail = true;
    const read = vi.fn(async () => {
      if (fail) throw new Error("host indisponível");
      return true;
    });
    const poller = new Poller(read);
    poller.attach();
    poller.configure(settings({ maxBackoffMs: 16_000 }));
    await advance(0);
    const times: number[] = [Date.now()];
    for (const wait of [4_000, 8_000, 16_000, 16_000]) {
      await advance(wait - 1);
      expect(read).toHaveBeenCalledTimes(times.length);
      await advance(1);
      times.push(Date.now());
      expect(read).toHaveBeenCalledTimes(times.length);
    }
    fail = false;
    await advance(16_000);
    expect(read).toHaveBeenCalledTimes(6);
    await advance(METRICS_DETAIL_INTERVAL_MS);
    expect(read).toHaveBeenCalledTimes(7);
    expect(backoffDelayMs(2_000, 0, 30_000)).toBe(2_000);
    expect(backoffDelayMs(2_000, 3, 30_000)).toBe(16_000);
    expect(backoffDelayMs(2_000, 40, 30_000)).toBe(30_000);
    poller.detach();
  });

  it("follows the wait a reading returns (provider TTL) instead of the interval", async () => {
    const read = vi.fn(async () => 31_000);
    const poller = new Poller(read);
    poller.attach();
    poller.configure(settings({ intervalMs: 15_000 }));
    await advance(0);
    await advance(30_999);
    expect(read).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(read).toHaveBeenCalledTimes(2);
    poller.detach();
  });

  it("stops when disabled or detached and, enabled again, reads at once only if a reading came due", async () => {
    const read = vi.fn(async () => true);
    const poller = new Poller(read);
    poller.attach();
    poller.configure(settings({ intervalMs: 10_000 }));
    await advance(0);
    expect(read).toHaveBeenCalledTimes(1);
    poller.configure(settings({ intervalMs: 10_000, enabled: false }));
    await advance(4_000);
    poller.configure(settings({ intervalMs: 10_000 }));
    await advance(0);
    expect(read).toHaveBeenCalledTimes(1); // still 6 s to go
    await advance(6_000);
    expect(read).toHaveBeenCalledTimes(2);
    poller.configure(settings({ intervalMs: 10_000, enabled: false }));
    await advance(30_000);
    expect(read).toHaveBeenCalledTimes(2);
    poller.configure(settings({ intervalMs: 10_000 }));
    await advance(0);
    expect(read).toHaveBeenCalledTimes(3);
    poller.detach();
    await advance(60_000);
    expect(read).toHaveBeenCalledTimes(3);
    poller.runNow();
    await advance(0);
    expect(read).toHaveBeenCalledTimes(3);
  });
});

describe("usePolling", () => {
  it("changes the cadence with the view, reads at once on runNow and stops on unmount", async () => {
    const read = vi.fn(async () => true);
    const { result, rerender, unmount } = renderHook(
      ({ intervalMs, enabled }: { intervalMs: number; enabled: boolean }) =>
        usePolling({ enabled, intervalMs, initialDelayMs: METRICS_DETAIL_INTERVAL_MS, read }),
      { initialProps: { intervalMs: METRICS_SUMMARY_INTERVAL_MS, enabled: false } }
    );
    await advance(10_000);
    expect(read).not.toHaveBeenCalled();

    rerender({ intervalMs: METRICS_SUMMARY_INTERVAL_MS, enabled: true });
    await advance(METRICS_DETAIL_INTERVAL_MS);
    expect(read).toHaveBeenCalledTimes(1);
    await advance(METRICS_SUMMARY_INTERVAL_MS);
    expect(read).toHaveBeenCalledTimes(2);

    rerender({ intervalMs: METRICS_DETAIL_INTERVAL_MS, enabled: true });
    act(() => result.current.runNow());
    await advance(0);
    expect(read).toHaveBeenCalledTimes(3);
    await advance(METRICS_DETAIL_INTERVAL_MS);
    expect(read).toHaveBeenCalledTimes(4);

    setVisibility("hidden");
    await advance(20_000);
    expect(read).toHaveBeenCalledTimes(4);
    setVisibility("visible");
    await advance(0);
    expect(read).toHaveBeenCalledTimes(5);

    unmount();
    await advance(20_000);
    expect(read).toHaveBeenCalledTimes(5);
  });

  it("uses the latest read callback without restarting the cadence", async () => {
    const first = vi.fn(async () => true);
    const second = vi.fn(async () => true);
    const { rerender, unmount } = renderHook(({ read }) => usePolling({ enabled: true, intervalMs: 2_000, read }), {
      initialProps: { read: first },
    });
    await advance(0);
    expect(first).toHaveBeenCalledTimes(1);
    rerender({ read: second });
    await advance(2_000);
    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
    unmount();
  });
});

describe("providersRefreshDelayMs", () => {
  const NOW = 1_700_000_000_000;
  const snapshot = (sources: Partial<ProvidersTelemetrySnapshot["sources"]>, ttl_ms = 300_000): ProvidersTelemetrySnapshot => ({
    schema_version: 1,
    profile_name: "default",
    sampled_at_unix_ms: NOW,
    ttl_ms,
    stale: false,
    data_origin: "offline_cache",
    provenance: "measured",
    sources: { sqlite_available: true, quota_snapshot_available: true, vault_keys_checked: true, ...sources },
    providers: [],
  });

  it("refreshes just after the local reader's cache expires", () => {
    const fresh = snapshot({ local_sources_checked: true, local_read_at_unix_ms: NOW, local_cache_ttl_ms: 30_000 });
    expect(providersRefreshDelayMs(fresh, NOW)).toBe(31_000);
    const cachedEarlier = snapshot({ local_sources_checked: true, local_read_at_unix_ms: NOW - 20_000, local_cache_ttl_ms: 30_000 });
    expect(providersRefreshDelayMs(cachedEarlier, NOW)).toBe(11_000);
    const expired = snapshot({ local_sources_checked: true, local_read_at_unix_ms: NOW - 60_000, local_cache_ttl_ms: 30_000 });
    expect(providersRefreshDelayMs(expired, NOW)).toBe(PROVIDERS_MIN_REFRESH_MS);
  });

  it("without local sources follows the TTL of the snapshot, never the metrics tick", () => {
    expect(providersRefreshDelayMs(snapshot({}), NOW)).toBe(300_000);
    expect(providersRefreshDelayMs(snapshot({ local_sources_checked: false }, 120_000), NOW)).toBe(120_000);
    expect(providersRefreshDelayMs(null, NOW)).toBe(DEFAULT_PROVIDERS_TTL_MS);
    const longCache = snapshot({ local_sources_checked: true, local_read_at_unix_ms: NOW, local_cache_ttl_ms: 900_000 }, 300_000);
    expect(providersRefreshDelayMs(longCache, NOW)).toBe(300_000);
  });
});
