import React, { useMemo, useState } from "react";
import { Icon } from "../ui/Icon";
import { mainViewPanelId, mainViewTabId } from "../shell/MainNav";
import type { ConnectionStatus, RuntimeMetrics } from "../shell/AppHeader";
import { formatBytesPtBR } from "../shell/StatusBar";
import type { SettingsSnapshot } from "../settings/api";
import type { TerminalPaneData } from "../TerminalPane";
import type { PaneState } from "../useTerminalStates";
import { formatTerminalNumber } from "../terminalIdentity";
import {
  formatSampleAge,
  resolveOriginBadge,
  type MetricProvenance,
  type ProvidersTelemetrySnapshot,
} from "./telemetryContract";
import { ProvidersSection } from "./ProvidersSection";
import {
  buildSparklineSegments,
  computeMonitoringAlerts,
  computeSeriesStats,
  cpuChartMax,
  DEFAULT_MONITORING_THRESHOLDS,
  formatAgentLabel,
  formatAxisPercent,
  formatPercentPtBR,
  formatRatePtBR,
  formatWindowDurationLabel,
  orderedCategories,
  provenanceBadgeText,
  provenanceToneClass,
  readCpuChartScale,
  resolvedCpuReading,
  resolvedPrivateRamReading,
  resolvedRamReading,
  resolvedSystemCpuReading,
  resolvedSystemRamReading,
  ROLE_LABELS,
  sortTopProcesses,
  summarizeSessions,
  writeCpuChartScale,
  type CpuChartScale,
  type HistorySample,
  type MonitoringThresholds,
  type SparklinePadding,
} from "./history";

export interface MonitoringViewProps {
  active: boolean;
  isTauri: boolean;
  connectionStatus: ConnectionStatus;
  metrics: RuntimeMetrics | null;
  metricsUnavailable: boolean;
  history: readonly HistorySample[];
  panes: readonly TerminalPaneData[];
  paneStates: Readonly<Record<string, PaneState>>;
  thresholds?: MonitoringThresholds;
  settings?: SettingsSnapshot | null;
  providersTelemetry?: ProvidersTelemetrySnapshot | null;
  providersLoading?: boolean;
  providersError?: string | null;
  onRefreshProviders?: () => void;
}

const CHART_WIDTH = 380;
const CHART_HEIGHT = 104;
/** Left: room for the Y-axis labels ("512,0 MB"). Top and bottom: the line and its round caps stay inside the box. */
const CHART_PAD_X = 64;
const CHART_PAD_Y = 12;
const CHART_PAD_RIGHT = 4;
const CHART_PADDING: SparklinePadding = { left: CHART_PAD_X, right: CHART_PAD_RIGHT, top: CHART_PAD_Y, bottom: CHART_PAD_Y };
const CHART_RIGHT = CHART_WIDTH - CHART_PAD_RIGHT;

const browserStorage = (): Storage | null => {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
};

const sessionStateBadge = (state?: PaneState): { text: string; className: string } => {
  if (!state || state.kind === "unknown") return { text: "indisponível", className: "badge badge--unknown" };
  if (state.kind === "alive") return { text: "ativo", className: "badge badge--ok" };
  if (state.kind === "preview") return { text: "preview", className: "badge badge--unknown" };
  return {
    text: state.exitCode === null ? "encerrado" : `encerrado (código ${state.exitCode})`,
    className: "badge badge--err",
  };
};

const resolvedProvenance = (prov: string | undefined, valid: boolean): MetricProvenance => {
  if (!valid || !prov || prov === "unknown") return "unknown";
  return prov === "estimated" ? "estimated" : "measured";
};

