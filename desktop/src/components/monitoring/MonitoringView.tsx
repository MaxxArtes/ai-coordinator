import React, { useMemo } from "react";
import { Icon } from "../ui/Icon";
import { mainViewPanelId, mainViewTabId } from "../shell/MainNav";
import type { ConnectionStatus, RuntimeMetrics } from "../shell/AppHeader";
import { formatBytesPtBR } from "../shell/StatusBar";
import type { TerminalPaneData } from "../TerminalPane";
import type { PaneState } from "../useTerminalStates";
import { formatTerminalNumber } from "../terminalIdentity";
import {
  buildSparklineSegments,
  computeMonitoringAlerts,
  DEFAULT_MONITORING_THRESHOLDS,
  formatPercentPtBR,
  orderedCategories,
  provenanceBadgeText,
  provenanceToneClass,
  resolvedCpuReading,
  resolvedRamReading,
  ROLE_LABELS,
  sortTopProcesses,
  summarizeSessions,
  type HistorySample,
  type MonitoringThresholds,
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
}

const CHART_WIDTH = 320;
const CHART_HEIGHT = 72;

const sessionStateBadge = (state?: PaneState): { text: string; className: string } => {
  if (!state || state.kind === "unknown") return { text: "indisponível", className: "badge badge--unknown" };
  if (state.kind === "alive") return { text: "ativo", className: "badge badge--ok" };
  if (state.kind === "preview") return { text: "preview", className: "badge badge--unknown" };
  return {
    text: state.exitCode === null ? "encerrado" : `encerrado (código ${state.exitCode})`,
    className: "badge badge--err",
  };
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
}) => {
  const cpuReading = resolvedCpuReading(metrics, metricsUnavailable);
  const ramReading = resolvedRamReading(metrics, metricsUnavailable);
  const cpuText = formatPercentPtBR(cpuReading.value) ?? "indisponível";
  const ramText = ramReading.value !== null ? formatBytesPtBR(ramReading.value) ?? "indisponível" : "indisponível";
  const hostRamText =
    !metricsUnavailable && metrics && metrics.host_ram_bytes > 0
      ? formatBytesPtBR(metrics.host_ram_bytes) ?? "indisponível"
      : "indisponível";

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

  const sessionSummary = useMemo(() => summarizeSessions(panes, paneStates), [panes, paneStates]);

  const cpuSeries = useMemo(() => history.map((s) => s.cpuPercent), [history]);
  const ramSeries = useMemo(() => history.map((s) => s.totalRamBytes), [history]);

  const cpuSegments = useMemo(
    () => buildSparklineSegments(cpuSeries, CHART_WIDTH, CHART_HEIGHT, 100),
    [cpuSeries]
  );

  const maxObservedRam = useMemo(() => {
    const valid = ramSeries.filter((v): v is number => typeof v === "number" && v > 0);
    if (valid.length === 0) return 0;
    return Math.max(...valid, thresholds.ramWarnBytes * 0.25);
  }, [ramSeries, thresholds.ramWarnBytes]);

  const ramSegments = useMemo(
    () => (maxObservedRam > 0 ? buildSparklineSegments(ramSeries, CHART_WIDTH, CHART_HEIGHT, maxObservedRam) : []),
    [ramSeries, maxObservedRam]
  );

  const hasCpuHistory = cpuSeries.some((v) => v !== null);
  const hasRamHistory = ramSeries.some((v) => v !== null);
  const warnLineY = (4 + (CHART_HEIGHT - 8) * (1 - Math.min(1, thresholds.cpuWarnPercent / 100))).toFixed(1);

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
              Consumo da árvore de processos (atualização a cada 2 s
              {!metricsUnavailable && metrics?.sample_interval_ms ? ` · última janela: ${metrics.sample_interval_ms} ms` : ""}
              ).
            </p>
          </div>
          <div className="monitoring-view__legend" aria-label="Legenda de origem das métricas">
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

        <section className="monitoring-cards" aria-label="Resumo do consumo atual">
          <article className="monitoring-card">
            <div className="monitoring-card__head">
              <span className="monitoring-card__label">CPU da árvore</span>
              <span className={provenanceToneClass(cpuReading.provenance)}>
                {provenanceBadgeText(cpuReading.provenance)}
              </span>
            </div>
            <p className="monitoring-card__value">{cpuText}</p>
            <p className="monitoring-card__meta">Limiar discreto: {thresholds.cpuWarnPercent}%</p>
          </article>

          <article className="monitoring-card">
            <div className="monitoring-card__head">
              <span className="monitoring-card__label">RAM da árvore</span>
              <span className={provenanceToneClass(ramReading.provenance)}>
                {provenanceBadgeText(ramReading.provenance)}
              </span>
            </div>
            <p className="monitoring-card__value">{ramText}</p>
            <p className="monitoring-card__meta">
              Host{!metricsUnavailable && metrics?.host_pid ? ` (PID ${metrics.host_pid})` : ""}: {hostRamText}
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
              <span className="monitoring-card__label">Cotas e custos</span>
              <span className="badge badge--unknown">indisponível</span>
            </div>
            <p className="monitoring-card__value monitoring-card__value--muted">Não coletado</p>
            <p className="monitoring-card__meta">Previsto na fase 4D.4.3 — nenhum dado simulado exibido.</p>
          </article>
        </section>

        <section className="monitoring-charts" aria-label="Gráficos históricos recentes">
          <article className="monitoring-panel">
            <div className="monitoring-panel__head">
              <h3 className="monitoring-panel__title">Evolução recente de CPU</h3>
              <span className="monitoring-panel__meta">
                {history.length}/{60} amostras
              </span>
            </div>
            {hasCpuHistory ? (
              <svg
                className="monitoring-chart"
                viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
                role="img"
                aria-label="Histórico recente de CPU"
              >
                <line
                  x1="4"
                  y1={warnLineY}
                  x2={CHART_WIDTH - 4}
                  y2={warnLineY}
                  className="monitoring-chart__threshold"
                />
                {cpuSegments.map((d, idx) => (
                  <path key={idx} d={d} className="monitoring-chart__line monitoring-chart__line--cpu" />
                ))}
              </svg>
            ) : (
              <p className="monitoring-empty">Sem medições de CPU disponíveis no histórico recente.</p>
            )}
          </article>

          <article className="monitoring-panel">
            <div className="monitoring-panel__head">
              <h3 className="monitoring-panel__title">Evolução recente de RAM</h3>
              <span className="monitoring-panel__meta">
                {maxObservedRam > 0 ? `Pico: ${formatBytesPtBR(maxObservedRam) ?? "indisponível"}` : "Sem amostras"}
              </span>
            </div>
            {hasRamHistory ? (
              <svg
                className="monitoring-chart"
                viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`}
                role="img"
                aria-label="Histórico recente de RAM"
              >
                {ramSegments.map((d, idx) => (
                  <path key={idx} d={d} className="monitoring-chart__line monitoring-chart__line--ram" />
                ))}
              </svg>
            ) : (
              <p className="monitoring-empty">Sem medições de RAM disponíveis no histórico recente.</p>
            )}
          </article>
        </section>

        <section className="monitoring-panel" aria-label="Detalhamento por categoria">
          <div className="monitoring-panel__head">
            <h3 className="monitoring-panel__title">Detalhamento por categoria</h3>
            <span className="monitoring-panel__meta">Árvore deduplicada do aplicativo</span>
          </div>
          {categories ? (
            <div className="monitoring-table-wrap">
              <table className="monitoring-table">
                <thead>
                  <tr>
                    <th scope="col">Categoria</th>
                    <th scope="col">Processos</th>
                    <th scope="col">CPU</th>
                    <th scope="col">RAM</th>
                  </tr>
                </thead>
                <tbody>
                  {categories.map((row) => {
                    const m = row.metrics;
                    const rowCpu = m ? formatPercentPtBR(m.cpu_usage_percent) ?? "indisponível" : "indisponível";
                    const rowRam = m && m.ram_bytes > 0 ? formatBytesPtBR(m.ram_bytes) ?? "indisponível" : m?.process_count === 0 ? "0,0 KB" : "indisponível";
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
                          <span>{rowRam}</span>{" "}
                          <span className={provenanceToneClass(m?.ram_provenance)}>
                            {provenanceBadgeText(m?.ram_provenance)}
                          </span>
                        </td>
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

        <section className="monitoring-panel" aria-label="Processos com maior consumo">
          <div className="monitoring-panel__head">
            <h3 className="monitoring-panel__title">Processos com maior consumo</h3>
            <span className="monitoring-panel__meta">Ordenados por CPU e RAM</span>
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
                    <th scope="col">RAM</th>
                  </tr>
                </thead>
                <tbody>
                  {topProcesses.map((proc) => (
                    <tr key={`${proc.pid}-${proc.start_time ?? proc.create_time_ns ?? 0}`}>
                      <td className="monitoring-table__mono">{proc.pid}</td>
                      <td>{proc.name || "processo"}</td>
                      <td>{ROLE_LABELS[proc.role] ?? proc.role}</td>
                      <td>
                        <span>{formatPercentPtBR(proc.cpu_usage_percent) ?? "indisponível"}</span>{" "}
                        <span className={provenanceToneClass(proc.cpu_provenance)}>
                          {provenanceBadgeText(proc.cpu_provenance)}
                        </span>
                      </td>
                      <td>
                        <span>{proc.ram_bytes !== null ? formatBytesPtBR(proc.ram_bytes) ?? "indisponível" : "indisponível"}</span>
                      </td>
                    </tr>
                  ))}
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
                return (
                  <li key={pane.sessionId} className="monitoring-sessions__item">
                    <div className="monitoring-sessions__main">
                      <strong>{`Terminal ${formatTerminalNumber(pane.number)}`}</strong>
                      <span className="monitoring-sessions__role">{roleText}</span>
                      {pane.interpreterLabel && <span className="badge">{pane.interpreterLabel}</span>}
                      <span className={st.className}>{st.text}</span>
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
      </div>
    </div>
  );
};
