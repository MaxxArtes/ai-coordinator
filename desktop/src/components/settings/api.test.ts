/**
 * Helpers of the settings API (Etapa 7): the appearance cached for the first paint, the error texts of the host
 * reasons (K3), the path comparison used with the host's (K4) and when a category may be said to be in effect (K1).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  applyInterfaceSettings,
  authorizeProject,
  categoryInEffect,
  deleteVaultSecret,
  errorText,
  getActiveProfile,
  pathWithin,
  restoreCachedAppearance,
  samePath,
  toStructuredError,
  type SettingsSnapshot,
} from "./api";
import { terminalPreferences } from "./terminalPreferences";

const root = document.documentElement;
const KEYS = ["stimulation", "density", "terminalFontSize", "terminalScrollback", "terminalCursorBlink"];
const clear = () => KEYS.forEach((key) => delete root.dataset[key]);

function snapshot(values: { aparencia?: Record<string, unknown>; terminal?: Record<string, unknown> }, top: Partial<SettingsSnapshot> = {}) {
  return {
    schema_version: 1,
    stored_schema_version: 1,
    unsupported_categories: {},
    categories: {
      aparencia: { values: values.aparencia ?? {}, saved_fields: [], validated: true, applied: false, application: "interface" },
      terminal: { values: values.terminal ?? {}, saved_fields: [], validated: true, applied: false, application: "interface" },
    },
    ...top,
  } as unknown as SettingsSnapshot;
}

afterEach(() => {
  clear();
  window.localStorage.clear();
});

describe("aparência da primeira pintura", () => {
  it("o que foi aplicado volta antes do primeiro quadro na próxima abertura", () => {
    applyInterfaceSettings(snapshot({ aparencia: { stimulation: "low", density: "compact" }, terminal: { font_size: 18, scrollback_lines: 400, cursor_blink: false } }));
    clear();
    restoreCachedAppearance();
    expect({ ...root.dataset }).toMatchObject({
      stimulation: "low",
      density: "compact",
      terminalFontSize: "18",
      terminalScrollback: "400",
      terminalCursorBlink: "false",
    });
  });

  it("os padrões aplicados (configurações não carregadas) também ficam guardados", () => {
    applyInterfaceSettings(snapshot({ aparencia: { stimulation: "low" } }));
    applyInterfaceSettings(null);
    clear();
    restoreCachedAppearance();
    expect(root.dataset.stimulation).toBeUndefined();
    expect(root.dataset.density).toBe("comfortable");
    expect(root.dataset.terminalFontSize).toBe("13");
  });

  it("um cache estranho ou ilegível é ignorado", () => {
    window.localStorage.setItem("ai-coordinator.appearance.v1", JSON.stringify({ stimulation: "neon", density: "x", terminalFontSize: "999", terminalCursorBlink: "talvez" }));
    restoreCachedAppearance();
    expect(Object.keys(root.dataset).filter((k) => KEYS.includes(k))).toEqual([]);
    window.localStorage.setItem("ai-coordinator.appearance.v1", "{não é json");
    expect(() => restoreCachedAppearance()).not.toThrow();
  });

  it("configurações protegidas (somente leitura): os padrões são aplicados, como a mensagem do host diz", () => {
    applyInterfaceSettings(
      snapshot(
        { aparencia: { stimulation: "low", density: "compact" }, terminal: { font_size: 18, scrollback_lines: 200, cursor_blink: false } },
        { read_only: true, stored_schema_version: 2 }
      )
    );
    expect(root.dataset.stimulation).toBeUndefined();
    expect(root.dataset.density).toBe("comfortable");
    expect(terminalPreferences()).toEqual({ fontSize: 13, scrollback: 1000, cursorBlink: true });
  });

  it("valores fora da faixa nunca chegam ao terminal", () => {
    applyInterfaceSettings(snapshot({ terminal: { font_size: 99, scrollback_lines: 5000, cursor_blink: "sim" } }));
    expect(terminalPreferences()).toEqual({ fontSize: 13, scrollback: 1000, cursorBlink: true });
  });
});

describe("textos de erro do host (K3)", () => {
  it("a mensagem do host vale; o texto da razão só substitui uma mensagem genérica", () => {
    expect(errorText({ code: -32602, message: "Feche os terminais.", data: { reason: "active_sessions" } })).toBe("Feche os terminais.");
    expect(errorText({ code: -32603, message: "Internal error: operation could not be completed", data: { reason: "busy" } })).toBe(
      "Outra janela está usando o perfil ou o cofre. Tente novamente."
    );
    expect(errorText({ code: -32603, message: "", data: { reason: "profile_unreadable" } })).toBe("O perfil não pôde ser lido. Os dados foram preservados.");
    expect(errorText("falha de transporte")).toBe("falha de transporte");
    expect(errorText(undefined)).toBe("Falha inesperada na comunicação com o Core.");
  });
});

describe("caminhos como o host compara no Windows (K4)", () => {
  it("separadores, maiúsculas e barra final não importam", () => {
    expect(samePath("D:\\Proj", "d:/proj/")).toBe(true);
    expect(samePath("D:\\proj", "D:\\proj2")).toBe(false);
    expect(samePath(null, "D:\\proj")).toBe(false);
  });

  it("um diretório dentro de outro conta; um prefixo de nome não", () => {
    expect(pathWithin("D:\\proj\\sub", "D:\\proj")).toBe(true);
    expect(pathWithin("D:\\proj", "D:\\PROJ")).toBe(true);
    expect(pathWithin("D:\\projeto", "D:\\proj")).toBe(false);
  });
});

describe("categoria em vigor (K1)", () => {
  it("só com o snapshot carregado, validado, gravável e sem campos inválidos", () => {
    expect(categoryInEffect(null, "terminal")).toBe(false);
    expect(categoryInEffect(snapshot({}), "terminal")).toBe(true);
    expect(categoryInEffect(snapshot({}, { read_only: true }), "terminal")).toBe(false);
    const invalid = snapshot({});
    invalid.categories.terminal = { ...invalid.categories.terminal, validated: false, invalid_fields: ["font_size"] };
    expect(categoryInEffect(invalid, "terminal")).toBe(false);
    expect(categoryInEffect(invalid, "aparencia")).toBe(true);
  });
});

describe("falhas de transporte do host (L2)", () => {
  const TIMEOUT = "O Core não respondeu em 30 s. A operação pode ter sido concluída: confira antes de repetir.";
  const CLOSED = "O Core encerrou a comunicação. Reinicie o aplicativo.";

  it("erro estruturado: a mensagem do host vale; a razão fica conhecida", () => {
    const timeout = { code: -32007, message: TIMEOUT, data: { field: "ipc", reason: "timeout" } };
    expect(errorText(timeout)).toBe(TIMEOUT);
    expect(toStructuredError(timeout)).toEqual({ code: -32007, message: TIMEOUT, field: "ipc", reason: "timeout" });
    const closed = { code: -32007, message: CLOSED, data: { field: "ipc", reason: "disconnected" } };
    expect(errorText(closed)).toBe(CLOSED);
    expect(toStructuredError(closed).reason).toBe("disconnected");
  });

  it("mensagem genérica com essas razões: o texto fixo diz o que fazer", () => {
    expect(errorText({ code: -32007, message: "", data: { reason: "timeout" } })).toBe(
      "O Core não respondeu a tempo. A operação pode ter sido concluída: confira antes de repetir."
    );
    expect(errorText({ code: -32007, message: "Internal error", data: { reason: "disconnected" } })).toBe(CLOSED);
  });

  it("comandos de texto (call): a mesma mensagem fixa é reconhecida, com qualquer prazo", () => {
    expect(toStructuredError(TIMEOUT)).toEqual({ code: -32007, message: TIMEOUT, field: "ipc", reason: "timeout" });
    expect(toStructuredError(TIMEOUT.replace("30 s", "120 s")).reason).toBe("timeout");
    expect(toStructuredError(CLOSED)).toEqual({ code: -32007, message: CLOSED, field: "ipc", reason: "disconnected" });
    expect(toStructuredError(new Error(CLOSED)).reason).toBe("disconnected");
    // Anything else keeps no reason: a message that only resembles it is not a transport failure.
    expect(toStructuredError("O Core não respondeu.")).toEqual({ code: -32603, message: "O Core não respondeu." });
    expect(errorText(TIMEOUT)).toBe(TIMEOUT);
  });
});

describe("contratos dos comandos (L3, L4, L7)", () => {
  it("authorizeProject só envia confirmBroad com o sim explícito", async () => {
    const callTauri = vi.fn(async () => ({ success: true, project_path: "C:\\", authorized_projects: ["C:\\"] }));
    await authorizeProject(callTauri, "C:\\");
    await authorizeProject(callTauri, "C:\\", true);
    expect(callTauri.mock.calls).toEqual([
      ["profile_authorize_project", { projectPath: "C:\\" }],
      ["profile_authorize_project", { projectPath: "C:\\", confirmBroad: true }],
    ]);
  });

  it("authorizeProject e getActiveProfile rejeitam com o erro estruturado do host", async () => {
    const broad = { code: -32602, message: "Este diretório dá acesso amplo.", data: { field: "project_path", reason: "broad_scope" } };
    await expect(authorizeProject(vi.fn(async () => Promise.reject(broad)), "C:\\")).rejects.toEqual({
      code: -32602,
      message: "Este diretório dá acesso amplo.",
      field: "project_path",
      reason: "broad_scope",
    });
    const busy = { code: -32603, message: "Outra janela está usando o perfil.", data: { reason: "busy" } };
    await expect(getActiveProfile(vi.fn(async () => Promise.reject(busy)))).rejects.toMatchObject({ reason: "busy" });
  });

  it("deleteVaultSecret leva a revisão quando há uma, e nenhuma chave extra sem ela", async () => {
    const callTauri = vi.fn(async () => ({ deleted: true, key: "K" }));
    await deleteVaultSecret(callTauri, "K", "a".repeat(64));
    await deleteVaultSecret(callTauri, "K");
    expect(callTauri.mock.calls).toEqual([
      ["vault_delete_secret", { key: "K", expectedRevision: "a".repeat(64) }],
      ["vault_delete_secret", { key: "K" }],
    ]);
  });
});
