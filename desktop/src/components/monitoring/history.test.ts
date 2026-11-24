/**
 * Chart geometry and the CPU chart scale (4D.4 r2): every point inside the plot area of the viewBox, a visible line for
 * constant series, a lone sample, zeros and values above the top, and the adaptive "Foco AI Coordinator" scale.
 */
import { describe, expect, it } from "vitest";
import {
  buildSparklineSegments,
  CPU_CHART_SCALE_STORAGE_KEY,
  cpuChartMax,
  formatAxisPercent,
  niceCeil,
  readCpuChartScale,
  writeCpuChartScale,
} from "./history";

const W = 380;
const H = 104;
const BOX = { left: 64, right: 4, top: 12, bottom: 12 };

/** [x, y] of every command of every segment. */
function coords(segments: string[]): Array<[number, number]> {
  return segments.flatMap((d) => [...d.matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])] as [number, number]));
}

function expectInsidePlot(segments: string[]) {
  const points = coords(segments);
  expect(points.length).toBeGreaterThan(0);
  for (const [x, y] of points) {
    expect(x).toBeGreaterThanOrEqual(BOX.left);
    expect(x).toBeLessThanOrEqual(W - BOX.right);
    expect(y).toBeGreaterThanOrEqual(BOX.top);
    expect(y).toBeLessThanOrEqual(H - BOX.bottom);
  }
}

/** A segment paints something only when it has at least one drawing command after the move. */
function visible(d: string): boolean {
  const points = coords([d]);
  return points.length >= 2 && points.some(([x, y]) => x !== points[0][0] || y !== points[0][1]);
}

describe("buildSparklineSegments", () => {
  it("regression: the old horizontal padding on both axes (52 of a 104 high box) left no plot area at all", () => {
    expect(buildSparklineSegments([2, 3, 2.5], W, H, 100, 52)).toEqual([]);
    const segments = buildSparklineSegments([2, 3, 2.5], W, H, 100, BOX);
    expect(segments).toHaveLength(1);
    expectInsidePlot(segments);
  });

  it("maps 0 to the bottom edge and the maximum to the top edge, left to right across the plot area", () => {
    const [d] = buildSparklineSegments([0, 50, 100], W, H, 100, BOX);
    expect(coords([d])).toEqual([
      [64, 92],
      [220, 52],
      [376, 12],
    ]);
  });

  it("a constant series is a horizontal line across the plot area", () => {
    for (const value of [0, 2.19, 37.5]) {
      const segments = buildSparklineSegments([value, value, value, value], W, H, 5, BOX);
      expectInsidePlot(segments);
      expect(segments.every(visible)).toBe(true);
      const ys = new Set(coords(segments).map(([, y]) => y));
      expect(ys.size).toBe(1);
    }
  });

  it("a single sample is drawn as a short dash at the newest end, not as an invisible move", () => {
    const segments = buildSparklineSegments([3.47], W, H, 5, BOX);
    expect(segments).toHaveLength(1);
    expect(visible(segments[0])).toBe(true);
    expectInsidePlot(segments);
    expect(Math.max(...coords(segments).map(([x]) => x))).toBe(W - BOX.right);
  });

  it("zeros sit on the bottom edge and values above the maximum on the top edge, never outside the viewBox", () => {
    const segments = buildSparklineSegments([0, 0, 180, 250, 0], W, H, 100, BOX);
    expectInsidePlot(segments);
    const ys = coords(segments).map(([, y]) => y);
    expect(Math.max(...ys)).toBe(H - BOX.bottom);
    expect(Math.min(...ys)).toBe(BOX.top);
  });

  it("gaps split the line; a lone sample between gaps still shows", () => {
    const segments = buildSparklineSegments([1, 2, null, 3, null, Number.NaN, -1, 4, 4.5], W, H, 5, BOX);
    expect(segments).toHaveLength(3);
    expect(segments.every(visible)).toBe(true);
    expectInsidePlot(segments);
  });

  it("refuses what cannot be drawn instead of producing NaN coordinates", () => {
    expect(buildSparklineSegments([], W, H, 100, BOX)).toEqual([]);
    expect(buildSparklineSegments([1, 2], W, H, 0, BOX)).toEqual([]);
    expect(buildSparklineSegments([1, 2], W, H, Number.NaN, BOX)).toEqual([]);
    expect(buildSparklineSegments([1, 2], W, H, Number.POSITIVE_INFINITY, BOX)).toEqual([]);
    expect(buildSparklineSegments([null, null], W, H, 100, BOX)).toEqual([]);
    for (const d of buildSparklineSegments([1, 2, 3], W, H, 3, BOX)) expect(d).not.toMatch(/NaN|Infinity/);
  });
});

describe("CPU chart scale", () => {
  it("niceCeil rounds up to 1, 2, 2,5 or 5 × 10^n", () => {
    expect([0.3, 1, 1.2, 2.2, 2.6, 4.34, 7, 23, 110].map(niceCeil)).toEqual([0.5, 1, 2, 2.5, 5, 5, 10, 25, 200]);
    expect(niceCeil(0)).toBe(1);
    expect(niceCeil(Number.NaN)).toBe(1);
  });

  it("global is always 0–100%", () => {
    expect(cpuChartMax([2.19, 3.47], "global")).toBe(100);
    expect(cpuChartMax([], "global")).toBe(100);
  });

  it("focus follows the peak of the AI Coordinator series with a margin, so a 3% process fills the chart", () => {
    expect(cpuChartMax([2.19, 3.47, 1.1], "focus")).toBe(5);
    expect(cpuChartMax([18.4], "focus")).toBe(25);
    expect(cpuChartMax([0, 0, null], "focus")).toBe(1);
    expect(cpuChartMax([null], "focus")).toBe(1);
    expect(cpuChartMax([95], "focus")).toBe(100);
    expect(cpuChartMax([150], "focus")).toBe(200);
    const max = cpuChartMax([2.19, 3.47], "focus");
    const ys = coords(buildSparklineSegments([2.19, 3.47], W, H, max, BOX)).map(([, y]) => y);
    // The peak reaches past half of the plot height instead of hugging the bottom edge as on 0–100%.
    expect(Math.min(...ys)).toBeLessThan(BOX.top + (H - BOX.top - BOX.bottom) / 2);
  });

  it("axis labels in pt-BR without trailing zeros", () => {
    expect(formatAxisPercent(100)).toBe("100%");
    expect(formatAxisPercent(2.5)).toBe("2,5%");
    expect(formatAxisPercent(1.25)).toBe("1,25%");
    expect(formatAxisPercent(0.5)).toBe("0,5%");
  });

  it("the preference is remembered per window storage and anything odd falls back to focus", () => {
    const store = new Map<string, string>();
    const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    expect(readCpuChartScale(storage)).toBe("focus");
    writeCpuChartScale(storage, "global");
    expect(store.get(CPU_CHART_SCALE_STORAGE_KEY)).toBe("global");
    expect(readCpuChartScale(storage)).toBe("global");
    store.set(CPU_CHART_SCALE_STORAGE_KEY, "outra");
    expect(readCpuChartScale(storage)).toBe("focus");
    const blocked = {
      getItem: () => {
        throw new Error("bloqueado");
      },
      setItem: () => {
        throw new Error("bloqueado");
      },
    };
    expect(readCpuChartScale(blocked)).toBe("focus");
    expect(() => writeCpuChartScale(blocked, "global")).not.toThrow();
    expect(readCpuChartScale(null)).toBe("focus");
  });
});
