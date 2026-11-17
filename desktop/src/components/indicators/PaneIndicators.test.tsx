import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PaneIndicators } from "./PaneIndicators";
import { Dialog } from "../ui/Dialog";
import { IndicatorRing } from "./IndicatorRing";
import { terminalReadings, type IndicatorReading } from "./readings";

afterEach(() => vi.useRealTimers());
const name = /^Indicadores do Terminal 05:/;
const known = (over: Partial<IndicatorReading> = {}): IndicatorReading => ({
  kind: "context", provenance: "measured", percent: 18, source: "provider", asOf: "2026-11-17T12:00:00Z", ...over,
});

describe("rings", () => {
  it("unknown has a dash, no percentage or numerical value, and no value band or arc", () => {
    const { container } = render(<IndicatorRing reading={terminalReadings()[0]} />);
    expect(container.textContent).toBe("ctx—");
    expect(container.textContent).not.toMatch(/\d|%/);
    expect(container.querySelector(".indicator-ring")).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector(".indicator-ring__arc")).toBeNull();
    expect(container.querySelector(".indicator-ring--low")).toBeNull();
  });
  it("normalizes invalid data at the display boundary", () => {
    const { container } = render(<IndicatorRing reading={known({ percent: NaN })} />);
    expect(container.textContent).toBe("ctx—");
  });
  it("estimated fixture has an approximate value, a dashed partial arc and fixture label", () => {
    const { container } = render(<IndicatorRing reading={known({ provenance: "estimated", origin: "test_fixture" })} />);
    expect(container.textContent).toBe("ctx≈18%fixture");
    const arc = container.querySelector("path")!;
    expect(arc).toHaveAttribute("stroke-dasharray", "2 1.5");
    expect(arc.getAttribute("d")).not.toContain("7 12.5 A"); // not a full-circle estimate
    expect(container.innerHTML).not.toMatch(/animation|animate|aria-live/);
  });
  it.each([0, 100])("estimated boundary %i has no invalid SVG arc", (percent) => {
    const { container } = render(<IndicatorRing reading={known({ provenance: "estimated", percent })} />);
    expect(container.textContent).toBe(`ctx≈${percent}%`);
    expect(container.querySelectorAll("path")).toHaveLength(percent === 0 ? 0 : 1);
  });
});

describe("pane explanation", () => {
  it("does not open on mount or focus; one button describes all three unavailable indicators", () => {
    const { container } = render(<PaneIndicators terminalName="Terminal 05" />);
    expect(screen.getAllByRole("button")).toHaveLength(1);
    const button = screen.getByRole("button", { name });
    button.focus();
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(container.textContent).not.toContain("%");
  });
  it.each(["{Enter}", " "])("opens with %s; Escape dismisses and returns focus without a terminal keystroke", async (key) => {
    const user = userEvent.setup();
    render(<PaneIndicators terminalName="Terminal 05" />);
    const button = screen.getByRole("button", { name });
    button.focus();
    await user.keyboard(key);
    const dialog = screen.getByRole("dialog", { name: "Indicadores do Terminal 05" });
    await waitFor(() => expect(dialog).toHaveFocus());
    expect(dialog).toHaveAttribute("aria-modal", "false");
    expect(dialog).toHaveAccessibleDescription(/indisponível.*4D\.4/);
    expect(button).toHaveAttribute("aria-controls", dialog.id);
    const shell = vi.fn();
    window.addEventListener("keydown", shell);
    await user.keyboard("{Escape}");
    window.removeEventListener("keydown", shell);
    expect(shell).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button).toHaveFocus();
  });
  it("the first mouse click pins a hovered explanation, the next dismisses it", async () => {
    const user = userEvent.setup();
    render(<PaneIndicators terminalName="Terminal 05" />);
    const button = screen.getByRole("button", { name });
    await user.click(button);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await user.click(button);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("hover is dismissible, hoverable and persistent over the popup", () => {
    vi.useFakeTimers();
    render(<PaneIndicators terminalName="Terminal 05" />);
    const button = screen.getByRole("button", { name });
    const root = button.parentElement!;
    fireEvent.mouseEnter(root);
    const dialog = screen.getByRole("dialog");
    fireEvent.mouseLeave(root);
    act(() => vi.advanceTimersByTime(100));
    fireEvent.mouseEnter(dialog);
    act(() => vi.advanceTimersByTime(1000));
    expect(dialog).toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button).toHaveFocus();
  });
  it("dismisses on an outside pointer and when keyboard focus leaves; never traps Tab", async () => {
    const user = userEvent.setup();
    render(<><PaneIndicators terminalName="Terminal 05" /><button>Outro</button></>);
    const button = screen.getByRole("button", { name });
    button.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveFocus());
    await user.tab();
    expect(screen.getByRole("button", { name: "Outro" })).toHaveFocus();
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(button);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("an open modal owns Escape even with a background hover explanation", async () => {
    const user = userEvent.setup();
    const closeModal = vi.fn();
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    const { rerender } = render(<PaneIndicators terminalName="Terminal 05" />, { container: root });
    fireEvent.mouseEnter(screen.getByRole("button", { name }).parentElement!);
    expect(screen.getByRole("dialog", { name: "Indicadores do Terminal 05" })).toBeInTheDocument();
    rerender(<><PaneIndicators terminalName="Terminal 05" /><Dialog open onClose={closeModal} title="Configurações">Conteúdo</Dialog></>);
    expect(root).toHaveAttribute("inert");
    await user.keyboard("{Escape}");
    expect(closeModal).toHaveBeenCalledTimes(1);
    expect(root.querySelector(".pane-indicators__popover")).toBeNull();
    expect(document.activeElement?.closest('[aria-modal="true"]')).not.toBeNull();
  });
  it("opening another pane explanation dismisses the first", () => {
    render(<><PaneIndicators terminalName="Terminal 05" /><PaneIndicators terminalName="Terminal 06" /></>);
    fireEvent.mouseEnter(screen.getByRole("button", { name }).parentElement!);
    fireEvent.mouseEnter(screen.getByRole("button", { name: /^Indicadores do Terminal 06:/ }).parentElement!);
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Indicadores do Terminal 06");
  });

  it("unmount clears the delayed hover dismissal", () => {
    vi.useFakeTimers();
    const { unmount } = render(<PaneIndicators terminalName="Terminal 05" />);
    const root = screen.getByRole("button", { name }).parentElement!;
    fireEvent.mouseEnter(root);
    fireEvent.mouseLeave(root);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
