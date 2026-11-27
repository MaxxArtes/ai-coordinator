/**
 * Status bar (Etapa 5, decision 6): measured RAM, CPU unavailable, nothing invented in the preview.
 */
import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { formatBytesPtBR, splitProjectPath, splitQualifier, StatusBar, type StatusBarProps } from "./StatusBar";

const base: StatusBarProps = {
  isTauri: true,
  connectionStatus: "connected",
  coreVersion: "0.6.1",
  paneCount: 2,
  metrics: { host_pid: 4242, host_ram_bytes: 61_000_000, total_tree_ram_bytes: 134_637_159, cpu_usage_percent: 37.5 },
  projectPath: "D:\\ai-coordinator",
};

const bar = () => screen.getByRole("contentinfo", { name: "Barra de status" });

describe("StatusBar", () => {
  it("Core, terminals and the measured RAM, in pt-BR", () => {
    render(<StatusBar {...base} />);
    expect(bar()).toHaveTextContent("Core v0.6.1 · conectado");
    expect(bar()).toHaveTextContent("Terminais: 2 de 4");
    expect(bar()).toHaveTextContent("RAM da árvore: 128,4 MB (medida)");
    expect(bar().textContent).not.toMatch(/ativos/);
  });

  it("before the first reading: 'aguardando', never '0 MB'", () => {
    render(<StatusBar {...base} metrics={null} />);
    expect(bar()).toHaveTextContent("RAM da árvore: aguardando");
    expect(bar().textContent).not.toMatch(/0[,.]0 ?MB|\b0 MB/);
  });

  it("the preview claims neither a Core nor a connection", () => {
    render(<StatusBar {...base} isTauri={false} paneCount={1} metrics={null} />);
    expect(bar()).toHaveTextContent("Preview: sem Core");
    expect(bar()).toHaveTextContent("RAM da árvore: indisponível (preview)");
    expect(bar()).toHaveTextContent("Terminais: 1 de 4 (preview)");
    expect(bar().textContent).not.toMatch(/conectado|0\.6\.1/);
  });

  it("an unknown Core version leaves no lone 'v'", () => {
    const { rerender } = render(<StatusBar {...base} coreVersion="indisponível" />);
    expect(bar()).toHaveTextContent("Core (versão indisponível) · conectado");
    expect(bar().textContent).not.toMatch(/vindispon/);
    rerender(<StatusBar {...base} coreVersion={null} connectionStatus="connecting" metrics={null} />);
    expect(bar()).toHaveTextContent("Core · conectando…");
  });

  it("a zero reading or a failed reading is not shown as a measurement", () => {
    const { rerender } = render(<StatusBar {...base} metrics={{ host_pid: 0, host_ram_bytes: 0, total_tree_ram_bytes: 0 }} />);
    expect(bar()).toHaveTextContent("RAM da árvore: indisponível");
    expect(bar().textContent).not.toMatch(/\(medida\)/);
    rerender(<StatusBar {...base} metricsUnavailable />);
    expect(bar()).toHaveTextContent("RAM da árvore: indisponível");
    expect(bar().textContent).not.toMatch(/128,4|\(medida\)/);
  });

  it("CPU stays 'indisponível' when the host sends a number without provenance", () => {
    render(<StatusBar {...base} />);
    expect(bar()).toHaveTextContent("CPU: indisponível");
    expect(bar().textContent).not.toMatch(/37[,.]5/);
  });

  it("shows measured CPU percentage when provenance is measured", () => {
    render(
      <StatusBar
        {...base}
        metrics={{
          ...base.metrics!,
          cpu_usage_percent: 4.5,
          cpu_provenance: "measured",
        }}
      />,
    );
    expect(bar()).toHaveTextContent("CPU: 4,5% (medida)");
  });

  it("shows estimated CPU percentage when provenance is estimated", () => {
    render(
      <StatusBar
        {...base}
        metrics={{
          ...base.metrics!,
          cpu_usage_percent: 12.25,
          cpu_provenance: "estimated",
        }}
      />,
    );
    expect(bar()).toHaveTextContent("CPU: 12,3% (estimada)");
  });

  it("shows 'indisponível' when CPU provenance is unknown even if numeric value is zero or null", () => {
    render(
      <StatusBar
        {...base}
        metrics={{
          ...base.metrics!,
          cpu_usage_percent: 0,
          cpu_provenance: "unknown",
        }}
      />,
    );
    expect(bar()).toHaveTextContent("CPU: indisponível");
    expect(bar().textContent).not.toContain("0,0%");
  });

  it("shows '0,0% (medida)' only when CPU is explicitly measured as zero", () => {
    render(
      <StatusBar
        {...base}
        metrics={{
          ...base.metrics!,
          cpu_usage_percent: 0,
          cpu_provenance: "measured",
        }}
      />,
    );
    expect(bar()).toHaveTextContent("CPU: 0,0% (medida)");
  });

  it("is not a live region (values change every 2 s)", () => {
    render(<StatusBar {...base} />);
    expect(bar().querySelectorAll("[aria-live], [role=status], [role=alert], [role=log], [role=timer]")).toHaveLength(0);
    expect(bar()).not.toHaveAttribute("aria-live");
  });

  it("keeps the full path accessible while the head can shrink", () => {
    render(<StatusBar {...base} projectPath={"D:\\work\\clients\\ai-coordinator"} />);
    expect(screen.getByText("Projeto: D:\\work\\clients\\ai-coordinator")).toHaveClass("sr-only");
    const path = bar().querySelector(".status-bar__path")!;
    expect(path).toHaveAttribute("title", "D:\\work\\clients\\ai-coordinator");
    expect(path.querySelector(".status-bar__path-head")).toHaveTextContent("D:\\work\\clients\\");
    expect(path.querySelector(".status-bar__path-tail")).toHaveTextContent("ai-coordinator");
    expect(bar().querySelector(".status-bar__group:not(.status-bar__group--end)")).toHaveTextContent("CPU: indisponível");
  });

  it("narrow windows (960 px): only the qualifiers and the hint are marked to go, the values stay whole (4D.4 r2)", () => {
    render(
      <StatusBar
        {...base}
        activeText="(3 ativos)"
        metrics={{
          ...base.metrics!,
          total_private_ram_bytes: 134_637_159,
          private_ram_provenance: "measured",
          cpu_usage_percent: 4.5,
          cpu_provenance: "measured",
        }}
      >
        <span className="status-bar__item status-bar__item--hint">F6 alternar regiões</span>
      </StatusBar>,
    );
    expect(bar()).toHaveTextContent("Terminais: 2 de 4 (3 ativos)");
    expect(bar()).toHaveTextContent("RAM da árvore: 128,4 MB (medida · privada)");
    expect(bar()).toHaveTextContent("CPU: 4,5% (medida)");
    const details = Array.from(bar().querySelectorAll(".status-bar__detail"));
    expect(details.map((d) => d.textContent)).toEqual([" (3 ativos)", " da árvore", " (medida · privada)", " (medida)"]);
    expect(bar().querySelectorAll(".status-bar__detail--value")).toHaveLength(3);
    for (const essential of ["Core v0.6.1 · conectado", "2 de 4", "128,4 MB", "4,5%"]) {
      expect(details.some((d) => d.textContent?.includes(essential))).toBe(false);
    }
    const items = Array.from(bar().querySelectorAll(".status-bar__group:not(.status-bar__group--end) .status-bar__item[title]")).map(
      (i) => i.getAttribute("title"),
    );
    expect(items).toEqual(["Terminais: 2 de 4 (3 ativos)", "RAM da árvore: 128,4 MB (medida · privada)", "CPU: 4,5% (medida)"]);
    expect(bar().querySelector(".status-bar__group--end .status-bar__item--hint")).toHaveTextContent("F6 alternar regiões");
  });

  it("splits a value from its qualifier only at a final parenthesis", () => {
    expect(splitQualifier("128,4 MB (medida · privada)")).toEqual({ main: "128,4 MB", detail: " (medida · privada)" });
    expect(splitQualifier("indisponível (preview)")).toEqual({ main: "indisponível", detail: " (preview)" });
    expect(splitQualifier("indisponível")).toEqual({ main: "indisponível", detail: "" });
    expect(splitQualifier("aguardando")).toEqual({ main: "aguardando", detail: "" });
    expect(splitQualifier("(medida)")).toEqual({ main: "(medida)", detail: "" });
  });

  it("formats bytes in base 1024 and refuses what is not a measurement", () => {
    expect(formatBytesPtBR(512 * 1024)).toBe("512,0 KB");
    expect(formatBytesPtBR(1.5 * 1024 * 1024 * 1024)).toBe("1,5 GB");
    expect(formatBytesPtBR(Number.NaN)).toBeNull();
    expect(formatBytesPtBR(-1)).toBeNull();
    expect(formatBytesPtBR(0)).toBeNull();
    expect(splitProjectPath("D:\\ai-coordinator\\")).toEqual({ head: "D:\\", tail: "ai-coordinator" });
    expect(splitProjectPath("projeto")).toEqual({ head: "", tail: "projeto" });
  });
});
