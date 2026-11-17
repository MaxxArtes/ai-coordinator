/**
 * Test-only stand-in for xterm 5.3 and its FitAddon (jsdom has no canvas and no layout).
 * The DOM it builds mirrors the real one: .xterm > .xterm-viewport + .xterm-screen > textarea.xterm-helper-textarea.
 */
export const xtermLog: { created: FakeTerminal[]; disposed: number } = { created: [], disposed: 0 };

export function resetXterm(): void {
  xtermLog.created.length = 0;
  xtermLog.disposed = 0;
}

export class FakeTerminal {
  cols = 80;
  rows = 24;
  options: Record<string, unknown>;
  element: HTMLElement | undefined;
  textarea: HTMLTextAreaElement | undefined;
  dataListeners: Array<(data: string) => void> = [];
  keyHandler: ((event: KeyboardEvent) => boolean) | undefined;
  written: string[] = [];
  disposed = false;

  constructor(options: Record<string, unknown> = {}) {
    this.options = { ...options };
    xtermLog.created.push(this);
  }

  loadAddon(): void {}

  open(parent: HTMLElement): void {
    const root = document.createElement("div");
    root.className = "xterm";
    const viewport = document.createElement("div");
    viewport.className = "xterm-viewport";
    const screen = document.createElement("div");
    screen.className = "xterm-screen";
    const textarea = document.createElement("textarea");
    textarea.className = "xterm-helper-textarea";
    textarea.setAttribute("aria-label", "Terminal input");
    screen.appendChild(textarea);
    root.append(viewport, screen);
    parent.appendChild(root);
    this.element = root;
    this.textarea = textarea;
    // Like xterm: a key press in the textarea goes through the custom handler, then becomes data for the shell.
    textarea.addEventListener("keydown", (event) => {
      if (this.keyHandler && this.keyHandler(event) === false) return;
      const data = KEY_DATA[event.key];
      if (data !== undefined) this.emit(event.shiftKey && event.key === "Tab" ? "\x1b[Z" : event.ctrlKey && event.key === "c" ? "\x03" : data);
    });
  }

  onData(fn: (data: string) => void): { dispose: () => void } {
    this.dataListeners.push(fn);
    return { dispose: () => (this.dataListeners = this.dataListeners.filter((f) => f !== fn)) };
  }

  onResize(): { dispose: () => void } {
    return { dispose: () => undefined };
  }

  attachCustomKeyEventHandler(fn: (event: KeyboardEvent) => boolean): void {
    this.keyHandler = fn;
  }

  write(data: string): void {
    this.written.push(data);
  }

  writeln(data: string): void {
    this.written.push(`${data}\r\n`);
  }

  focus(): void {
    this.textarea?.focus();
  }

  resize(cols: number, rows: number): void {
    this.cols = cols;
    this.rows = rows;
  }

  emit(data: string): void {
    for (const fn of this.dataListeners) fn(data);
  }

  dispose(): void {
    this.disposed = true;
    xtermLog.disposed++;
  }
}

const KEY_DATA: Record<string, string> = {
  Tab: "\t",
  Escape: "\x1b",
  Enter: "\r",
  c: "c",
};

export class FakeFitAddon {
  activate(): void {}
  dispose(): void {}
  fit(): void {}
  proposeDimensions(): { cols: number; rows: number } {
    return { cols: 80, rows: 24 };
  }
}

export class FakeResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
