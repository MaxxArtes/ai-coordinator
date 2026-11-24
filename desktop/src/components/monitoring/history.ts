import type { PaneState } from "../useTerminalStates";
import type { TerminalPaneData } from "../TerminalPane";
import { isPreviewMode, type ConnectionStatus } from "../shell/AppHeader";
import type {
  CategoryResourceMetrics,
  MetricProvenance,
  ProcessNodeMetrics,
  ProcessRole,
  ProcessTreeCategories,
  RuntimeMetrics,
} from "./telemetryContract";
import { formatSampleAge } from "./telemetryContract";

export const MAX_HISTORY_POINTS = 60;
const MIB = 1024 * 1024;
const GIB = 1024 * MIB;

export interface MonitoringThresholds {
  cpuWarnPercent: number;
  cpuCriticalPercent: number;
  ramWarnBytes: number;
  ramCriticalBytes: number;
}

export const DEFAULT_MONITORING_THRESHOLDS: MonitoringThresholds = {
  cpuWarnPercent: 70,
  cpuCriticalPercent: 85,
  ramWarnBytes: 1 * GIB,
  ramCriticalBytes: 2 * GIB,
};

export interface HistorySample {
  timestampMs: number;
  totalRamBytes: number | null;
  privateRamBytes: number | null;
  workingSetRamBytes: number | null;
  ramProvenance: MetricProvenance;
  privateRamProvenance: MetricProvenance;
  cpuPercent: number | null;
  cpuProvenance: MetricProvenance;
  systemCpuPercent: number | null;
  systemCpuProvenance: MetricProvenance;
  systemRamPercent: number | null;
  networkBytesPerSec: number | null;
  diskActivityPercent: number | null;
  processCount: number | null;
}

export interface MonitoringAlert {
  id: string;
  severity: "info" | "warn" | "critical";
  title: string;
  detail: string;
}

export interface CategoryDisplayRow {
  role: ProcessRole;
  label: string;
  metrics: CategoryResourceMetrics | null;
}

export interface SessionStatusSummary {
  total: number;
  active: number;
  ended: number;
  unknown: number;
  humanCount: number;
  agentCount: number;
}

export interface SeriesStats {
  current: number | null;
  average: number | null;
  peak: number | null;
}

const ROLE_ORDER: ReadonlyArray<{ role: ProcessRole; label: string }> = [
  { role: "host", label: "Aplicativo (Host)" },
  { role: "core", label: "Core Python" },
  { role: "webview", label: "WebView2" },
  { role: "terminal", label: "Terminais" },
  { role: "agent", label: "Agentes de IA" },
  { role: "child", label: "Filhos genéricos" },
];

export const ROLE_LABELS: Record<string, string> = Object.fromEntries(ROLE_ORDER.map((r) => [r.role, r.label]));

const AGENT_DISPLAY: Record<string, string> = {
  claude: "Claude",
  codex: "Codex",
  opencode: "OpenCode",
  agy: "Antigravity",
  gemini: "Gemini",
  aider: "Aider",
  "ai-coordinator": "AI Coordinator",
};

export function formatAgentLabel(kind?: string | null): string | null {
  if (!kind) return null;
  return AGENT_DISPLAY[kind.toLowerCase()] ?? kind;
}

const ONE_DECIMAL = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const INTEGER_FMT = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });

export function formatPercentPtBR(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return `${ONE_DECIMAL.format(value)}%`;
}

export function formatRatePtBR(bytesPerSec: number | null | undefined): string | null {
  if (typeof bytesPerSec !== "number" || !Number.isFinite(bytesPerSec) || bytesPerSec < 0) return null;
  if (bytesPerSec >= GIB) return `${ONE_DECIMAL.format(bytesPerSec / GIB)} GB/s`;
  if (bytesPerSec >= MIB) return `${ONE_DECIMAL.format(bytesPerSec / MIB)} MB/s`;
  if (bytesPerSec >= 1024) return `${ONE_DECIMAL.format(bytesPerSec / 1024)} KB/s`;
  return `${INTEGER_FMT.format(bytesPerSec)} B/s`;
}

export function formatCountPtBR(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return INTEGER_FMT.format(Math.round(value));
}

export function provenanceBadgeText(provenance?: MetricProvenance): string {
  if (provenance === "measured") return "medida";
  if (provenance === "estimated") return "estimada";
  return "indisponível";
}

