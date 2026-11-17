/**
 * Status bar (Etapa 5, decision 6): measured RAM, CPU unavailable, nothing invented in the preview.
 */
import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { formatBytesPtBR, splitProjectPath, StatusBar, type StatusBarProps } from "./StatusBar";

const base: StatusBarProps = {
  isTauri: true,
  connectionStatus: "connected",
  coreVersion: "0.6.0",
  paneCount: 2,
  metrics: { host_pid: 4242, host_ram_bytes: 61_000_000, total_tree_ram_bytes: 134_637_159, cpu_usage_percent: 37.5 },
  projectPath: "D:\\ai-coordinator",
};

const bar = () => screen.getByRole("contentinfo", { name: "Barra de status" });

describe("StatusBar", () => {
  it("Core, terminals and the measured RAM, in pt-BR", () => {
    render(<StatusBar {...base} />);
    expect(bar()).toHaveTextContent("Core v0.6.0 · conectado");
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
    expect(bar().textContent).not.toMatch(/conectado|0\.6\.0/);
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

  it("CPU is always 'indisponível', even when the host sends a number", () => {
    render(<StatusBar {...base} />);
    expect(bar()).toHaveTextContent("CPU: indisponível");
    expect(bar().textContent).not.toMatch(/37[,.]5/);
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
