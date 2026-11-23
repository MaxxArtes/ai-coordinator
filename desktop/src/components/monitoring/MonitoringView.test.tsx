import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MonitoringView } from "./MonitoringView";
import { appendHistorySample, MAX_HISTORY_POINTS, type HistorySample } from "./history";
import type { RuntimeMetrics } from "../shell/AppHeader";
import type { TerminalPaneData } from "../TerminalPane";
import type { PaneState } from "../useTerminalStates";

const FULL_METRICS: RuntimeMetrics = {
  host_pid: 4242,
  core_pid: 5150,
  host_ram_bytes: 58 * 1024 * 1024,
  total_tree_ram_bytes: 210 * 1024 * 1024,
  ram_provenance: "measured",
  cpu_usage_percent: 18.4,
  cpu_provenance: "measured",
  sample_interval_ms: 2000,
  process_count: 4,
  sampled_at_unix_ms: 1_700_000_000_000,
  categories: {
    host: { role: "host", process_count: 1, ram_bytes: 58 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 2.1, cpu_provenance: "measured" },
    core: { role: "core", process_count: 1, ram_bytes: 32 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 1.3, cpu_provenance: "measured" },
    webview: { role: "webview", process_count: 1, ram_bytes: 90 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 11.0, cpu_provenance: "measured" },
    terminal: { role: "terminal", process_count: 1, ram_bytes: 30 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 4.0, cpu_provenance: "measured" },
    child: { role: "child", process_count: 0, ram_bytes: 0, ram_provenance: "unknown", cpu_usage_percent: null, cpu_provenance: "unknown" },
  },
  nodes: [
    { pid: 4242, ppid: 1, role: "host", name: "ai-coordinator-desktop.exe", ram_bytes: 58 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 2.1, cpu_provenance: "measured" },
    { pid: 6001, ppid: 4242, role: "webview", name: "msedgewebview2.exe", ram_bytes: 90 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 11.0, cpu_provenance: "measured" },
    { pid: 5150, ppid: 4242, role: "core", name: "python.exe", ram_bytes: 32 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 1.3, cpu_provenance: "measured" },
    { pid: 7002, ppid: 5150, role: "terminal", name: "pwsh.exe", ram_bytes: 30 * 1024 * 1024, ram_provenance: "measured", cpu_usage_percent: 4.0, cpu_provenance: "measured" },
  ],
};

const PANES: TerminalPaneData[] = [
  { sessionId: "s-1", tabId: "tab-main", paneId: 0, kind: "human_terminal", number: 1, interpreterId: "powershell", interpreterLabel: "PowerShell 7", cwd: "C:\\Users\\10864" },
  { sessionId: "s-2", tabId: "tab-2", paneId: 1, kind: "governed_agent", number: 2, interpreterId: "cmd", interpreterLabel: "CMD", cwd: "D:\\ai-coordinator" },
];

const STATES: Record<string, PaneState> = {
  "s-1": { kind: "alive" },
  "s-2": { kind: "ended", exitCode: 0 },
};