export function provenanceToneClass(provenance?: MetricProvenance): string {
  if (provenance === "measured") return "badge badge--ok";
  if (provenance === "estimated") return "badge badge--warn";
  return "badge badge--unknown";
}

export function resolvedCpuReading(
  metrics: RuntimeMetrics | null,
  unavailable: boolean
): { value: number | null; provenance: MetricProvenance } {
  if (unavailable || !metrics) return { value: null, provenance: "unknown" };
  const prov = metrics.cpu_provenance;
  if (prov !== "measured" && prov !== "estimated") return { value: null, provenance: "unknown" };
  const raw = metrics.cpu_usage_percent;
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0) return { value: null, provenance: "unknown" };
  return { value: raw, provenance: prov };
}

export function resolvedSystemCpuReading(
  metrics: RuntimeMetrics | null,
  unavailable: boolean
): { value: number | null; provenance: MetricProvenance } {
  const sys = !unavailable ? metrics?.system : null;
  if (!sys) return { value: null, provenance: "unknown" };
  const prov = sys.cpu_provenance;
  if (prov !== "measured" && prov !== "estimated") return { value: null, provenance: "unknown" };
  const raw = sys.cpu_usage_percent;
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw < 0) return { value: null, provenance: "unknown" };
  return { value: raw, provenance: prov };
}

export function resolvedPrivateRamReading(
  metrics: RuntimeMetrics | null,
  unavailable: boolean
): { value: number | null; provenance: MetricProvenance } {
  if (unavailable || !metrics) return { value: null, provenance: "unknown" };
  const prov = metrics.private_ram_provenance;
  if (prov !== "measured" && prov !== "estimated") return { value: null, provenance: "unknown" };
  const raw = metrics.total_private_ram_bytes;
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw <= 0) return { value: null, provenance: "unknown" };
  return { value: raw, provenance: prov };
}

export function resolvedRamReading(
  metrics: RuntimeMetrics | null,
  unavailable: boolean
): { value: number | null; provenance: MetricProvenance } {
  if (unavailable || !metrics) return { value: null, provenance: "unknown" };
  const raw = metrics.total_tree_ram_bytes;
  if (typeof raw !== "number" || !Number.isFinite(raw) || raw <= 0) return { value: null, provenance: "unknown" };
  const prov: MetricProvenance =
    metrics.ram_provenance === "estimated"
      ? "estimated"
      : metrics.ram_provenance === "unknown"
        ? "unknown"
        : "measured";
  if (prov === "unknown") return { value: null, provenance: "unknown" };
  return { value: raw, provenance: prov };
}

export function resolvedSystemRamReading(
  metrics: RuntimeMetrics | null,
  unavailable: boolean
): { percent: number | null; usedBytes: number | null; totalBytes: number | null; provenance: MetricProvenance } {
  const sys = !unavailable ? metrics?.system : null;
  if (!sys || sys.ram_provenance === "unknown") {
    return { percent: null, usedBytes: null, totalBytes: null, provenance: "unknown" };
  }
  const prov = sys.ram_provenance === "estimated" ? "estimated" : sys.ram_provenance === "measured" ? "measured" : "unknown";
  if (prov === "unknown") return { percent: null, usedBytes: null, totalBytes: null, provenance: "unknown" };
  const tot = typeof sys.ram_total_bytes === "number" && sys.ram_total_bytes > 0 ? sys.ram_total_bytes : null;
  const used = typeof sys.ram_used_bytes === "number" && sys.ram_used_bytes >= 0 ? sys.ram_used_bytes : null;
  const pct = typeof sys.ram_usage_percent === "number" && Number.isFinite(sys.ram_usage_percent) ? sys.ram_usage_percent : null;
  if (pct === null || tot === null) return { percent: null, usedBytes: null, totalBytes: null, provenance: "unknown" };
  return { percent: pct, usedBytes: used, totalBytes: tot, provenance: prov };
}

