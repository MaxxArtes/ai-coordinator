import type { PaneState } from "../useTerminalStates";
import type { TerminalPaneData } from "../TerminalPane";
import {
  isPreviewMode,
  type CategoryResourceMetrics,
  type ConnectionStatus,
  type MetricProvenance,
  type ProcessNodeMetrics,
  type ProcessRole,
  type ProcessTreeCategories,
  type RuntimeMetrics,
} from "../shell/AppHeader";

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
  ramProvenance: MetricProvenance;
  cpuPercent: number | null;
  cpuProvenance: MetricProvenance;
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

const ROLE_ORDER: ReadonlyArray<{ role: ProcessRole; label: string }> = [
  { role: "host", label: "Aplicativo (Host)" },
  { role: "core", label: "Core Python" },
  { role: "webview", label: "WebView2" },
  { role: "terminal", label: "Terminais" },
  { role: "child", label: "Filhos e agentes" },
];

export const ROLE_LABELS: Record<string, string> = Object.fromEntries(ROLE_ORDER.map((r) => [r.role, r.label]));

const ONE_DECIMAL = new Intl.NumberFormat("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

export function formatPercentPtBR(value: number | null | undefined): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return `${ONE_DECIMAL.format(value)}%`;
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

export function appendHistorySample(
  history: readonly HistorySample[],
  metrics: RuntimeMetrics | null,
  failed: boolean,
  nowMs: number = Date.now(),
  maxPoints: number = MAX_HISTORY_POINTS
): HistorySample[] {
  const ram = resolvedRamReading(metrics, failed);
  const cpu = resolvedCpuReading(metrics, failed);
  const procCount =
    !failed && metrics && typeof metrics.process_count === "number" && metrics.process_count >= 0
      ? metrics.process_count
      : null;

  const sample: HistorySample = {
    timestampMs: !failed && metrics?.sampled_at_unix_ms ? metrics.sampled_at_unix_ms : nowMs,
    totalRamBytes: ram.value,
    ramProvenance: ram.provenance,
    cpuPercent: cpu.value,
    cpuProvenance: cpu.provenance,
    processCount: procCount,
  };

  const next = [...history, sample];
  return next.length > maxPoints ? next.slice(next.length - maxPoints) : next;
}

export function computeMonitoringAlerts(
  metrics: RuntimeMetrics | null,
  unavailable: boolean,
  isTauri: boolean,
  connectionStatus: ConnectionStatus,
  thresholds: MonitoringThresholds = DEFAULT_MONITORING_THRESHOLDS
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
  const cpu = resolvedCpuReading(metrics, false);
  const ram = resolvedRamReading(metrics, false);

  if (cpu.value !== null) {
    if (cpu.value >= thresholds.cpuCriticalPercent) {
      alerts.push({
        id: "cpu-critical",
        severity: "critical",
        title: `CPU da árvore elevada (${ONE_DECIMAL.format(cpu.value)}%)`,
        detail: `Acima do limite crítico configurado de ${thresholds.cpuCriticalPercent}%.`,
      });
    } else if (cpu.value >= thresholds.cpuWarnPercent) {
      alerts.push({
        id: "cpu-warn",
        severity: "warn",
        title: `CPU da árvore se aproximando do limite (${ONE_DECIMAL.format(cpu.value)}%)`,
        detail: `Acima do alerta discreto de ${thresholds.cpuWarnPercent}%.`,
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

  if (ram.value !== null) {
    if (ram.value >= thresholds.ramCriticalBytes) {
      alerts.push({
        id: "ram-critical",
        severity: "critical",
        title: "Uso crítico de RAM da árvore",
        detail: `Consumo atual atingiu ou superou ${(thresholds.ramCriticalBytes / GIB).toFixed(1)} GB.`,
      });
    } else if (ram.value >= thresholds.ramWarnBytes) {
      alerts.push({
        id: "ram-warn",
        severity: "warn",
        title: "Uso elevado de RAM da árvore",
        detail: `Consumo atual atingiu ou superou ${Math.round(thresholds.ramWarnBytes / MIB)} MB.`,
      });
    }
  }

  return alerts;
}

export function buildSparklineSegments(
  points: ReadonlyArray<number | null>,
  width: number,
  height: number,
  maxValue: number,
  padding: number = 4
): string[] {
  if (points.length === 0 || width <= padding * 2 || height <= padding * 2 || maxValue <= 0) {
    return [];
  }
  const usableW = width - padding * 2;
  const usableH = height - padding * 2;
  const denom = Math.max(1, points.length - 1);

  const segments: string[] = [];
  let current: string[] = [];

  points.forEach((value, idx) => {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
      if (current.length > 0) {
        segments.push(current.join(" "));
        current = [];
      }
      return;
    }
    const x = padding + (points.length === 1 ? usableW : (idx / denom) * usableW);
    const clamped = Math.min(1, Math.max(0, value / maxValue));
    const y = padding + usableH * (1 - clamped);
    const cmd = current.length === 0 ? "M" : "L";
    current.push(`${cmd}${x.toFixed(1)} ${y.toFixed(1)}`);
  });

  if (current.length > 0) segments.push(current.join(" "));
  return segments;
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
