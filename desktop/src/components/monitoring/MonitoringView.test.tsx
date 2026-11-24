import React from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MonitoringView } from "./MonitoringView";
import { appendHistorySample, CPU_CHART_SCALE_STORAGE_KEY, MAX_HISTORY_POINTS, type HistorySample } from "./history";
import type { RuntimeMetrics } from "../shell/AppHeader";
import type { ProvidersTelemetrySnapshot } from "./telemetryContract";
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
    expect(summary).toHaveTextContent("Aguardando leitura de provedores — nenhum dado simulado exibido.");

    const catSection = screen.getByRole("region", { name: "Detalhamento por categoria" });
    expect(catSection).toHaveTextContent("Aplicativo (Host)");
    expect(catSection).toHaveTextContent("Core Python");
    expect(catSection).toHaveTextContent("WebView2");
    expect(catSection).toHaveTextContent("Terminais");
    expect(catSection).toHaveTextContent("Agentes de IA");
    expect(catSection).toHaveTextContent("Filhos genéricos");

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

  it("separates system resources, exclusive private RAM, AI agents, scaled charts, and session PID metrics", () => {
    const detailed: RuntimeMetrics = {
      ...FULL_METRICS,
      data_origin: "live_os",
      ttl_ms: 10_000,
      host_private_ram_bytes: 34_000_000,
      total_private_ram_bytes: 148 * 1024 * 1024,
      private_ram_provenance: "measured",
      total_working_set_ram_bytes: 547 * 1024 * 1024,
      total_shared_ram_bytes: 399 * 1024 * 1024,
      total_tree_ram_bytes: 547 * 1024 * 1024,
      logical_cpu_count: 8,
      categories: {
        ...FULL_METRICS.categories!,
        agent: {
          role: "agent",
          process_count: 1,
          ram_bytes: 110 * 1024 * 1024,
          ram_provenance: "measured",
          private_ram_bytes: 82 * 1024 * 1024,
          private_ram_provenance: "measured",
          working_set_ram_bytes: 110 * 1024 * 1024,
          shared_ram_bytes: 28 * 1024 * 1024,
          cpu_usage_percent: 6.2,
          cpu_provenance: "measured",
        },
      },
      nodes: [
        ...(FULL_METRICS.nodes ?? []),
        {
          pid: 8801,
          ppid: 7002,
          role: "agent",
          name: "claude.exe",
          ram_bytes: 110 * 1024 * 1024,
          ram_provenance: "measured",
          private_ram_bytes: 82 * 1024 * 1024,
          private_ram_provenance: "measured",
          working_set_ram_bytes: 110 * 1024 * 1024,
          shared_ram_bytes: 28 * 1024 * 1024,
          cpu_usage_percent: 6.2,
          cpu_provenance: "measured",
          agent_kind: "claude",
        },
      ],
      system: {
        logical_cpu_count: 8,
        cpu_usage_percent: 48.0,
        cpu_provenance: "measured",
        ram_total_bytes: 32 * 1024**3,
        ram_used_bytes: 22 * 1024**3,
        ram_available_bytes: 10 * 1024**3,
        ram_usage_percent: 68.8,
        ram_provenance: "measured",
        storage_total_bytes: 1000 * 1024**3,
        storage_used_bytes: 620 * 1024**3,
        storage_available_bytes: 380 * 1024**3,
        storage_usage_percent: 62.0,
        storage_provenance: "measured",
        storage_volumes: [{ name: "C:", mount_point: "C:\\", total_bytes: 1000 * 1024**3, used_bytes: 620 * 1024**3, available_bytes: 380 * 1024**3, usage_percent: 62.0 }],
        disk_activity_percent: 12.0,
        disk_activity_provenance: "measured",
        disk_read_bytes_per_sec: 1_500_000,
        disk_write_bytes_per_sec: 500_000,
        disk_io_provenance: "measured",
        network_rx_bytes_per_sec: 8_000_000,
        network_tx_bytes_per_sec: 400_000,
        network_total_bytes_per_sec: 8_400_000,
        network_provenance: "measured",
        gpu_usage_percent: 3.0,
        gpu_provenance: "measured",
      },
    };
    let hist: HistorySample[] = [];
    hist = appendHistorySample(hist, detailed, false, 1_700_000_000_000);
    hist = appendHistorySample(hist, detailed, false, 1_700_000_002_000);

    const panesWithPid: TerminalPaneData[] = [
      ...PANES,
      { sessionId: "s-3", tabId: "tab-main", paneId: 2, kind: "governed_agent", number: 3, pid: 8801, cwd: "D:\\ai-coordinator" },
    ];

    render(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={detailed}
        metricsUnavailable={false}
        history={hist}
        panes={panesWithPid}
        paneStates={{ ...STATES, "s-3": { kind: "alive" } }}
      />
    );

    const sysSection = screen.getByRole("region", { name: "Recursos do computador" });
    expect(sysSection).toHaveTextContent("48,0%");
    expect(sysSection).toHaveTextContent("68,8%");
    expect(sysSection).toHaveTextContent("62,0%");
    expect(sysSection).toHaveTextContent("12,0%");
    expect(sysSection).toHaveTextContent("8,0 MB/s");
    expect(sysSection).toHaveTextContent("3,0%");

    const summary = screen.getByRole("region", { name: "Resumo do consumo atual" });
    expect(summary).toHaveTextContent("148,0 MB");
    expect(summary).toHaveTextContent("Working Set total (inclui compartilhada): 547,0 MB");

    const agentsSection = screen.getByRole("region", { name: "Agentes de IA em execução" });
    expect(agentsSection).toHaveTextContent("Claude");
    expect(agentsSection).toHaveTextContent("8801");
    expect(agentsSection).toHaveTextContent("82,0 MB");

    const chartsSection = screen.getByRole("region", { name: "Gráficos históricos recentes" });
    expect(within(chartsSection).getByRole("button", { name: "Global 0–100%" })).toBeInTheDocument();
    expect(chartsSection).toHaveTextContent("Escala: 0–25% (pico da janela com margem)");
    expect(chartsSection).toHaveTextContent("agora");
    expect(chartsSection).toHaveTextContent("pico");

    const sessSection = screen.getByRole("region", { name: "Sessões supervisionadas" });
    expect(sessSection).toHaveTextContent("CPU 6,2% · RAM priv. 82,0 MB");
  });

  it("renders 4D.4.3 provider cards, 5h/weekly quotas, tokens, costs, and test_fixture segregation", () => {
    const provSnap: ProvidersTelemetrySnapshot = {
      schema_version: 1,
      profile_name: "operador",
      sampled_at_unix_ms: Date.now() - 1000,
      ttl_ms: 300_000,
      stale: true,
      data_origin: "test_fixture",
      provenance: "measured",
      sources: { sqlite_available: true, quota_snapshot_available: true, vault_keys_checked: true },
      providers: [
        {
          provider: "claude",
          display_name: "Claude",
          profile_name: "operador",
          launcher_available: true,
          has_vault_key: true,
          availability_status: "available",
          quota_kind: "subscription_quota",
          windows: {
            five_hour: {
              window: "5h",
              label: "5 horas",
              used_percent: 38.0,
              remaining_percent: 62.0,
              resets_at_iso: "2026-11-24T19:00:00Z",
              provenance: "measured",
              stale: false,
            },
            weekly: {
              window: "weekly",
              label: "Semanal",
              used_percent: 64.0,
              remaining_percent: 36.0,
              resets_at_iso: "2026-11-30T00:00:00Z",
              provenance: "measured",
              stale: true,
            },
          },
          tokens: {
            input_tokens: 1200,
            output_tokens: 350,
            cache_read_tokens: 4000,
            cache_creation_tokens: 500,
            reasoning_tokens: 150,
            total_tokens: 6200,
            provenance: "measured",
          },
          cost_credits: {
            kind: "subscription_quota",
            currency: "USD",
            balance_usd: null,
            credits_remaining_usd: null,
            spent_usd: 0.0125,
            estimated_cost_usd: null,
            provenance: "measured",
          },
          models: [{ model: "sonnet", tokens: { input_tokens: 1200, output_tokens: 350, cache_read_tokens: null, cache_creation_tokens: null, reasoning_tokens: null, total_tokens: 1550, provenance: "measured" }, cost_usd: 0.0125, cost_provenance: "measured" }],
          data_origin: "test_fixture",
          observed_at_unix_ms: Date.now() - 1000,
          ttl_ms: 300_000,
          stale: true,
        },
        {
          provider: "codex",
          display_name: "Codex",
          profile_name: "operador",
          launcher_available: true,
          has_vault_key: false,
          availability_status: "unknown",
          quota_kind: "subscription_quota",
          windows: {
            five_hour: { window: "5h", label: "5 horas", used_percent: null, remaining_percent: null, resets_at_iso: null, provenance: "unknown" },
            weekly: { window: "weekly", label: "Semanal", used_percent: null, remaining_percent: null, resets_at_iso: null, provenance: "unknown" },
          },
          tokens: { input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_creation_tokens: null, reasoning_tokens: null, total_tokens: null, provenance: "unknown" },
          models: [],
          data_origin: "offline_cache",
          observed_at_unix_ms: null,
          ttl_ms: 300_000,
          stale: false,
        },
      ],
    };

    render(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={FULL_METRICS}
        metricsUnavailable={false}
        history={[]}
        panes={PANES}
        paneStates={STATES}
        providersTelemetry={provSnap}
      />
    );

    const provSection = screen.getByRole("region", { name: "Provedores de Inteligência Artificial" });
    expect(provSection).toHaveTextContent("Provedores em modo Fixture de teste");
    expect(provSection).toHaveTextContent("38,0%");
    expect(provSection).toHaveTextContent("64,0%");
    expect(provSection).toHaveTextContent("Entrada: 1.200");
    expect(provSection).toHaveTextContent("Raciocínio: 150");
    expect(provSection).toHaveTextContent("expirado");
    expect(provSection).toHaveTextContent("Codex");
  });
});