export function appendHistorySample(
  history: readonly HistorySample[],
  metrics: RuntimeMetrics | null,
  failed: boolean,
  nowMs: number = Date.now(),
  maxPoints: number = MAX_HISTORY_POINTS
): HistorySample[] {
  const ram = resolvedRamReading(metrics, failed);
  const priv = resolvedPrivateRamReading(metrics, failed);
  const cpu = resolvedCpuReading(metrics, failed);
  const sysCpu = resolvedSystemCpuReading(metrics, failed);
  const sysRam = resolvedSystemRamReading(metrics, failed);
  const sys = !failed ? metrics?.system : null;

  const procCount =
    !failed && metrics && typeof metrics.process_count === "number" && metrics.process_count >= 0
      ? metrics.process_count
      : null;

  const netBps =
    sys && sys.network_provenance !== "unknown" && typeof sys.network_total_bytes_per_sec === "number"
      ? sys.network_total_bytes_per_sec
      : null;
  const diskAct =
    sys && sys.disk_activity_provenance !== "unknown" && typeof sys.disk_activity_percent === "number"
      ? sys.disk_activity_percent
      : null;

  const sample: HistorySample = {
    timestampMs: !failed && metrics?.sampled_at_unix_ms ? metrics.sampled_at_unix_ms : nowMs,
    totalRamBytes: ram.value,
    privateRamBytes: priv.value,
    workingSetRamBytes: ram.value,
    ramProvenance: ram.provenance,
    privateRamProvenance: priv.provenance,
    cpuPercent: cpu.value,
    cpuProvenance: cpu.provenance,
    systemCpuPercent: sysCpu.value,
    systemCpuProvenance: sysCpu.provenance,
    systemRamPercent: sysRam.percent,
    networkBytesPerSec: netBps,
    diskActivityPercent: diskAct,
    processCount: procCount,
  };

  const next = [...history, sample];
  return next.length > maxPoints ? next.slice(next.length - maxPoints) : next;
}

export function computeSeriesStats(points: ReadonlyArray<number | null>): SeriesStats {
  const valid = points.filter((v): v is number => typeof v === "number" && Number.isFinite(v) && v >= 0);
  if (valid.length === 0) return { current: null, average: null, peak: null };
  const sum = valid.reduce((acc, n) => acc + n, 0);
  return {
    current: valid[valid.length - 1],
    average: sum / valid.length,
    peak: Math.max(...valid),
  };
}

export function computeMonitoringAlerts(
  metrics: RuntimeMetrics | null,
  unavailable: boolean,
  isTauri: boolean,
  connectionStatus: ConnectionStatus,
  thresholds: MonitoringThresholds = DEFAULT_MONITORING_THRESHOLDS,
  nowMs: number = Date.now()
): MonitoringAlert[] {
  if (isPreviewMode(isTauri, connectionStatus)) {
    return [
      {
        id: "preview-unavailable",
        severity: "info",
        title: "Coletor nativo indisponível (modo preview)",
        detail: "Em navegador web sem o host Desktop, CPU, RAM e árvore de processos permanecem indisponíveis.",
      },
    ];
  }

  if (unavailable) {
    return [
      {
        id: "telemetry-unavailable",
        severity: "warn",
        title: "Leitura de telemetria indisponível",
        detail: "A última coleta do host falhou. Nenhuma estimativa antiga é exibida como medição atual.",
      },
    ];
  }

  if (!metrics) {
    return [
      {
        id: "telemetry-warming",
        severity: "info",
        title: "Aguardando primeira linha de base",
        detail: "A CPU requer duas amostras com intervalo mínimo para evitar falsas leituras de 0%.",
      },
    ];
  }

  const alerts: MonitoringAlert[] = [];

  if (metrics.data_origin === "test_fixture") {
    alerts.push({
      id: "origin-test-fixture",
      severity: "warn",
      title: "Origem identificada como fixture de teste",
      detail: "Estes valores são sintéticos/de teste e nunca devem ser tratados como telemetria de produção.",
    });
  }

  const age = formatSampleAge(metrics.sampled_at_unix_ms, metrics.ttl_ms ?? 10_000, nowMs);
  if (metrics.stale || age.expired) {
    alerts.push({
      id: "telemetry-stale",
      severity: "warn",
      title: "Leitura de telemetria expirada",
      detail: `A amostra tem idade superior ao limite de validade (${age.text}). Aguardando nova atualização do coletor.`,
    });
  }

  const cpu = resolvedCpuReading(metrics, false);
  const privRam = resolvedPrivateRamReading(metrics, false);
  const fallbackRam = resolvedRamReading(metrics, false);
  const ramForThreshold = privRam.value ?? fallbackRam.value;
  const ramThresholdLabel = privRam.value !== null ? "RAM privada" : "RAM da árvore";

  if (cpu.value !== null) {
    if (cpu.value >= thresholds.cpuCriticalPercent) {
      alerts.push({
        id: "cpu-critical",
        severity: "critical",
        title: `CPU da árvore elevada (${ONE_DECIMAL.format(cpu.value)}%)`,
        detail: `Acima do limite de referência crítico de ${thresholds.cpuCriticalPercent}%.`,
      });
    } else if (cpu.value >= thresholds.cpuWarnPercent) {
      alerts.push({
        id: "cpu-warn",
        severity: "warn",
        title: `CPU da árvore se aproximando do limite (${ONE_DECIMAL.format(cpu.value)}%)`,
        detail: `Acima do alerta discreto de referência de ${thresholds.cpuWarnPercent}%.`,
      });
    }
  } else {
    alerts.push({
      id: "cpu-unknown",
      severity: "info",
      title: "CPU ainda sem linha de base temporal",
      detail: "Aguardando intervalo mínimo de amostragem do sistema operacional.",
    });
  }

  if (ramForThreshold !== null) {
    if (ramForThreshold >= thresholds.ramCriticalBytes) {
      alerts.push({
        id: "ram-critical",
        severity: "critical",
        title: "Uso crítico de RAM da árvore",
        detail: `${ramThresholdLabel} atingiu ou superou o limite de referência de ${(thresholds.ramCriticalBytes / GIB).toFixed(1)} GB.`,
      });
    } else if (ramForThreshold >= thresholds.ramWarnBytes) {
      alerts.push({
        id: "ram-warn",
        severity: "warn",
        title: "Uso elevado de RAM da árvore",
        detail: `${ramThresholdLabel} atingiu ou superou o limite de referência de ${Math.round(thresholds.ramWarnBytes / MIB)} MB.`,
      });
    }
  }

  return alerts;
}

