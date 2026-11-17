/**
 * Terminal identity (Etapa 6, F10): free slot, stable number, executable name only.
 */
import { describe, expect, it } from "vitest";
import { formatTerminalNumber, lowestFreeSlot, paneFromSession, programName } from "./terminalIdentity";

describe("terminal identity", () => {
  it("the lowest free slot, never the number of panes", () => {
    expect(lowestFreeSlot([])).toBe(0);
    expect(lowestFreeSlot([{ paneId: 0 }, { paneId: 2 }])).toBe(1);
    expect(lowestFreeSlot([{ paneId: 1 }, { paneId: 0 }, { paneId: 3 }])).toBe(2);
    expect(lowestFreeSlot([{ paneId: 0 }, { paneId: 1 }, { paneId: 2 }, { paneId: 3 }])).toBeNull();
  });

  it("two-digit numbers", () => {
    expect(formatTerminalNumber(5)).toBe("05");
    expect(formatTerminalNumber(12)).toBe("12");
  });

  it("only the executable name, never the arguments", () => {
    const argv = ["C:\\x\\cmd.exe", "/k", "SEGREDO"];
    expect(programName(argv)).toBe("cmd.exe");
    expect(programName(["/usr/bin/bash"])).toBe("bash");
    expect(programName("cmd.exe /k SEGREDO")).toBeUndefined();
    expect(programName([])).toBeUndefined();
    const pane = paneFromSession({ session_id: "s", pane_id: 2, command: argv, cwd: "D:\\proj", pid: 77, is_alive: true }, 5, "tab", 0);
    expect(JSON.stringify(pane)).not.toContain("SEGREDO");
    expect(pane).toMatchObject({ sessionId: "s", paneId: 2, number: 5, program: "cmd.exe", cwd: "D:\\proj", pid: 77, startedAlive: true });
  });

  it("uses the slot the host reported, and keeps unknown values unknown", () => {
    const pane = paneFromSession({ session_id: "s", pid: 0, is_alive: false }, 1, "tab", 3);
    expect(pane.paneId).toBe(3);
    expect(pane.pid).toBeNull();
    expect(pane.program).toBeUndefined();
    expect(pane.startedAlive).toBe(false);
  });
});