describe("MonitoringView and history buffer (4D.4.2)", () => {
  it("caps history at MAX_HISTORY_POINTS (60) and records explicit unknown gaps on failure", () => {
    let history: HistorySample[] = [];
    for (let i = 0; i < MAX_HISTORY_POINTS + 15; i++) {
      history = appendHistorySample(history, FULL_METRICS, false, 1000 + i * 2000);
    }
    expect(history).toHaveLength(MAX_HISTORY_POINTS);
    expect(history[0].timestampMs).toBe(FULL_METRICS.sampled_at_unix_ms);

    const afterFailure = appendHistorySample(history, FULL_METRICS, true, 999_999);
    expect(afterFailure).toHaveLength(MAX_HISTORY_POINTS);
    const last = afterFailure[afterFailure.length - 1];
    expect(last.cpuPercent).toBeNull();
    expect(last.cpuProvenance).toBe("unknown");
    expect(last.totalRamBytes).toBeNull();
    expect(last.ramProvenance).toBe("unknown");
  });

  it("renders real CPU/RAM, categories, sorted processes, active/ended sessions and 4D.4.3 placeholder honestly", () => {
    const history = appendHistorySample([], FULL_METRICS, false, 1_700_000_000_000);
    render(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={FULL_METRICS}
        metricsUnavailable={false}
        history={history}
        panes={PANES}
        paneStates={STATES}
      />
    );

    const summary = screen.getByRole("region", { name: "Resumo do consumo atual" });
    expect(summary).toHaveTextContent("18,4%");
    expect(summary).toHaveTextContent("210,0 MB");
    expect(summary).toHaveTextContent("Host (PID 4242): 58,0 MB");
    expect(summary).toHaveTextContent("4 processos");
    expect(summary).toHaveTextContent("1 ativas · 1 encerradas · 0 indisponíveis");
    expect(summary).toHaveTextContent("Previsto na fase 4D.4.3 — nenhum dado simulado exibido.");

    const catSection = screen.getByRole("region", { name: "Detalhamento por categoria" });
    expect(catSection).toHaveTextContent("Aplicativo (Host)");
    expect(catSection).toHaveTextContent("Core Python");
    expect(catSection).toHaveTextContent("WebView2");
    expect(catSection).toHaveTextContent("Terminais");
    expect(catSection).toHaveTextContent("Filhos e agentes");

    const procSection = screen.getByRole("region", { name: "Processos com maior consumo" });
    const rows = within(procSection).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual(["6001", "7002", "4242", "5150"]);

    const sessSection = screen.getByRole("region", { name: "Sessões supervisionadas" });
    expect(sessSection).toHaveTextContent("Terminal 01");
    expect(sessSection).toHaveTextContent("ativo");
    expect(sessSection).toHaveTextContent("Terminal 02");
    expect(sessSection).toHaveTextContent("Agente governado");
    expect(sessSection).toHaveTextContent("encerrado (código 0)");

    expect(screen.getByRole("img", { name: "Histórico recente de CPU" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Histórico recente de RAM" })).toBeInTheDocument();
  });

  it("never fakes 0% CPU or stale RAM when unavailable or without provenance", () => {
    const legacyNoProv = { host_pid: 4242, host_ram_bytes: 58_000_000, total_tree_ram_bytes: 120_000_000, cpu_usage_percent: 37.5 };
    render(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={legacyNoProv}
        metricsUnavailable={false}
        history={[]}
        panes={[]}
        paneStates={{}}
      />
    );
    expect(screen.queryByText(/37[,.]5/)).toBeNull();
    expect(screen.getByText("CPU ainda sem linha de base temporal")).toBeInTheDocument();
    expect(screen.getByText("Sem medições de CPU disponíveis no histórico recente.")).toBeInTheDocument();

    const { rerender } = render(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={FULL_METRICS}
        metricsUnavailable
        history={[]}
        panes={[]}
        paneStates={{}}
      />
    );
    rerender(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={FULL_METRICS}
        metricsUnavailable
        history={[]}
        panes={[]}
        paneStates={{}}
      />
    );
    expect(screen.getByText("Leitura de telemetria indisponível")).toBeInTheDocument();
    expect(screen.queryByText("18,4%")).toBeNull();
    expect(screen.queryByText("210,0 MB")).toBeNull();
  });

  it("shows discrete threshold alerts when CPU or RAM approaches or exceeds limits", () => {
    const hot: RuntimeMetrics = {
      ...FULL_METRICS,
      cpu_usage_percent: 88.2,
      cpu_provenance: "measured",
      total_tree_ram_bytes: 2.3 * 1024 * 1024 * 1024,
      ram_provenance: "measured",
    };
    render(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={hot}
        metricsUnavailable={false}
        history={appendHistorySample([], hot, false, 1000)}
        panes={PANES}
        paneStates={STATES}
      />
    );
    const alerts = screen.getByRole("status", { name: "Alertas de monitoramento" });
    expect(alerts).toHaveTextContent("CPU da árvore elevada (88,2%)");
    expect(alerts).toHaveTextContent("Uso crítico de RAM da árvore");
  });

  it("reports preview unavailability clearly without invented numbers", () => {
    render(
      <MonitoringView
        active
        isTauri={false}
        connectionStatus="connected"
        metrics={null}
        metricsUnavailable={false}
        history={[]}
        panes={[]}
        paneStates={{}}
      />
    );
    expect(screen.getByText("Coletor nativo indisponível (modo preview)")).toBeInTheDocument();
  });
});