/** Plot area inside the chart's viewBox: room for the Y-axis labels on the left, and the line never touches the edges. */
export interface SparklinePadding {
  left: number;
  right: number;
  top: number;
  bottom: number;
}

/** A lone point (a single sample, or one between gaps) is drawn as a short dash: a bare "M" paints nothing. */
const SPARKLINE_DASH = 6;

function sparklinePadding(padding: number | Partial<SparklinePadding>): SparklinePadding {
  if (typeof padding === "number") return { left: padding, right: padding, top: padding, bottom: padding };
  return { left: padding.left ?? 4, right: padding.right ?? 4, top: padding.top ?? 4, bottom: padding.bottom ?? 4 };
}

/**
 * SVG path data of a series in a width × height viewBox, with 0 on the bottom edge of the plot area and maxValue on its
 * top edge. Values above maxValue stay on the top edge and gaps (null, negative, NaN) split the line, so every
 * coordinate stays inside the plot area.
 */
export function buildSparklineSegments(
  points: ReadonlyArray<number | null>,
  width: number,
  height: number,
  maxValue: number,
  padding: number | Partial<SparklinePadding> = 4
): string[] {
  const pad = sparklinePadding(padding);
  const usableW = width - pad.left - pad.right;
  const usableH = height - pad.top - pad.bottom;
  if (points.length === 0 || !(usableW > 0) || !(usableH > 0) || !(maxValue > 0) || !Number.isFinite(maxValue)) {
    return [];
  }
  const right = pad.left + usableW;
  const denom = Math.max(1, points.length - 1);

  const segments: string[] = [];
  let current: Array<[number, number]> = [];
  const flush = () => {
    if (current.length === 1) {
      const [x, y] = current[0];
      const x0 = Math.max(pad.left, Math.min(x, right - SPARKLINE_DASH));
      const x1 = Math.min(right, x0 + SPARKLINE_DASH);
      segments.push(`M${x0.toFixed(1)} ${y.toFixed(1)} L${x1.toFixed(1)} ${y.toFixed(1)}`);
    } else if (current.length > 1) {
      segments.push(current.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" "));
    }
    current = [];
  };

  points.forEach((value, idx) => {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
      flush();
      return;
    }
    const x = pad.left + (points.length === 1 ? usableW : (idx / denom) * usableW);
    const clamped = Math.min(1, Math.max(0, value / maxValue));
    const y = pad.top + usableH * (1 - clamped);
    current.push([x, y]);
  });

  flush();
  return segments;
}

export type CpuChartScale = "focus" | "global";

export const CPU_CHART_SCALE_STORAGE_KEY = "ai-coordinator.monitoring.cpu-scale.v1";
const FOCUS_HEADROOM = 1.25;
const FOCUS_MIN_MAX = 1;

