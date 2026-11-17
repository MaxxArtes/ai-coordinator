import { describe, expect, it } from "vitest";
import { band, indicatorsName, normalizeReading, readingDescription, readingValueText, terminalReadings, type IndicatorReading } from "./readings";

const measured = (over: Partial<IndicatorReading> = {}): IndicatorReading => ({
  kind: "context", provenance: "measured", percent: 18, source: "provider", asOf: "2026-11-17T12:00:00Z", ...over,
});

describe("indicator provenance", () => {
  it("the production source contains three unknown readings, with a reason and milestone, never a value", () => {
    const values = terminalReadings();
    expect(values.map((r) => r.kind)).toEqual(["context", "quota_5h", "quota_7d"]);
    for (const r of values) {
      expect(r).toMatchObject({ provenance: "unknown", plannedMilestone: "4D.4" });
      expect(r.reason).toMatch(/Desktop ainda não recebe/);
      expect(r).not.toHaveProperty("percent");
      expect(readingValueText(r)).toBe("—");
    }
    expect(indicatorsName("Terminal 05", values)).toBe("Indicadores do Terminal 05: contexto, cota de 5 horas e cota semanal indisponíveis");
  });
  it.each([undefined, NaN, Infinity, -1, 101])("rejects an invalid percentage: %s", (percent) => {
    const r = normalizeReading(measured({ percent }));
    expect(r.provenance).toBe("unknown");
    expect(r).not.toHaveProperty("percent");
    expect(r.reason).toBeTruthy();
    expect(r.plannedMilestone).toBe("4D.4");
  });
  it.each([{ source: undefined }, { asOf: undefined }, { source: " " }, { asOf: " " }])("no measurement without a source and time: %j", (over) => {
    expect(normalizeReading(measured(over)).provenance).toBe("unknown");
  });
  it.each([0, 100])("accepts the boundary %i as measured", (percent) => {
    expect(normalizeReading(measured({ percent }))).toMatchObject({ percent, provenance: "measured" });
  });
  it("unknown discards any supplied value while preserving its reason", () => {
    const r = normalizeReading(measured({ provenance: "unknown", reason: "sem leitura" }));
    expect(r).not.toHaveProperty("percent");
    expect(readingDescription(r)).toMatch(/indisponível.*Sem leitura.*4D\.4/);
  });
  it("estimated is explicitly approximate; a fixture stays marked even if invalid", () => {
    const r = normalizeReading(measured({ provenance: "estimated", origin: "test_fixture", percent: 18.4 }));
    expect(readingValueText(r)).toBe("≈18%");
    expect(readingDescription(r)).toMatch(/estimado.*fixture/);
    expect(indicatorsName("Terminal 01", [r])).toMatch(/≈18% estimado fixture/);
    const bad = normalizeReading({ ...r, percent: NaN });
    expect(bad.origin).toBe("test_fixture");
    expect(readingDescription(bad)).toContain("fixture");
    expect(indicatorsName("Terminal 01", [bad])).toContain("fixture");
  });
  it.each([[49, "low"], [50, "mid"], [80, "mid"], [81, "high"]] as const)("band %i is %s", (p, expected) => {
    expect(band(p)).toBe(expected);
  });
});