describe("CPU and RAM charts: visible lines and the adaptive scale (4D.4 r2)", () => {
  const VIEW_W = 380;
  const VIEW_H = 104;

  beforeEach(() => {
    window.localStorage.clear();
  });

  function historyOf(app: Array<number | null>, sys: number): HistorySample[] {
    let hist: HistorySample[] = [];
    app.forEach((cpu, i) => {
      const metrics: RuntimeMetrics = {
        ...FULL_METRICS,
        cpu_usage_percent: cpu,
        cpu_provenance: cpu === null ? "unknown" : "measured",
        sampled_at_unix_ms: 1_700_000_000_000 + i * 2000,
        system: { logical_cpu_count: 4, cpu_usage_percent: sys, cpu_provenance: "measured" },
      };
      hist = appendHistorySample(hist, metrics, false, 1_700_000_000_000 + i * 2000);
    });
    return hist;
  }

  function renderView(history: HistorySample[]) {
    return render(
      <MonitoringView
        active
        isTauri
        connectionStatus="connected"
        metrics={FULL_METRICS}
        metricsUnavailable={false}
        history={history}
        panes={[]}
        paneStates={{}}
      />
    );
  }

  function pathPoints(svg: Element, selector: string): Array<[number, number]> {
    return Array.from(svg.querySelectorAll(selector)).flatMap((path) =>
      [...(path.getAttribute("d") ?? "").matchAll(/[ML](-?[\d.]+) (-?[\d.]+)/g)].map((m) => [Number(m[1]), Number(m[2])] as [number, number])
    );
  }

  function expectInsideViewBox(points: Array<[number, number]>) {
    expect(points.length).toBeGreaterThan(0);
    for (const [x, y] of points) {
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x).toBeLessThanOrEqual(VIEW_W);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y).toBeLessThanOrEqual(VIEW_H);
    }
  }

  it("draws the CPU and RAM lines inside the viewBox (regression: the vertical padding left no room)", () => {
    renderView(historyOf([2.19, 3.47, 1.8], 31));
    const cpu = screen.getByRole("img", { name: "Histórico recente de CPU" });
    const ram = screen.getByRole("img", { name: "Histórico recente de RAM" });
    expect(cpu.getAttribute("viewBox")).toBe(`0 0 ${VIEW_W} ${VIEW_H}`);
    expectInsideViewBox(pathPoints(cpu, ".monitoring-chart__line--cpu"));
    expectInsideViewBox(pathPoints(ram, ".monitoring-chart__line--ram"));
  });

  it("focus (default) scales to the AI Coordinator peak, labels the Y axis and leaves the computer to the global scale", () => {
    renderView(historyOf([2.19, 3.47, 1.8], 31));
    const charts = screen.getByRole("region", { name: "Gráficos históricos recentes" });
    expect(within(charts).getByRole("button", { name: "Foco AI Coordinator" })).toHaveAttribute("aria-pressed", "true");
    expect(within(charts).getByRole("button", { name: "Global 0–100%" })).toHaveAttribute("aria-pressed", "false");
    const cpu = screen.getByRole("img", { name: "Histórico recente de CPU" });
    expect(cpu).toHaveAttribute("data-scale", "focus");
    expect(Array.from(cpu.querySelectorAll(".monitoring-chart__axis")).map((t) => t.textContent)).toEqual(["5%", "2,5%", "0%"]);
    expect(cpu.querySelectorAll(".monitoring-chart__line--sys")).toHaveLength(0);
    expect(cpu.querySelector(".monitoring-chart__threshold")).toBeNull();
    expect(charts).toHaveTextContent("Escala: 0–5% (pico da janela com margem)");
    expect(charts).toHaveTextContent("Computador: só na escala global");
    // 3,47% of a 0–5% scale is high in the chart, not flat on the bottom as on 0–100%.
    const ys = pathPoints(cpu, ".monitoring-chart__line--cpu").map(([, y]) => y);
    expect(Math.min(...ys)).toBeLessThan(VIEW_H / 2);
  });

  it("global shows 0–100% with both series and the warning line, and the choice is remembered", () => {
    const view = renderView(historyOf([2.19, 3.47, 1.8], 31));
    fireEvent.click(screen.getByRole("button", { name: "Global 0–100%" }));
    const cpu = screen.getByRole("img", { name: "Histórico recente de CPU" });
    expect(cpu).toHaveAttribute("data-scale", "global");
    expect(Array.from(cpu.querySelectorAll(".monitoring-chart__axis")).map((t) => t.textContent)).toEqual(["100%", "50%", "0%"]);
    expectInsideViewBox(pathPoints(cpu, ".monitoring-chart__line--sys"));
    expectInsideViewBox(pathPoints(cpu, ".monitoring-chart__line--cpu"));
    expect(cpu.querySelector(".monitoring-chart__threshold")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Global 0–100%" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("Computador")).toBeInTheDocument();
    expect(window.localStorage.getItem(CPU_CHART_SCALE_STORAGE_KEY)).toBe("global");

    view.unmount();
    renderView(historyOf([2.19, 3.47, 1.8], 31));
    expect(screen.getByRole("img", { name: "Histórico recente de CPU" })).toHaveAttribute("data-scale", "global");
    fireEvent.click(screen.getByRole("button", { name: "Foco AI Coordinator" }));
    expect(window.localStorage.getItem(CPU_CHART_SCALE_STORAGE_KEY)).toBe("focus");
  });

  it("constant, zero, single and above-100% series all leave a visible line inside the viewBox", () => {
    for (const series of [[4, 4, 4, 4], [0, 0, 0], [7.5], [180, 250, 120]]) {
      for (const mode of ["Foco AI Coordinator", "Global 0–100%"]) {
        const view = renderView(historyOf(series, 12));
        fireEvent.click(screen.getByRole("button", { name: mode }));
        const points = pathPoints(screen.getByRole("img", { name: "Histórico recente de CPU" }), ".monitoring-chart__line--cpu");
        expectInsideViewBox(points);
        expect(points.length).toBeGreaterThanOrEqual(2);
        view.unmount();
      }
    }
  });

  it("without AI Coordinator CPU samples, focus says so instead of drawing the computer on a wrong scale", () => {
    renderView(historyOf([null, null], 40));
    expect(screen.queryByRole("img", { name: "Histórico recente de CPU" })).toBeNull();
    expect(
      screen.getByText("Sem medições de CPU do AI Coordinator no histórico recente; a CPU do computador aparece na escala global.")
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Global 0–100%" }));
    const cpu = screen.getByRole("img", { name: "Histórico recente de CPU" });
    expectInsideViewBox(pathPoints(cpu, ".monitoring-chart__line--sys"));
    expect(cpu.querySelectorAll(".monitoring-chart__line--cpu")).toHaveLength(0);
  });
});