/** Smallest 1, 2, 2,5 or 5 × 10^n that is at least value. */
export function niceCeil(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return FOCUS_MIN_MAX;
  const base = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = Number((step * base).toPrecision(12));
    if (candidate >= value) return candidate;
  }
  return Number((10 * base).toPrecision(12));
}

/**
 * Top of the CPU chart. Global: 0–100%. Focus: the peak of the AI Coordinator series in the window plus a margin,
 * rounded up to a readable step (never above 100% unless the series itself goes above it).
 */
export function cpuChartMax(points: ReadonlyArray<number | null>, mode: CpuChartScale): number {
  if (mode === "global") return 100;
  const valid = points.filter((v): v is number => typeof v === "number" && Number.isFinite(v) && v >= 0);
  const peak = valid.length > 0 ? Math.max(...valid) : 0;
  const top = niceCeil(Math.max(peak * FOCUS_HEADROOM, FOCUS_MIN_MAX));
  return peak <= 100 ? Math.min(top, 100) : top;
}

const AXIS_FMT = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });

export function formatAxisPercent(value: number): string {
  return `${AXIS_FMT.format(value)}%`;
}

export function readCpuChartScale(storage: Pick<Storage, "getItem"> | null | undefined): CpuChartScale {
  try {
    return storage?.getItem(CPU_CHART_SCALE_STORAGE_KEY) === "global" ? "global" : "focus";
  } catch {
    return "focus";
  }
}

export function writeCpuChartScale(storage: Pick<Storage, "setItem"> | null | undefined, mode: CpuChartScale): void {
  try {
    storage?.setItem(CPU_CHART_SCALE_STORAGE_KEY, mode);
  } catch {
    // Storage blocked or full: the choice lasts until the window closes.
  }
}

export function formatWindowDurationLabel(history: readonly HistorySample[]): string {
  if (history.length < 2) return "-2 min";
  const first = history[0]?.timestampMs;
  const last = history[history.length - 1]?.timestampMs;
  if (typeof first !== "number" || typeof last !== "number" || last <= first) {
    return `-${Math.max(2, history.length * 2)} s`;
  }
  const spanSec = Math.round((last - first) / 1000);
  if (spanSec < 60) return `-${spanSec} s`;
  const mins = spanSec / 60;
  return Number.isInteger(mins) ? `-${mins} min` : `-${ONE_DECIMAL.format(mins)} min`;
}

export function orderedCategories(categories?: ProcessTreeCategories): CategoryDisplayRow[] {
  return ROLE_ORDER.map(({ role, label }) => ({
    role,
    label,
    metrics: categories?.[role] ?? null,
  }));
}

export function sortTopProcesses(nodes?: readonly ProcessNodeMetrics[]): ProcessNodeMetrics[] {
  if (!Array.isArray(nodes) || nodes.length === 0) return [];
  return [...nodes].sort((a, b) => {
    const aCpu = typeof a.cpu_usage_percent === "number" && a.cpu_provenance !== "unknown" ? a.cpu_usage_percent : -1;
    const bCpu = typeof b.cpu_usage_percent === "number" && b.cpu_provenance !== "unknown" ? b.cpu_usage_percent : -1;
    if (bCpu !== aCpu) return bCpu - aCpu;
    const aPriv = typeof a.private_ram_bytes === "number" ? a.private_ram_bytes : -1;
    const bPriv = typeof b.private_ram_bytes === "number" ? b.private_ram_bytes : -1;
    if (bPriv !== aPriv) return bPriv - aPriv;
    const aRam = typeof a.ram_bytes === "number" ? a.ram_bytes : -1;
    const bRam = typeof b.ram_bytes === "number" ? b.ram_bytes : -1;
    if (bRam !== aRam) return bRam - aRam;
    return a.pid - b.pid;
  });
}

export function summarizeSessions(
  panes: readonly TerminalPaneData[],
  paneStates: Readonly<Record<string, PaneState>>
): SessionStatusSummary {
  let active = 0;
  let ended = 0;
  let unknown = 0;
  let humanCount = 0;
  let agentCount = 0;

  for (const pane of panes) {
    if (pane.kind === "governed_agent") agentCount += 1;
    else humanCount += 1;

    const st = paneStates[pane.sessionId]?.kind ?? "unknown";
    if (st === "alive" || st === "preview") active += 1;
    else if (st === "ended") ended += 1;
    else unknown += 1;
  }

  return {
    total: panes.length,
    active,
    ended,
    unknown,
    humanCount,
    agentCount,
  };
}