export const MonitoringView: React.FC<MonitoringViewProps> = ({
  active,
  isTauri,
  connectionStatus,
  metrics,
  metricsUnavailable,
  history,
  panes,
  paneStates,
  thresholds = DEFAULT_MONITORING_THRESHOLDS,
  settings = null,
  providersTelemetry = null,
  providersLoading = false,
  providersError = null,
  onRefreshProviders,
}) => {
  const cpuReading = resolvedCpuReading(metrics, metricsUnavailable);
  const privRamReading = resolvedPrivateRamReading(metrics, metricsUnavailable);
  const wsRamReading = resolvedRamReading(metrics, metricsUnavailable);
  const sysCpuReading = resolvedSystemCpuReading(metrics, metricsUnavailable);
  const sysRamReading = resolvedSystemRamReading(metrics, metricsUnavailable);

  const cpuText = formatPercentPtBR(cpuReading.value) ?? "indisponível";
  const privRamText =
    privRamReading.value !== null ? formatBytesPtBR(privRamReading.value) ?? "indisponível" : "indisponível";
  const wsRamText = wsRamReading.value !== null ? formatBytesPtBR(wsRamReading.value) ?? "indisponível" : "indisponível";
  const hostWsText =
    !metricsUnavailable && metrics && metrics.host_ram_bytes > 0
      ? formatBytesPtBR(metrics.host_ram_bytes) ?? "indisponível"
      : "indisponível";
  const hostPrivText =
    !metricsUnavailable && metrics && typeof metrics.host_private_ram_bytes === "number" && metrics.host_private_ram_bytes > 0
      ? formatBytesPtBR(metrics.host_private_ram_bytes)
      : null;

  const sys = !metricsUnavailable ? metrics?.system ?? null : null;
  const sysCpuText = formatPercentPtBR(sysCpuReading.value) ?? "indisponível";
  const sysRamPctText = formatPercentPtBR(sysRamReading.percent) ?? "indisponível";
  const sysRamDetail =
    sysRamReading.usedBytes !== null && sysRamReading.totalBytes !== null
      ? `${formatBytesPtBR(sysRamReading.usedBytes) ?? "—"} / ${formatBytesPtBR(sysRamReading.totalBytes) ?? "—"}`
      : "Memória física global indisponível";

  const storageProv = resolvedProvenance(
    sys?.storage_provenance,
    typeof sys?.storage_usage_percent === "number" && Number.isFinite(sys.storage_usage_percent)
  );
  const storagePctText =
    storageProv !== "unknown" ? formatPercentPtBR(sys?.storage_usage_percent) ?? "indisponível" : "indisponível";
  const storageDetail =
    storageProv !== "unknown" && sys?.storage_total_bytes
      ? `${formatBytesPtBR(sys.storage_used_bytes ?? 0) ?? "0 KB"} / ${formatBytesPtBR(sys.storage_total_bytes) ?? "—"}${
          sys.storage_volumes?.length ? ` · ${sys.storage_volumes.length} volume(s)` : ""
        }`
      : "Capacidade dos volumes indisponível";

  const diskActProv = resolvedProvenance(
    sys?.disk_activity_provenance,
    typeof sys?.disk_activity_percent === "number" && Number.isFinite(sys.disk_activity_percent)
  );
  const diskActText =
    diskActProv !== "unknown" ? formatPercentPtBR(sys?.disk_activity_percent) ?? "indisponível" : "indisponível";
  const diskReadText =
    sys?.disk_io_provenance !== "unknown" ? formatRatePtBR(sys?.disk_read_bytes_per_sec) : null;
  const diskWriteText =
    sys?.disk_io_provenance !== "unknown" ? formatRatePtBR(sys?.disk_write_bytes_per_sec) : null;

  const netProv = resolvedProvenance(
    sys?.network_provenance,
    typeof sys?.network_total_bytes_per_sec === "number" && Number.isFinite(sys.network_total_bytes_per_sec)
  );
  const netTotalText =
    netProv !== "unknown" ? formatRatePtBR(sys?.network_total_bytes_per_sec) ?? "indisponível" : "indisponível";
  const netRxText = netProv !== "unknown" ? formatRatePtBR(sys?.network_rx_bytes_per_sec) : null;
  const netTxText = netProv !== "unknown" ? formatRatePtBR(sys?.network_tx_bytes_per_sec) : null;

  const gpuProv = resolvedProvenance(
    sys?.gpu_provenance,
    typeof sys?.gpu_usage_percent === "number" && Number.isFinite(sys.gpu_usage_percent)
  );
  const gpuText = gpuProv !== "unknown" ? formatPercentPtBR(sys?.gpu_usage_percent) ?? "indisponível" : "indisponível";

  const originBadge = resolveOriginBadge(metricsUnavailable ? undefined : metrics?.data_origin ?? (metrics ? "live_os" : undefined));
  const sampleAge = formatSampleAge(
    !metricsUnavailable ? metrics?.sampled_at_unix_ms : null,
    !metricsUnavailable ? metrics?.ttl_ms ?? 10_000 : null
  );

  const alerts = useMemo(
    () => computeMonitoringAlerts(metrics, metricsUnavailable, isTauri, connectionStatus, thresholds),
    [metrics, metricsUnavailable, isTauri, connectionStatus, thresholds]
  );

  const categories = useMemo(
    () => (metricsUnavailable || !metrics?.categories ? null : orderedCategories(metrics.categories)),
    [metrics, metricsUnavailable]
  );

  const topProcesses = useMemo(
    () => (metricsUnavailable || !metrics?.nodes ? [] : sortTopProcesses(metrics.nodes).slice(0, 12)),
    [metrics, metricsUnavailable]
  );

  const nodesByPid = useMemo(() => {
    const map = new Map<number, NonNullable<RuntimeMetrics["nodes"]>[number]>();
    if (!metricsUnavailable && metrics?.nodes) {
      for (const n of metrics.nodes) map.set(n.pid, n);
    }
    return map;
  }, [metrics, metricsUnavailable]);

  const agentNodes = useMemo(
    () =>
      !metricsUnavailable && metrics?.nodes
        ? sortTopProcesses(metrics.nodes.filter((n) => n.role === "agent" || Boolean(n.agent_kind)))
        : [],
    [metrics, metricsUnavailable]
  );

  const sessionSummary = useMemo(() => summarizeSessions(panes, paneStates), [panes, paneStates]);

  const cpuSeries = useMemo(() => history.map((s) => s.cpuPercent), [history]);
  const sysCpuSeries = useMemo(() => history.map((s) => s.systemCpuPercent), [history]);
  const usePrivateRamHistory = useMemo(
    () => history.some((s) => s.privateRamBytes !== null && s.privateRamProvenance !== "unknown"),
    [history]
  );
  const ramSeries = useMemo(
    () => history.map((s) => (usePrivateRamHistory ? s.privateRamBytes : s.totalRamBytes)),
    [history, usePrivateRamHistory]
  );

  const cpuStats = useMemo(() => computeSeriesStats(cpuSeries), [cpuSeries]);
  const sysCpuStats = useMemo(() => computeSeriesStats(sysCpuSeries), [sysCpuSeries]);
  const ramStats = useMemo(() => computeSeriesStats(ramSeries), [ramSeries]);

  const [cpuScale, setCpuScale] = useState<CpuChartScale>(() => readCpuChartScale(browserStorage()));
  const chooseCpuScale = (mode: CpuChartScale) => {
    setCpuScale(mode);
    writeCpuChartScale(browserStorage(), mode);
  };
  const cpuMax = useMemo(() => cpuChartMax(cpuSeries, cpuScale), [cpuSeries, cpuScale]);
  const focusScale = cpuScale === "focus";

  const cpuSegments = useMemo(
    () => buildSparklineSegments(cpuSeries, CHART_WIDTH, CHART_HEIGHT, cpuMax, CHART_PADDING),
    [cpuSeries, cpuMax]
  );
  const sysCpuSegments = useMemo(
    () => (focusScale ? [] : buildSparklineSegments(sysCpuSeries, CHART_WIDTH, CHART_HEIGHT, 100, CHART_PADDING)),
    [sysCpuSeries, focusScale]
  );

  const maxObservedRam = useMemo(() => {
    const valid = ramSeries.filter((v): v is number => typeof v === "number" && v > 0);
    if (valid.length === 0) return 0;
    return Math.max(...valid, thresholds.ramWarnBytes * 0.25);
  }, [ramSeries, thresholds.ramWarnBytes]);

  const ramSegments = useMemo(
    () => (maxObservedRam > 0 ? buildSparklineSegments(ramSeries, CHART_WIDTH, CHART_HEIGHT, maxObservedRam, CHART_PADDING) : []),
    [ramSeries, maxObservedRam]
  );

  const hasAppCpuHistory = cpuSeries.some((v) => v !== null);
  const hasCpuHistory = hasAppCpuHistory || sysCpuSeries.some((v) => v !== null);
  const hasRamHistory = ramSeries.some((v) => v !== null);
  const usableH = CHART_HEIGHT - CHART_PAD_Y * 2;
  const showWarnLine = thresholds.cpuWarnPercent > 0 && thresholds.cpuWarnPercent <= cpuMax;
  const warnLineY = (CHART_PAD_Y + usableH * (1 - Math.min(1, thresholds.cpuWarnPercent / cpuMax))).toFixed(1);
  const midY = (CHART_PAD_Y + usableH * 0.5).toFixed(1);
  const bottomY = (CHART_HEIGHT - CHART_PAD_Y).toFixed(1);
  const cpuScaleText = focusScale
    ? `Escala: 0–${formatAxisPercent(cpuMax)} (pico da janela com margem)`
    : "Escala: 0–100% (global)";
  const timeStartLabel = formatWindowDurationLabel(history);

  const coordBudget = settings?.categories?.coordinator?.values?.flight_budget as
    | { max_steps?: number; max_elapsed_seconds?: number }
    | undefined;

  return (
    <div
      role="tabpanel"
      id={mainViewPanelId("monitoramento")}
      aria-labelledby={mainViewTabId("monitoramento")}
      hidden={!active}
      tabIndex={0}
      className="app-view monitoring-view"
      data-f6-region="view"
      data-f6-entry=""
    >
      <div className="monitoring-view__inner">
        <header className="monitoring-view__header">
          <div>
            <h2 className="monitoring-view__title">Monitoramento</h2>
            <p className="monitoring-view__subtitle">
              Atualização a cada 2 s
              {!metricsUnavailable && metrics?.sample_interval_ms ? ` · janela: ${metrics.sample_interval_ms} ms` : ""}
              {!metricsUnavailable && metrics?.sampled_at_unix_ms ? ` · última leitura: ${sampleAge.text}` : ""}
              .
            </p>
          </div>
          <div className="monitoring-view__legend" aria-label="Legenda de origem e proveniência das métricas">
            {metrics && !metricsUnavailable && <span className={originBadge.className}>{originBadge.text}</span>}
            <span className="badge badge--ok">medida</span>
            <span className="badge badge--warn">estimada</span>
            <span className="badge badge--unknown">indisponível</span>
          </div>
        </header>

        {alerts.length > 0 && (
          <section className="monitoring-alerts" aria-label="Alertas de monitoramento" role="status">
            {alerts.map((alert) => (
              <div key={alert.id} className={`monitoring-alert monitoring-alert--${alert.severity}`}>
                <Icon name={alert.severity === "info" ? "info" : "alert"} size="sm" />
                <div className="monitoring-alert__body">
                  <strong className="monitoring-alert__title">{alert.title}</strong>
                  <span className="monitoring-alert__detail">{alert.detail}</span>
                </div>
              </div>
            ))}
          </section>
        )}

        <section className="monitoring-section" aria-label="Recursos do computador">
          <div className="monitoring-section__head">
            <h3 className="monitoring-section__title">Recursos do computador</h3>
            <span className="monitoring-panel__meta">
              Host geral (separado do AI Coordinator)
              {sys?.logical_cpu_count ? ` · ${sys.logical_cpu_count} núcleo(s) lógico(s)` : ""}
            </span>
          </div>
          <div className="monitoring-cards monitoring-cards--compact">
            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">CPU geral</span>
                <span className={provenanceToneClass(sysCpuReading.provenance)}>
                  {provenanceBadgeText(sysCpuReading.provenance)}
                </span>
              </div>
              <p className="monitoring-card__value">{sysCpuText}</p>
              <p className="monitoring-card__meta">Carga global do sistema operacional</p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">RAM geral</span>
                <span className={provenanceToneClass(sysRamReading.provenance)}>
                  {provenanceBadgeText(sysRamReading.provenance)}
                </span>
              </div>
              <p className="monitoring-card__value">{sysRamPctText}</p>
              <p className="monitoring-card__meta">{sysRamDetail}</p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">Armazenamento</span>
                <span className={provenanceToneClass(storageProv)}>{provenanceBadgeText(storageProv)}</span>
              </div>
              <p className="monitoring-card__value">{storagePctText}</p>
              <p className="monitoring-card__meta">{storageDetail}</p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">Atividade de disco</span>
                <span className={provenanceToneClass(diskActProv)}>{provenanceBadgeText(diskActProv)}</span>
              </div>
              <p className="monitoring-card__value">{diskActText}</p>
              <p className="monitoring-card__meta">
                {diskReadText && diskWriteText
                  ? `Leitura ${diskReadText} · Escrita ${diskWriteText}`
                  : "Taxa de leitura/escrita indisponível"}
              </p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">Rede</span>
                <span className={provenanceToneClass(netProv)}>{provenanceBadgeText(netProv)}</span>
              </div>
              <p className="monitoring-card__value">{netTotalText}</p>
              <p className="monitoring-card__meta">
                {netRxText && netTxText ? `↓ ${netRxText} · ↑ ${netTxText}` : "Tráfego agregado indisponível"}
              </p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">GPU geral</span>
                <span className={provenanceToneClass(gpuProv)}>{provenanceBadgeText(gpuProv)}</span>
              </div>
              <p className="monitoring-card__value">{gpuText}</p>
              <p className="monitoring-card__meta">Motor gráfico com maior uso</p>
            </article>
          </div>
        </section>

        <section className="monitoring-section" aria-label="Resumo do consumo atual">
          <div className="monitoring-section__head">
            <h3 className="monitoring-section__title">AI Coordinator e agentes</h3>
            <span className="monitoring-panel__meta">Árvore deduplicada do aplicativo</span>
          </div>
          <div className="monitoring-cards">
            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">CPU da árvore</span>
                <span className={provenanceToneClass(cpuReading.provenance)}>
                  {provenanceBadgeText(cpuReading.provenance)}
                </span>
              </div>
              <p className="monitoring-card__value">{cpuText}</p>
              <p className="monitoring-card__meta">
                Limite de referência: {thresholds.cpuWarnPercent}% (aviso) · {thresholds.cpuCriticalPercent}% (crítico)
              </p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">RAM privada (exclusiva)</span>
                <span className={provenanceToneClass(privRamReading.provenance)}>
                  {provenanceBadgeText(privRamReading.provenance)}
                </span>
              </div>
              <p className="monitoring-card__value">{privRamText}</p>
              <p className="monitoring-card__meta">
                Working Set total (inclui compartilhada): {wsRamText} · Host
                {!metricsUnavailable && metrics?.host_pid ? ` (PID ${metrics.host_pid})` : ""}:{" "}
                {hostPrivText ? `${hostPrivText} priv. / ${hostWsText} WS` : hostWsText}
              </p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">Processos e sessões</span>
                <span className={!metricsUnavailable && metrics ? "badge badge--ok" : "badge badge--unknown"}>
                  {!metricsUnavailable && metrics ? "medida" : "indisponível"}
                </span>
              </div>
              <p className="monitoring-card__value">
                {!metricsUnavailable && typeof metrics?.process_count === "number"
                  ? `${metrics.process_count} ${metrics.process_count === 1 ? "processo" : "processos"}`
                  : "indisponível"}
              </p>
              <p className="monitoring-card__meta">
                {sessionSummary.active} ativas · {sessionSummary.ended} encerradas · {sessionSummary.unknown} indisponíveis
              </p>
            </article>

            <article className="monitoring-card">
              <div className="monitoring-card__head">
                <span className="monitoring-card__label">Cotas e provedores</span>
                <span className={provenanceToneClass(providersTelemetry?.provenance)}>
                  {provenanceBadgeText(providersTelemetry?.provenance)}
                </span>
              </div>
              <p className="monitoring-card__value">
                {providersTelemetry
                  ? `${providersTelemetry.providers.length} provedores`
                  : "Não coletado"}
              </p>
              <p className="monitoring-card__meta">
                {providersTelemetry
                  ? `${providersTelemetry.providers.filter((p) => p.availability_status === "available").length} disponíveis · origem ${resolveOriginBadge(providersTelemetry.data_origin).text}`
                  : "Aguardando leitura de provedores — nenhum dado simulado exibido."}
              </p>
            </article>
          </div>

          <div className="monitoring-limits-note" aria-label="Limites de referência e contenção">
            <span>
              <strong>Referência (Monitoramento):</strong> CPU {thresholds.cpuWarnPercent}%/{thresholds.cpuCriticalPercent}% ·
              RAM {formatBytesPtBR(thresholds.ramWarnBytes)}/{formatBytesPtBR(thresholds.ramCriticalBytes)}
            </span>
            <span>
              <strong>Contenção ativa:</strong> Agentes governados limitados a 256 MB (Job Object) · Terminal humano sem teto de memória
              {coordBudget?.max_steps
                ? ` · Orçamento de missão salvo (${coordBudget.max_steps} passos, não aplicado até 4D.5)`
                : ""}
            </span>
          </div>
        </section>

        <section className="monitoring-charts" aria-label="Gráficos históricos recentes">
          <article className="monitoring-panel">
            <div className="monitoring-panel__head">
              <div>
                <h3 className="monitoring-panel__title">Histórico de CPU (%)</h3>
                <div className="monitoring-chart__legend">
                  {!focusScale && (
                    <span className="monitoring-legend-item">
                      <span className="monitoring-dot monitoring-dot--sys" aria-hidden="true" /> Computador
                    </span>
                  )}
                  <span className="monitoring-legend-item">
                    <span className="monitoring-dot monitoring-dot--app" aria-hidden="true" /> AI Coordinator
                  </span>
                  {focusScale && <span>Computador: só na escala global</span>}
                </div>
              </div>
              <div className="monitoring-panel__tools">
                <div className="monitoring-scale" role="group" aria-label="Escala do gráfico de CPU">
                  <button
                    type="button"
                    className="btn btn--sm monitoring-scale__option"
                    aria-pressed={focusScale}
                    onClick={() => chooseCpuScale("focus")}
                  >
                    Foco AI Coordinator
                  </button>
                  <button
                    type="button"
                    className="btn btn--sm monitoring-scale__option"
                    aria-pressed={!focusScale}
                    onClick={() => chooseCpuScale("global")}
                  >
                    Global 0–100%
                  </button>
                </div>
                <span className="monitoring-panel__meta">{history.length}/60 amostras</span>
              </div>
            </div>
            {(focusScale ? hasAppCpuHistory : hasCpuHistory) ? (
              <>
                <svg
                  className="monitoring-chart"
                  viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
                  role="img"
                  aria-label="Histórico recente de CPU"
                  data-scale={cpuScale}
                  data-max={cpuMax}
                >
                  <line x1={CHART_PAD_X} y1={CHART_PAD_Y} x2={CHART_RIGHT} y2={CHART_PAD_Y} className="monitoring-chart__grid" />
                  <line x1={CHART_PAD_X} y1={midY} x2={CHART_RIGHT} y2={midY} className="monitoring-chart__grid" />
                  <line x1={CHART_PAD_X} y1={bottomY} x2={CHART_RIGHT} y2={bottomY} className="monitoring-chart__grid" />
                  <text x="4" y={CHART_PAD_Y + 4} className="monitoring-chart__axis">
                    {formatAxisPercent(cpuMax)}
                  </text>
                  <text x="4" y={Number(midY) + 4} className="monitoring-chart__axis">
                    {formatAxisPercent(cpuMax / 2)}
                  </text>
                  <text x="4" y={Number(bottomY) + 4} className="monitoring-chart__axis">
                    0%
                  </text>
                  {showWarnLine && (
                    <line
                      x1={CHART_PAD_X}
                      y1={warnLineY}
                      x2={CHART_RIGHT}
                      y2={warnLineY}
                      className="monitoring-chart__threshold"
                    />
                  )}
                  {sysCpuSegments.map((d, idx) => (
                    <path key={`sys-${idx}`} d={d} className="monitoring-chart__line monitoring-chart__line--sys" />
                  ))}
                  {cpuSegments.map((d, idx) => (
                    <path key={`app-${idx}`} d={d} className="monitoring-chart__line monitoring-chart__line--cpu" />
                  ))}
                </svg>
                <div className="monitoring-chart__footer">
                  <span>{timeStartLabel}</span>
                  <span>
                    App: atual {formatPercentPtBR(cpuStats.current) ?? "—"} · média {formatPercentPtBR(cpuStats.average) ?? "—"} · pico{" "}
                    {formatPercentPtBR(cpuStats.peak) ?? "—"}
                    {sysCpuStats.current !== null ? ` | PC: ${formatPercentPtBR(sysCpuStats.current)}` : ""}
                  </span>
                  <span>agora</span>
                </div>
                <p className="monitoring-chart__scale">{cpuScaleText}</p>
              </>
            ) : (
              <p className="monitoring-empty">
                {hasCpuHistory
                  ? "Sem medições de CPU do AI Coordinator no histórico recente; a CPU do computador aparece na escala global."
                  : "Sem medições de CPU disponíveis no histórico recente."}
              </p>
            )}
          </article>

          <article className="monitoring-panel">
            <div className="monitoring-panel__head">
              <h3 className="monitoring-panel__title">
                Histórico recente de RAM ({usePrivateRamHistory ? "privada" : "Working Set"})
              </h3>
              <span className="monitoring-panel__meta">
                {maxObservedRam > 0 ? `Escala topo: ${formatBytesPtBR(maxObservedRam) ?? "indisponível"}` : "Sem amostras"}
              </span>
            </div>
            {hasRamHistory ? (
              <>
                <svg
                  className="monitoring-chart"
                  viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
                  role="img"
                  aria-label="Histórico recente de RAM"
                >
                  <line x1={CHART_PAD_X} y1={CHART_PAD_Y} x2={CHART_RIGHT} y2={CHART_PAD_Y} className="monitoring-chart__grid" />
                  <line x1={CHART_PAD_X} y1={midY} x2={CHART_RIGHT} y2={midY} className="monitoring-chart__grid" />
                  <line x1={CHART_PAD_X} y1={bottomY} x2={CHART_RIGHT} y2={bottomY} className="monitoring-chart__grid" />
                  <text x="4" y={CHART_PAD_Y + 4} className="monitoring-chart__axis">
                    {formatBytesPtBR(maxObservedRam) ?? "—"}
                  </text>
                  <text x="4" y={Number(midY) + 4} className="monitoring-chart__axis">
                    {formatBytesPtBR(maxObservedRam / 2) ?? "—"}
                  </text>
                  <text x="4" y={Number(bottomY) + 4} className="monitoring-chart__axis">
                    0
                  </text>
                  {ramSegments.map((d, idx) => (
                    <path key={idx} d={d} className="monitoring-chart__line monitoring-chart__line--ram" />
                  ))}
                </svg>
                <div className="monitoring-chart__footer">
                  <span>{timeStartLabel}</span>
                  <span>
                    Atual: {ramStats.current ? formatBytesPtBR(ramStats.current) : "—"} · Média:{" "}
                    {ramStats.average ? formatBytesPtBR(ramStats.average) : "—"} · Pico:{" "}
                    {ramStats.peak ? formatBytesPtBR(ramStats.peak) : "—"}
                  </span>
                  <span>agora</span>
                </div>
              </>
            ) : (
              <p className="monitoring-empty">Sem medições de RAM disponíveis no histórico recente.</p>
            )}
          </article>
        </section>

        <section className="monitoring-panel" aria-label="Detalhamento por categoria">
          <div className="monitoring-panel__head">
            <h3 className="monitoring-panel__title">Detalhamento por categoria</h3>
            <span className="monitoring-panel__meta">
              RAM privada é exclusiva; páginas compartilhadas não são aditivas entre processos
            </span>
          </div>
          {categories ? (
            <div className="monitoring-table-wrap">
              <table className="monitoring-table">
                <thead>
                  <tr>
                    <th scope="col">Categoria</th>
                    <th scope="col">Processos</th>
                    <th scope="col">CPU</th>
                    <th scope="col">RAM privada</th>
                    <th scope="col">Working Set</th>
                    <th scope="col">Compartilhada</th>
                  </tr>
                </thead>
                <tbody>
                  {categories.map((row) => {
                    const m = row.metrics;
                    const rowCpu = m ? formatPercentPtBR(m.cpu_usage_percent) ?? "indisponível" : "indisponível";
                    const rowPriv =
                      m && typeof m.private_ram_bytes === "number" && m.private_ram_bytes > 0
                        ? formatBytesPtBR(m.private_ram_bytes) ?? "indisponível"
                        : m?.process_count === 0
                          ? "0,0 KB"
                          : "indisponível";
                    const wsBytes = m ? m.working_set_ram_bytes ?? m.ram_bytes : 0;
                    const rowWs =
                      m && wsBytes > 0 ? formatBytesPtBR(wsBytes) ?? "indisponível" : m?.process_count === 0 ? "0,0 KB" : "indisponível";
                    const rowShared =
                      m && typeof m.shared_ram_bytes === "number" && m.shared_ram_bytes >= 0
                        ? formatBytesPtBR(m.shared_ram_bytes) ?? "0,0 KB"
                        : "indisponível";
                    return (
                      <tr key={row.role}>
                        <td>{row.label}</td>
                        <td>{m ? m.process_count : "indisponível"}</td>
                        <td>
                          <span>{rowCpu}</span>{" "}
                          <span className={provenanceToneClass(m?.cpu_provenance)}>
                            {provenanceBadgeText(m?.cpu_provenance)}
                          </span>
                        </td>
                        <td>
                          <span>{rowPriv}</span>{" "}
                          <span className={provenanceToneClass(m?.private_ram_provenance)}>
                            {provenanceBadgeText(m?.private_ram_provenance)}
                          </span>
                        </td>
                        <td>
                          <span>{rowWs}</span>{" "}
                          <span className={provenanceToneClass(m?.ram_provenance)}>
                            {provenanceBadgeText(m?.ram_provenance)}
                          </span>
                        </td>
                        <td>{rowShared}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="monitoring-empty">Detalhamento por categoria indisponível.</p>
          )}
        </section>

        <section className="monitoring-panel" aria-label="Agentes de IA em execução">
          <div className="monitoring-panel__head">
            <h3 className="monitoring-panel__title">Agentes de IA identificados</h3>
            <span className="monitoring-panel__meta">
              {agentNodes.length} {agentNodes.length === 1 ? "processo de agente" : "processos de agentes"}
            </span>
          </div>
          {agentNodes.length > 0 ? (
            <div className="monitoring-table-wrap">
              <table className="monitoring-table">
                <thead>
                  <tr>
                    <th scope="col">Agente</th>
                    <th scope="col">PID</th>
                    <th scope="col">Processo</th>
                    <th scope="col">CPU</th>
                    <th scope="col">RAM privada</th>
                  </tr>
                </thead>
                <tbody>
                  {agentNodes.map((proc) => (
                    <tr key={`agent-${proc.pid}-${proc.start_time ?? proc.create_time_ns ?? 0}`}>
                      <td>
                        <span className="badge badge--ok">{formatAgentLabel(proc.agent_kind) ?? "Agente"}</span>
                      </td>
                      <td className="monitoring-table__mono">{proc.pid}</td>
                      <td>{proc.name}</td>
                      <td>{formatPercentPtBR(proc.cpu_usage_percent) ?? "indisponível"}</td>
                      <td>
                        {typeof proc.private_ram_bytes === "number"
                          ? formatBytesPtBR(proc.private_ram_bytes) ?? "indisponível"
                          : "indisponível"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="monitoring-empty">Nenhum agente de IA identificado em execução no momento.</p>
          )}
        </section>

        <section className="monitoring-panel" aria-label="Processos com maior consumo">
          <div className="monitoring-panel__head">
            <h3 className="monitoring-panel__title">Processos com maior consumo</h3>
            <span className="monitoring-panel__meta">Ordenados por CPU e RAM privada</span>
          </div>
          {topProcesses.length > 0 ? (
            <div className="monitoring-table-wrap">
              <table className="monitoring-table">
                <thead>
                  <tr>
                    <th scope="col">PID</th>
                    <th scope="col">Processo</th>
                    <th scope="col">Categoria</th>
                    <th scope="col">CPU</th>
                    <th scope="col">RAM privada</th>
                    <th scope="col">Working Set</th>
                  </tr>
                </thead>
                <tbody>
                  {topProcesses.map((proc) => {
                    const agentBadge = formatAgentLabel(proc.agent_kind);
                    const privText =
                      typeof proc.private_ram_bytes === "number" && proc.private_ram_provenance !== "unknown"
                        ? formatBytesPtBR(proc.private_ram_bytes) ?? "indisponível"
                        : "indisponível";
                    const wsText =
                      proc.ram_bytes !== null ? formatBytesPtBR(proc.ram_bytes) ?? "indisponível" : "indisponível";
                    return (
                      <tr key={`${proc.pid}-${proc.start_time ?? proc.create_time_ns ?? 0}`}>
                        <td className="monitoring-table__mono">{proc.pid}</td>
                        <td>{proc.name || "processo"}</td>
                        <td>
                          {ROLE_LABELS[proc.role] ?? proc.role}
                          {agentBadge && (
                            <>
                              {" "}
                              <span className="badge badge--ok">{agentBadge}</span>
                            </>
                          )}
                        </td>
                        <td>
                          <span>{formatPercentPtBR(proc.cpu_usage_percent) ?? "indisponível"}</span>{" "}
                          <span className={provenanceToneClass(proc.cpu_provenance)}>
                            {provenanceBadgeText(proc.cpu_provenance)}
                          </span>
                        </td>
                        <td>{privText}</td>
                        <td>{wsText}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="monitoring-empty">Nenhum processo detalhado disponível na árvore atual.</p>
          )}
        </section>

        <section className="monitoring-panel" aria-label="Sessões supervisionadas">
          <div className="monitoring-panel__head">
            <h3 className="monitoring-panel__title">Terminais e agentes supervisionados</h3>
            <span className="monitoring-panel__meta">
              {sessionSummary.humanCount} {sessionSummary.humanCount === 1 ? "terminal" : "terminais"} ·{" "}
              {sessionSummary.agentCount} {sessionSummary.agentCount === 1 ? "agente" : "agentes"}
            </span>
          </div>
          {panes.length > 0 ? (
            <ul className="monitoring-sessions">
              {panes.map((pane) => {
                const st = sessionStateBadge(paneStates[pane.sessionId]);
                const roleText = pane.kind === "governed_agent" ? "Agente governado" : "Terminal humano";
                const node = typeof pane.pid === "number" ? nodesByPid.get(pane.pid) : undefined;
                const procSummary = node
                  ? `CPU ${formatPercentPtBR(node.cpu_usage_percent) ?? "indisponível"} · RAM priv. ${
                      typeof node.private_ram_bytes === "number" && node.private_ram_provenance !== "unknown"
                        ? formatBytesPtBR(node.private_ram_bytes) ?? "indisponível"
                        : formatBytesPtBR(node.ram_bytes ?? 0) ?? "indisponível"
                    }`
                  : null;
                return (
                  <li key={pane.sessionId} className="monitoring-sessions__item">
                    <div className="monitoring-sessions__main">
                      <strong>{`Terminal ${formatTerminalNumber(pane.number)}`}</strong>
                      <span className="monitoring-sessions__role">{roleText}</span>
                      {pane.interpreterLabel && <span className="badge">{pane.interpreterLabel}</span>}
                      <span className={st.className}>{st.text}</span>
                      {procSummary && <span className="monitoring-panel__meta">{procSummary}</span>}
                    </div>
                    {pane.cwd && <span className="monitoring-sessions__cwd">{pane.cwd}</span>}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="monitoring-empty">Nenhuma sessão aberta no momento.</p>
          )}
        </section>

        <ProvidersSection
          telemetry={providersTelemetry}
          loading={providersLoading}
          error={providersError}
          onRefresh={onRefreshProviders}
        />
      </div>
    </div>
  );
};
