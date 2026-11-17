/**
 * Provenance contract of the pane indicators (Marco 4D.3, Etapa 6).
 *
 * - Three indicators per terminal: context (ctx), 5-hour quota (5h) and weekly quota (7d).
 * - Every reading says where it comes from. A value is shown only when it is a measurement with a source and a time,
 *   or an estimate marked as such; anything else is "unknown", with the reason and the milestone that will bring it.
 * - In 4D.3 the only source is terminalReadings(), which returns "unknown" for all three: the Desktop does not receive
 *   context nor quotas yet. The 4D.4 replaces the source without touching the components.
 */
export type IndicatorKind = "context" | "quota_5h" | "quota_7d";
export type Provenance = "measured" | "estimated" | "unknown";
export type ReadingOrigin = "live_network" | "test_fixture" | "offline_cache";

export interface IndicatorReading {
  kind: IndicatorKind;
  provenance: Provenance;
  origin?: ReadingOrigin;
  /** 0-100, only with a value. */
  percent?: number;
  used?: number;
  limit?: number;
  source?: string;
  /** Moment of the measurement (ISO 8601). */
  asOf?: string;
  /** Required without a value. */
  reason?: string;
  plannedMilestone?: string;
}

export const INDICATOR_KINDS: readonly IndicatorKind[] = ["context", "quota_5h", "quota_7d"];

export const INDICATOR_LABEL: Record<IndicatorKind, { short: string; long: string }> = {
  context: { short: "ctx", long: "contexto" },
  quota_5h: { short: "5h", long: "cota de 5 horas" },
  quota_7d: { short: "7d", long: "cota semanal" },
};

export const UNAVAILABLE_REASON = "terminal humano sem agente de IA; o Desktop ainda não recebe contexto nem cotas";
export const UNAVAILABLE_MILESTONE = "4D.4";

const unknown = (kind: IndicatorKind, reason = UNAVAILABLE_REASON, plannedMilestone = UNAVAILABLE_MILESTONE): IndicatorReading => ({
  kind,
  provenance: "unknown",
  reason,
  plannedMilestone,
});

/** Applies the rules: a value without proof becomes unknown; unknown always carries a reason and a milestone. */
export function normalizeReading(reading: IndicatorReading): IndicatorReading {
  const { kind } = reading;
  if (reading.provenance === "unknown") {
    return { ...unknown(kind, reading.reason || UNAVAILABLE_REASON, reading.plannedMilestone || UNAVAILABLE_MILESTONE), origin: reading.origin };
  }
  const p = reading.percent;
  if (typeof p !== "number" || !Number.isFinite(p) || p < 0 || p > 100) {
    return { ...unknown(kind, "valor fora do intervalo de 0 a 100", reading.plannedMilestone || UNAVAILABLE_MILESTONE), origin: reading.origin };
  }
  if (reading.provenance === "measured" && (!reading.source?.trim() || !reading.asOf?.trim())) {
    return { ...unknown(kind, "medida sem fonte ou sem horário", reading.plannedMilestone || UNAVAILABLE_MILESTONE), origin: reading.origin };
  }
  return { ...reading, percent: Math.round(p) };
}

/** The only source in 4D.3: nothing is measured yet. */
export function terminalReadings(): IndicatorReading[] {
  return INDICATOR_KINDS.map((kind) => unknown(kind));
}

export type Band = "low" | "mid" | "high";

/** Color bands, only ever shown together with the number: below 50, 50 to 80, above 80. */
export function band(percent: number): Band {
  return percent > 80 ? "high" : percent >= 50 ? "mid" : "low";
}

/** Short text next to the ring: "—", "18%" or "≈18%". */
export function readingValueText(reading: IndicatorReading): string {
  if (reading.provenance === "unknown" || reading.percent === undefined) return "—";
  return reading.provenance === "estimated" ? `≈${reading.percent}%` : `${reading.percent}%`;
}

/** One line of the explanation: state, reason and milestone. */
export function readingDescription(reading: IndicatorReading): string {
  const label = INDICATOR_LABEL[reading.kind].long;
  if (reading.provenance === "unknown") {
    return `${label}${reading.origin === "test_fixture" ? " (fixture)" : ""}: indisponível. ${capitalize(reading.reason ?? UNAVAILABLE_REASON)}. Previsto para a ${reading.plannedMilestone ?? UNAVAILABLE_MILESTONE}.`;
  }
  const kind = reading.provenance === "estimated" ? "estimado" : "medido";
  const fixture = reading.origin === "test_fixture" ? " (fixture)" : "";
  const source = reading.source ? `, fonte: ${reading.source}` : "";
  return `${label}: ${readingValueText(reading)}, ${kind}${source}${fixture}.`;
}

/** Accessible name of the indicators button of one terminal. */
export function indicatorsName(terminalName: string, readings: IndicatorReading[]): string {
  if (readings.every((r) => r.provenance === "unknown" && r.origin !== "test_fixture")) {
    return `Indicadores do ${terminalName}: contexto, cota de 5 horas e cota semanal indisponíveis`;
  }
  const parts = readings.map((r) =>
    r.provenance === "unknown"
      ? `${INDICATOR_LABEL[r.kind].long} indisponível${r.origin === "test_fixture" ? " fixture" : ""}`
      : `${INDICATOR_LABEL[r.kind].long} ${readingValueText(r)} ${r.provenance === "estimated" ? "estimado" : "medido"}${r.origin === "test_fixture" ? " fixture" : ""}`
  );
  return `Indicadores do ${terminalName}: ${parts.join(", ")}`;
}

const capitalize = (text: string): string => text.charAt(0).toUpperCase() + text.slice(1);
