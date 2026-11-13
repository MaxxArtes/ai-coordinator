import React, { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsDialog } from "./SettingsDialog";
import type { SettingsCategoryId } from "./categories";
import type { UpdateCheckResult } from "./UpdatesSection";
import type { RuntimeStatus } from "../shell/AppHeader";
import { applyInterfaceSettings, type SettingsSnapshot } from "./api";
import { CONFIRM_ARM_DELAY_MS, armingClock } from "./VaultPanel";

vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));

const RUNTIME: RuntimeStatus = {
  core_version: "0.6.0",
  python_version: "3.12.8",
  app_version: "0.6.1-alpha.50",
  license: "PolyForm-Noncommercial-1.0.0",
  host_os: "windows",
  host_arch: "x86_64",
  is_isolated: true,
  python_path: "C:\\App\\runtime\\python\\python.exe",
};

const UP_TO_DATE: UpdateCheckResult = {
  available: false,
  current_version: "0.6.1-alpha.50",
  latest_version: "0.6.1-alpha.50",
  notes: "",
  date: "",
  active_sessions_count: 0,
  sessions_known: true,
  offline: false,
  error: null,
  runtime_stopped: false,
};

const AVAILABLE: UpdateCheckResult = { ...UP_TO_DATE, available: true, latest_version: "0.6.1-alpha.51", notes: "Notas." };

type Handler = (args?: Record<string, any>) => unknown;

function makeCallTauri(handlers: Record<string, Handler>) {
  return vi.fn(async (cmd: string, args?: Record<string, any>) => {
    const handler = handlers[cmd];
    if (!handler) throw new Error(`comando Tauri inesperado: ${cmd}`);
    return handler(args);
  });
}

function snapshotWith(overrides: Partial<Record<string, any>> = {}): SettingsSnapshot {
  const base: any = {
    schema_version: 1,
    stored_schema_version: null,
    unsupported_categories: {},
    categories: {
      perfil: { values: { display_name: null }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
      geral: { values: { open_last_project_on_start: true }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
      aparencia: { values: { stimulation: "standard", density: "comfortable" }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
      terminal: { values: { font_size: 13, cursor_blink: true, scrollback_lines: 1000 }, saved_fields: [], validated: true, applied: false, application: "pending_ui" },
      coordinator: {
        values: { flight_budget: { max_steps: 50, max_elapsed_seconds: 3600, max_uncheckpointed_steps: 10, max_worker_calls: 15 } },
        saved_fields: [],
        validated: true,
        applied: false,
        application: "unsupported",
      },
    },
  };
  for (const [cat, value] of Object.entries(overrides)) base.categories[cat] = { ...base.categories[cat], ...value };
  return base as SettingsSnapshot;
}

function updaterHandlers(check: UpdateCheckResult): Record<string, Handler> {
  return {
    settings_get: () => snapshotWith(),
    vault_status: () => ({ available: false, backend: "unavailable" }),
    updater_get_channel_info: () => ({ current_version: "0.6.1-alpha.50", channel: "alpha" }),
    updater_check: () => check,
  };
}

function renderSettings(
  callTauri: ReturnType<typeof makeCallTauri>,
  { initialCategory = "sobre" as SettingsCategoryId, activePanesCount = 0, onClose = vi.fn() } = {}
) {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  render(
    <SettingsDialog
      open
      onClose={onClose}
      initialCategory={initialCategory}
      isTauri
      runtimeStatus={RUNTIME}
      connectionStatus="connected"
      activePanesCount={activePanesCount}
      callTauri={callTauri}
    />,
    { container: root }
  );
  return { onClose, user: userEvent.setup() };
}

const ORDER = ["Perfil", "Geral", "Aparência", "Terminal", "Coordinator", "Agentes", "Provedores", "Segurança", "Sobre"];

describe("SettingsDialog — estrutura e navegação", () => {
  beforeEach(() => vi.clearAllMocks());

  it("apresenta as nove categorias do roadmap na ordem aprovada", () => {
    renderSettings(makeCallTauri(updaterHandlers(UP_TO_DATE)), { initialCategory: "perfil" });
    expect(screen.getByRole("dialog", { name: "Configurações" })).toHaveAttribute("aria-modal", "true");
    const tabs = within(screen.getByRole("tablist", { name: "Categorias" })).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent?.replace("planejado", "").trim())).toEqual(ORDER);
  });

  it("navega pelas categorias com setas, Home e End e mantém só a aba ativa no Tab", async () => {
    const { user } = renderSettings(makeCallTauri(updaterHandlers(UP_TO_DATE)), { initialCategory: "perfil" });
    const perfil = screen.getByRole("tab", { name: /Perfil/ });
    expect(perfil).toHaveFocus();
    expect(perfil).toHaveAttribute("tabindex", "0");

    await user.keyboard("{ArrowDown}");
    const geral = screen.getByRole("tab", { name: /Geral/ });
    expect(geral).toHaveFocus();
    expect(geral).toHaveAttribute("aria-selected", "true");
    expect(perfil).toHaveAttribute("tabindex", "-1");
    expect(screen.getByRole("tabpanel", { name: /Geral/ })).toBeVisible();

    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: /Sobre/ })).toHaveFocus();
    await user.keyboard("{ArrowDown}"); // wraps to the first category
    expect(perfil).toHaveFocus();
    await user.keyboard("{ArrowUp}"); // wraps to the last category
    expect(screen.getByRole("tab", { name: /Sobre/ })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(perfil).toHaveFocus();
  });

  it("não apresenta como operacional nenhuma categoria ainda sem backend (D4)", async () => {
    const { user } = renderSettings(makeCallTauri(updaterHandlers(UP_TO_DATE)), { initialCategory: "perfil" });
    for (const label of ["Agentes", "Segurança"]) {
      await user.click(screen.getByRole("tab", { name: new RegExp(label) }));
      const panel = screen.getByRole("tabpanel", { name: new RegExp(label) });
      expect(within(panel).getByText("Ainda não configurável nesta versão experimental")).toBeInTheDocument();
      expect(within(panel).queryByRole("textbox")).not.toBeInTheDocument();
      expect(within(panel).queryByRole("checkbox")).not.toBeInTheDocument();
      expect(within(panel).queryByRole("button")).not.toBeInTheDocument();
    }
  });

  it("marca como não suportado o que exigiria mudar o Core ou um cofre inexistente (D8)", async () => {
    const { user } = renderSettings(makeCallTauri(updaterHandlers(UP_TO_DATE)), { initialCategory: "provedores" });
    const panel = screen.getByRole("tabpanel", { name: /Provedores/ });
    expect(await within(panel).findByText(/Armazenamento persistente de credenciais indisponível/)).toBeInTheDocument();
    expect(within(panel).queryByRole("textbox")).not.toBeInTheDocument();
    expect(panel.querySelector("input[type=password]")).toBeNull();
    await user.click(screen.getByRole("tab", { name: /Agentes/ }));
    expect(within(screen.getByRole("tabpanel", { name: /Agentes/ })).getByText("Gemini CLI")).toBeInTheDocument();
  });
});

describe("SettingsDialog — Sobre", () => {
  beforeEach(() => vi.clearAllMocks());

  it("mostra versões, licença e diagnóstico vindos do host", () => {
    renderSettings(makeCallTauri(updaterHandlers(UP_TO_DATE)));
    const panel = screen.getByRole("tabpanel", { name: /Sobre/ });
    expect(within(panel).getByText("0.6.1-alpha.50")).toBeInTheDocument();
    expect(within(panel).getByText("3.12.8")).toBeInTheDocument();
    expect(within(panel).getByText("PolyForm-Noncommercial-1.0.0")).toBeInTheDocument();
    expect(within(panel).getByText("windows / x86_64")).toBeInTheDocument();
    expect(within(panel).getByText("Privado e isolado")).toBeInTheDocument();
  });

  it("só consulta o atualizador quando Sobre está visível", async () => {
    const callTauri = makeCallTauri(updaterHandlers(UP_TO_DATE));
    const { user } = renderSettings(callTauri, { initialCategory: "perfil" });
    expect(callTauri).not.toHaveBeenCalledWith("updater_check");
    await user.click(screen.getByRole("tab", { name: /Sobre/ }));
    expect(await screen.findByText("O AI Coordinator Desktop está atualizado.")).toBeInTheDocument();
    expect(callTauri).toHaveBeenCalledWith("updater_check");
  });

  it("fecha com Esc quando nenhuma instalação está em andamento", async () => {
    const { onClose, user } = renderSettings(makeCallTauri(updaterHandlers(UP_TO_DATE)));
    await screen.findByText("O AI Coordinator Desktop está atualizado.");
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("exige autorização explícita para a contagem atual de sessões antes de instalar", async () => {
    const callTauri = makeCallTauri({
      ...updaterHandlers({ ...AVAILABLE, active_sessions_count: 2 }),
      updater_download_and_install: () => true,
    });
    const { user } = renderSettings(callTauri, { activePanesCount: 2 });

    const installButton = await screen.findByRole("button", { name: "Atualizar e reiniciar" });
    expect(installButton).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: /Autorizo o encerramento de todos os 2/ }));
    expect(installButton).toBeEnabled();
    await user.click(installButton);
    expect(callTauri).toHaveBeenCalledWith("updater_download_and_install", { force: true, expectedActiveSessions: 2 });
  });

  it("mantém a mensagem de uma falha recuperável depois da nova verificação", async () => {
    const callTauri = makeCallTauri({
      ...updaterHandlers(AVAILABLE),
      updater_download_and_install: () => {
        throw { code: "DOWNLOAD_OR_SIGNATURE_FAILED", message: "assinatura inválida", runtime_stopped: false };
      },
    });
    const { user } = renderSettings(callTauri);
    await user.click(await screen.findByRole("button", { name: "Atualizar e reiniciar" }));
    expect(await screen.findByText("Erro ao instalar atualização: assinatura inválida")).toBeInTheDocument();
    await waitFor(() => expect(callTauri.mock.calls.filter(([cmd]) => cmd === "updater_check")).toHaveLength(2));
    expect(screen.getByText("Erro ao instalar atualização: assinatura inválida")).toBeInTheDocument();
  });

  it("após falha pós-desligamento trava as Configurações e oferece apenas o reinício", async () => {
    const callTauri = makeCallTauri({
      ...updaterHandlers(AVAILABLE),
      updater_download_and_install: () => {
        throw { code: "INSTALL_FAILED_RUNTIME_STOPPED", message: "instalador não iniciou", runtime_stopped: true };
      },
      updater_restart_app: () => undefined,
    });
    const { onClose, user } = renderSettings(callTauri);
    await user.click(await screen.findByRole("button", { name: "Atualizar e reiniciar" }));
    expect(await screen.findByText(/Runtime Core encerrado/)).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Fechar configurações" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Adiar" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Atualizar e reiniciar" })).not.toBeInTheDocument();
    expect(callTauri.mock.calls.filter(([cmd]) => cmd === "updater_check")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Reiniciar aplicativo" }));
    expect(callTauri).toHaveBeenCalledWith("updater_restart_app");
  });

  it("mostra o reinício quando o host informa runtime parado na verificação", async () => {
    renderSettings(makeCallTauri(updaterHandlers({ ...UP_TO_DATE, runtime_stopped: true, sessions_known: false })));
    expect(await screen.findByText(/Runtime Core encerrado/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reiniciar aplicativo" })).toBeEnabled();
    expect(screen.queryByText("O AI Coordinator Desktop está atualizado.")).not.toBeInTheDocument();
  });
});

describe("SettingsDialog — configurações integradas (Etapa 4)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    delete document.documentElement.dataset.stimulation;
  });

  it("carrega os valores reais e não declara aplicado o que só tem padrão", async () => {
    renderSettings(makeCallTauri(updaterHandlers(UP_TO_DATE)), { initialCategory: "terminal" });
    const panel = screen.getByRole("tabpanel", { name: /Terminal/ });
    expect(await within(panel).findByDisplayValue("13")).toBeInTheDocument();
    expect(within(panel).getByText("Padrão (não salvo)")).toBeInTheDocument();
    expect(within(panel).getByText("Aplicação pendente")).toBeInTheDocument();
    expect(within(panel).queryByText("Aplicado")).not.toBeInTheDocument();
  });

  it("salva Aparência e só declara aplicado o modo de baixa estimulação, que é aplicado de fato", async () => {
    const saved = snapshotWith({
      aparencia: { values: { stimulation: "low", density: "comfortable" }, saved_fields: ["density", "stimulation"] },
    });
    const callTauri = makeCallTauri({ ...updaterHandlers(UP_TO_DATE), settings_update: () => saved });
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    render(
      <SettingsDialog
        open
        onClose={vi.fn()}
        initialCategory="aparencia"
        isTauri
        runtimeStatus={RUNTIME}
        connectionStatus="connected"
        activePanesCount={0}
        callTauri={callTauri}
        onSettingsChanged={(s) => applyInterfaceSettings(s)}
      />,
      { container: root }
    );
    const user = userEvent.setup();
    const panel = screen.getByRole("tabpanel", { name: /Aparência/ });
    await user.click(await within(panel).findByRole("radio", { name: /Baixa estimulação/ }));
    await user.click(within(panel).getByRole("button", { name: "Salvar" }));

    expect(callTauri).toHaveBeenCalledWith("settings_update", {
      schemaVersion: 1,
      category: "aparencia",
      values: { stimulation: "low", density: "comfortable" },
    });
    expect(await within(panel).findByText("Salvo")).toBeInTheDocument();
    expect(within(panel).getByText("Validado no backend")).toBeInTheDocument();
    expect(within(panel).getByText("Aplicação parcial")).toBeInTheDocument();
    expect(within(panel).getByText(/Aplicado na interface: estimulação visual\. Demais campos: aplicação pendente\./)).toBeInTheDocument();
    expect(document.documentElement.dataset.stimulation).toBe("low");
  });

  it("mostra o erro estruturado do backend no próprio campo e não marca nada como salvo", async () => {
    const callTauri = makeCallTauri({
      ...updaterHandlers(UP_TO_DATE),
      settings_update: () => {
        throw {
          code: -32602,
          message: "'terminal.font_size' deve estar entre 10 e 24.",
          data: { field: "terminal.font_size", reason: "invalid_value" },
        };
      },
    });
    const { user } = renderSettings(callTauri, { initialCategory: "terminal" });
    const panel = screen.getByRole("tabpanel", { name: /Terminal/ });
    const input = await within(panel).findByLabelText("Tamanho da fonte");
    await user.clear(input);
    await user.type(input, "99");
    await user.click(within(panel).getByRole("button", { name: "Salvar" }));

    expect(await within(panel).findByText("'terminal.font_size' deve estar entre 10 e 24.")).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(within(panel).getByText("Padrão (não salvo)")).toBeInTheDocument();
  });

  it("nunca apresenta o orçamento de missão como aplicado", async () => {
    const saved = snapshotWith({ coordinator: { saved_fields: ["flight_budget"] } });
    const callTauri = makeCallTauri({ ...updaterHandlers(UP_TO_DATE), settings_update: () => saved });
    const { user } = renderSettings(callTauri, { initialCategory: "coordinator" });
    const panel = screen.getByRole("tabpanel", { name: /Coordinator/ });
    await within(panel).findByDisplayValue("50");
    expect(within(panel).getByText("Não suportado nesta versão")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Salvar" }));
    expect(await within(panel).findByText(/Não aplicado: o desktop ainda não executa missões/)).toBeInTheDocument();
    expect(within(panel).queryByText("Aplicado")).not.toBeInTheDocument();
  });

  it("com cofre disponível grava a credencial sem jamais exibir o valor", async () => {
    const keys: string[] = [];
    const secret = "sk-test-0123456789-nao-exibir";
    const callTauri = makeCallTauri({
      ...updaterHandlers(UP_TO_DATE),
      vault_status: () => ({ available: true, backend: "dpapi" }),
      vault_list_keys: () => ({ keys: [...keys] }),
      vault_set_secret: (args) => {
        keys.push(args!.key);
        return { stored: true, key: args!.key };
      },
    });
    const { user } = renderSettings(callTauri, { initialCategory: "provedores" });
    const panel = screen.getByRole("tabpanel", { name: /Provedores/ });
    await user.type(await within(panel).findByLabelText(/Nome/), "OPENROUTER_API_KEY");
    await user.type(within(panel).getByLabelText("Valor"), secret);
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));

    expect(callTauri).toHaveBeenCalledWith("vault_set_secret", { key: "OPENROUTER_API_KEY", value: secret });
    expect(await within(panel).findByText(/Credencial OPENROUTER_API_KEY gravada no cofre/)).toBeInTheDocument();
    expect(within(panel).getByText("OPENROUTER_API_KEY")).toBeInTheDocument();
    expect(within(panel).getByLabelText("Valor")).toHaveValue("");
    expect(document.body.textContent).not.toContain(secret);
    expect(screen.queryByDisplayValue(secret)).not.toBeInTheDocument();
    expect(within(panel).getByText("Uso pelos provedores: não suportado nesta versão")).toBeInTheDocument();
  });
});

describe("SettingsDialog — confirmação no cofre (Provedores)", () => {
  // The clock is under the test's control: a pointer click only answers a confirmation after the arm delay.
  let now = 0;
  beforeEach(() => {
    vi.clearAllMocks();
    now = 5_000;
    vi.spyOn(armingClock, "now").mockImplementation(() => now);
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const SECRET = "sk-test-substituto-0123456789-nao-exibir";
  const passArmDelay = () => {
    now += CONFIRM_ARM_DELAY_MS + 1;
  };

  type User = ReturnType<typeof userEvent.setup>;
  type Calls = ReturnType<typeof makeCallTauri>;

  function vaultHandlers(keys: string[], overrides: Record<string, Handler> = {}): Record<string, Handler> {
    return {
      ...updaterHandlers(UP_TO_DATE),
      vault_status: () => ({ available: true, backend: "dpapi" }),
      vault_list_keys: () => ({ keys: [...keys].sort() }),
      vault_set_secret: (args) => {
        if (!keys.includes(args!.key)) keys.push(args!.key);
        return { stored: true, key: args!.key };
      },
      vault_delete_secret: (args) => {
        const i = keys.indexOf(args!.key);
        if (i >= 0) keys.splice(i, 1);
        return { deleted: i >= 0, key: args!.key };
      },
      ...overrides,
    };
  }

  /** A handler whose next call, once `gate.hold` is set, waits for `gate.release()` before answering. */
  function gated(produce: Handler) {
    const gate = { hold: false, held: false, release: () => {} };
    const handler: Handler = (args) => {
      if (!gate.hold) return produce(args);
      gate.hold = false;
      gate.held = true;
      return new Promise((resolve, reject) => {
        gate.release = () => {
          try {
            resolve(produce(args));
          } catch (err) {
            reject(err);
          }
        };
      });
    };
    return { gate, handler };
  }

  const calls = (callTauri: Calls, cmd: string) => callTauri.mock.calls.filter(([c]) => c === cmd);

  async function fill(user: User, panel: HTMLElement, name: string, value = SECRET) {
    await user.click(await within(panel).findByLabelText(/Nome/));
    await user.paste(name);
    if (value) {
      await user.click(within(panel).getByLabelText("Valor"));
      await user.paste(value);
    }
  }

  async function submit(user: User, panel: HTMLElement, name: string, value = SECRET) {
    await fill(user, panel, name, value);
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));
  }

  /** While typed, the value may exist only inside the password field: not as text, title, label or data attribute. */
  function expectSecretOnlyInItsField() {
    const clone = document.body.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("input[type=password]").forEach((el) => el.remove());
    expect(clone.innerHTML).not.toContain(SECRET);
  }

  const expectSecretGone = () => expect(document.body.innerHTML).not.toContain(SECRET);

  function openProvedores(callTauri: Calls) {
    const rendered = renderSettings(callTauri, { initialCategory: "provedores" });
    return { ...rendered, panel: screen.getByRole("tabpanel", { name: /Provedores/ }) };
  }

  it("pede confirmação antes de substituir uma credencial existente e só grava depois dela", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");

    const confirmation = await within(panel).findByRole("alertdialog", { name: "Substituir a credencial OPENROUTER_API_KEY?" });
    expect(confirmation).toHaveAccessibleDescription(/não poderá ser recuperado/);
    expect(within(confirmation).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
    expectSecretOnlyInItsField();
    // Nothing else can run or change while the operator decides.
    expect(within(panel).getByRole("button", { name: "Gravar no cofre" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" })).toBeDisabled();
    expect(within(panel).getByLabelText(/Nome/)).toBeDisabled();
    expect(within(panel).getByLabelText("Valor")).toBeDisabled();

    passArmDelay();
    await user.click(within(confirmation).getByRole("button", { name: "Substituir credencial" }));
    expect(callTauri).toHaveBeenCalledWith("vault_set_secret", { key: "OPENROUTER_API_KEY", value: SECRET });
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(1);
    expect(await within(panel).findByText(/Credencial OPENROUTER_API_KEY substituída no cofre/)).toBeInTheDocument();
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(within(panel).getByLabelText("Valor")).toHaveValue("");
    await waitFor(() => expect(within(panel).getByLabelText(/Nome/)).toHaveFocus());
    expectSecretGone();
  });

  it("cancelar a substituição não grava nada, mantém o que foi digitado e devolve o foco ao botão Gravar", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel, onClose } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");
    const confirmation = await within(panel).findByRole("alertdialog");

    await user.click(within(confirmation).getByRole("button", { name: "Cancelar" }));
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(within(panel).getByRole("button", { name: "Gravar no cofre" })).toHaveFocus());
    // Deliberate: the form keeps name and value, so the name can be corrected without retyping the credential.
    expect(within(panel).getByLabelText(/Nome/)).toHaveValue("OPENROUTER_API_KEY");
    expect(within(panel).getByLabelText("Valor")).toHaveValue(SECRET);
    expectSecretOnlyInItsField();
    expect(onClose).not.toHaveBeenCalled();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
    expect(keys).toEqual(["OPENROUTER_API_KEY"]);
  });

  it("Esc responde à confirmação de qualquer ponto de Configurações, sem fechar o diálogo", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel, onClose } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");
    const send = within(panel).getByRole("button", { name: "Gravar no cofre" });

    // Focus inside the confirmation, on the dialog's close button and on the category tab.
    const places: Array<() => HTMLElement> = [
      () => within(panel).getByRole("button", { name: "Substituir credencial" }),
      () => within(screen.getByRole("dialog", { name: "Configurações" })).getAllByRole("button", { name: /Fechar/ })[0],
      () => screen.getByRole("tab", { name: /Provedores/ }),
    ];
    for (const place of places) {
      await within(panel).findByRole("alertdialog");
      place().focus();
      await user.keyboard("{Escape}");
      expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
      expect(onClose).not.toHaveBeenCalled();
      await waitFor(() => expect(send).toHaveFocus());
      await user.click(send);
    }
    // A held Escape answers the question and stops there: its repeats do not go on to close Configurações.
    await within(panel).findByRole("alertdialog");
    await user.keyboard("{Escape>4/}");
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);

    // With no question open, Escape goes back to closing Configurações.
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("decide pela lista atual do cofre, não pela exibida, e não pede confirmação para um nome novo", async () => {
    const keys: string[] = [];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    expect(await within(panel).findByText("Nenhuma credencial gravada.")).toBeInTheDocument();

    keys.push("GEMINI_API_KEY"); // written elsewhere after the list was shown
    await submit(user, panel, "GEMINI_API_KEY");
    expect(await within(panel).findByRole("alertdialog", { name: "Substituir a credencial GEMINI_API_KEY?" })).toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
    await user.click(within(panel).getByRole("button", { name: "Cancelar" }));

    await user.clear(within(panel).getByLabelText(/Nome/));
    await user.paste("OPENAI_API_KEY");
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));
    expect(await within(panel).findByText(/Credencial OPENAI_API_KEY gravada no cofre/)).toBeInTheDocument();
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(callTauri).toHaveBeenCalledWith("vault_set_secret", { key: "OPENAI_API_KEY", value: SECRET });
    // The form is clear and ready for the next credential; the focus did not fall out of Configurações.
    await waitFor(() => expect(within(panel).getByLabelText(/Nome/)).toHaveFocus());
    expectSecretGone();
  });

  it("compara o nome sem os espaços em volta", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "  OPENROUTER_API_KEY ");

    const confirmation = await within(panel).findByRole("alertdialog", { name: "Substituir a credencial OPENROUTER_API_KEY?" });
    passArmDelay();
    await user.click(within(confirmation).getByRole("button", { name: "Substituir credencial" }));
    expect(callTauri).toHaveBeenCalledWith("vault_set_secret", { key: "OPENROUTER_API_KEY", value: SECRET });
  });

  it("não grava quando não consegue conferir a lista do cofre antes", async () => {
    const state = { fail: false };
    const callTauri = makeCallTauri(
      vaultHandlers([], {
        vault_list_keys: () => {
          if (state.fail) throw { code: -32010, message: "Cofre indisponível.", data: { field: "vault", reason: "unavailable" } };
          return { keys: [] };
        },
      })
    );
    const { user, panel } = openProvedores(callTauri);
    await fill(user, panel, "OPENROUTER_API_KEY");
    state.fail = true;
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));

    expect(await within(panel).findByText("Cofre indisponível.")).toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(within(panel).getByRole("button", { name: "Gravar no cofre" })).toHaveFocus());
    expectSecretOnlyInItsField();
  });

  it("não pergunta nem chama o cofre quando o valor está vazio, e ignora um envio sem nada digitado", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await within(panel).findByText("OPENROUTER_API_KEY");
    const listed = calls(callTauri, "vault_list_keys").length;

    await submit(user, panel, "OPENROUTER_API_KEY", "");
    const valor = within(panel).getByLabelText("Valor");
    expect(await within(panel).findByText("Informe o valor da credencial.")).toBeInTheDocument();
    expect(valor).toHaveAttribute("aria-invalid", "true");
    await waitFor(() => expect(valor).toHaveFocus());
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();

    await user.clear(within(panel).getByLabelText(/Nome/));
    await user.keyboard("{Enter}"); // nothing typed at all: nothing is sent and nothing on screen changes
    expect(within(panel).getByText("Informe o valor da credencial.")).toBeInTheDocument();
    expect(calls(callTauri, "vault_list_keys")).toHaveLength(listed);
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
  });

  it("grava exatamente o que foi enviado: os campos ficam travados enquanto confere e enquanto pergunta", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const list = gated(() => ({ keys: [...keys].sort() }));
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_list_keys: list.handler }));
    const { user, panel } = openProvedores(callTauri);
    const nome = await within(panel).findByLabelText(/Nome/);
    const valor = within(panel).getByLabelText("Valor");

    // Existing name: the question and the write are about the pair that was submitted.
    await fill(user, panel, "OPENROUTER_API_KEY");
    list.gate.hold = true;
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));
    await within(panel).findByRole("button", { name: "Conferindo o cofre…" });
    expect(nome).toHaveAttribute("readonly");
    expect(valor).toHaveAttribute("readonly");
    await user.type(nome, "_OUTRA");
    await user.type(valor, "-outro-valor");
    expect(nome).toHaveValue("OPENROUTER_API_KEY");
    expect(valor).toHaveValue(SECRET);
    await act(async () => list.gate.release());
    const confirmation = await within(panel).findByRole("alertdialog", { name: "Substituir a credencial OPENROUTER_API_KEY?" });
    passArmDelay();
    await user.click(within(confirmation).getByRole("button", { name: "Substituir credencial" }));
    await within(panel).findByText(/Credencial OPENROUTER_API_KEY substituída no cofre/);
    expect(calls(callTauri, "vault_set_secret")).toEqual([["vault_set_secret", { key: "OPENROUTER_API_KEY", value: SECRET }]]);

    // New name: same rule, there is just no question.
    await fill(user, panel, "OPENAI_API_KEY");
    list.gate.hold = true;
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));
    await within(panel).findByRole("button", { name: "Conferindo o cofre…" });
    await user.type(valor, "-outro-valor");
    expect(valor).toHaveValue(SECRET);
    await act(async () => list.gate.release());
    await within(panel).findByText(/Credencial OPENAI_API_KEY gravada no cofre/);
    expect(calls(callTauri, "vault_set_secret")[1]).toEqual(["vault_set_secret", { key: "OPENAI_API_KEY", value: SECRET }]);
  });

  it("sair do painel durante a conferência cancela o envio: nada é perguntado nem gravado", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const list = gated(() => ({ keys: [...keys].sort() }));
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_list_keys: list.handler }));
    const { user, panel } = openProvedores(callTauri);

    for (const name of ["OPENROUTER_API_KEY", "OPENAI_API_KEY"]) {
      await fill(user, panel, name);
      list.gate.hold = true;
      await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));
      await within(panel).findByRole("button", { name: "Conferindo o cofre…" });
      await user.click(screen.getByRole("tab", { name: /Perfil/ }));
      await act(async () => list.gate.release());

      expect(within(panel).queryByRole("alertdialog", { hidden: true })).not.toBeInTheDocument();
      await user.click(screen.getByRole("tab", { name: /Provedores/ }));
      await within(panel).findByRole("button", { name: "Gravar no cofre" });
      expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
      expect(within(panel).queryByText(/gravada no cofre|substituída no cofre/)).not.toBeInTheDocument();
      // Said in so many words, instead of leaving the operator to work it out from the list.
      expect(within(panel).getByText("Envio cancelado ao sair do painel: nada foi gravado no cofre.")).toHaveAttribute("role", "status");
      expect(within(panel).getByLabelText("Valor")).toHaveValue("");
      await user.clear(within(panel).getByLabelText(/Nome/));
    }
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
    expect(keys).toEqual(["OPENROUTER_API_KEY"]);
  });

  it("ao sair do painel descarta na hora a confirmação pendente e o valor digitado", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");
    await within(panel).findByRole("alertdialog");

    await user.click(screen.getByRole("tab", { name: /Perfil/ }));
    // Checked while the panel is hidden, not only after coming back.
    expect(panel).not.toBeVisible();
    expect(within(panel).queryByRole("alertdialog", { hidden: true })).not.toBeInTheDocument();
    expect(panel.querySelector<HTMLInputElement>("input[type=password]")!.value).toBe("");
    expectSecretGone();

    await user.click(screen.getByRole("tab", { name: /Provedores/ }));
    await waitFor(() => expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(within(panel).getByLabelText("Valor")).toHaveValue("");
    expect(within(panel).getByLabelText("Valor")).toBeEnabled();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
  });

  it("se a substituição confirmada falha, mostra o erro estruturado no campo e não declara nada gravado", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(
      vaultHandlers(keys, {
        vault_set_secret: () => {
          throw { code: -32602, message: "'value' deve ser um texto não vazio.", data: { field: "value", reason: "invalid_value" } };
        },
      })
    );
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");
    passArmDelay();
    await user.click(await within(panel).findByRole("button", { name: "Substituir credencial" }));

    const valor = within(panel).getByLabelText("Valor");
    expect(await within(panel).findByText("'value' deve ser um texto não vazio.")).toBeInTheDocument();
    expect(valor).toHaveAttribute("aria-invalid", "true");
    await waitFor(() => expect(valor).toHaveFocus());
    expect(within(panel).queryByText(/substituída no cofre/)).not.toBeInTheDocument();
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(1);
    expectSecretOnlyInItsField();
  });

  it("o resultado de uma substituição que termina com o painel oculto continua visível ao voltar", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const write = gated(() => {
      throw { code: -32603, message: "Falha ao gravar o cofre.", data: { field: "vault", reason: "io" } };
    });
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_set_secret: write.handler }));
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");
    const confirm = await within(panel).findByRole("button", { name: "Substituir credencial" });
    write.gate.hold = true;
    passArmDelay();
    await user.click(confirm);
    // While the write is in flight the focus is parked inside Configurações and the progress is announced.
    expect(await within(panel).findByText("Gravando a credencial OPENROUTER_API_KEY…")).toHaveAttribute("role", "status");
    expect(document.activeElement).not.toBe(document.body);

    await user.click(screen.getByRole("tab", { name: /Perfil/ }));
    await act(async () => write.gate.release());
    await user.click(screen.getByRole("tab", { name: /Provedores/ }));
    await waitFor(() => expect(calls(callTauri, "vault_list_keys").length).toBeGreaterThanOrEqual(3));
    expect(await within(panel).findByText("Falha ao gravar o cofre.")).toBeInTheDocument();
    expect(within(panel).queryByText(/substituída no cofre/)).not.toBeInTheDocument();
  });

  it("se a gravação dá certo mas a lista não pode ser relida, mostra o que foi gravado e nenhum erro", async () => {
    const keys: string[] = [];
    const state = { fail: false };
    const callTauri = makeCallTauri(
      vaultHandlers(keys, {
        vault_list_keys: () => {
          if (state.fail) throw { code: -32010, message: "Cofre indisponível.", data: { field: "vault", reason: "unavailable" } };
          return { keys: [...keys].sort() };
        },
        vault_set_secret: (args) => {
          keys.push(args!.key);
          state.fail = true;
          return { stored: true, key: args!.key };
        },
      })
    );
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "OPENAI_API_KEY");

    expect(await within(panel).findByText(/Credencial OPENAI_API_KEY gravada no cofre/)).toBeInTheDocument();
    expect(within(panel).getByText("OPENAI_API_KEY")).toBeInTheDocument();
    expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
    expect(within(panel).queryByText("Cofre indisponível.")).not.toBeInTheDocument();
  });

  it("pede confirmação antes de remover, junto da credencial; cancelar (inclusive com Esc) não remove e devolve o foco", async () => {
    const keys = ["GEMINI_API_KEY", "OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel, onClose } = openProvedores(callTauri);
    const remove = await within(panel).findByRole("button", { name: "Remover a credencial GEMINI_API_KEY" });

    await user.click(remove);
    const confirmation = within(panel).getByRole("alertdialog", { name: "Remover a credencial GEMINI_API_KEY?" });
    expect(confirmation).toHaveAccessibleDescription(/não poderá ser recuperado/);
    // The question is inside the row of the credential it is about, not after the whole list.
    expect(remove.closest("li")).toContainElement(confirmation);
    expect(within(remove.closest("li")!).getByText("GEMINI_API_KEY")).toBeVisible();
    expect(within(confirmation).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    expect(within(panel).getByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Gravar no cofre" })).toBeDisabled();

    await user.click(within(confirmation).getByRole("button", { name: "Cancelar" }));
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(remove).toHaveFocus());

    await user.click(remove);
    expect(within(panel).getByRole("alertdialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    await waitFor(() => expect(remove).toHaveFocus());
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(0);
    expect(keys).toEqual(["GEMINI_API_KEY", "OPENROUTER_API_KEY"]);
  });

  it("remove só depois da confirmação, anuncia o andamento e mantém o foco dentro de Configurações", async () => {
    const keys = ["GEMINI_API_KEY", "OPENROUTER_API_KEY"];
    const remove = gated(() => {
      keys.splice(keys.indexOf("OPENROUTER_API_KEY"), 1);
      return { deleted: true, key: "OPENROUTER_API_KEY" };
    });
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_delete_secret: remove.handler }));
    const { user, panel } = openProvedores(callTauri);

    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    const confirmation = within(panel).getByRole("alertdialog", { name: "Remover a credencial OPENROUTER_API_KEY?" });
    remove.gate.hold = true;
    passArmDelay();
    await user.click(within(confirmation).getByRole("button", { name: "Remover credencial" }));

    expect(await within(panel).findByText("Removendo a credencial OPENROUTER_API_KEY…")).toHaveAttribute("role", "status");
    expect(within(panel).getByRole("heading", { name: "Credenciais no cofre" })).toHaveFocus();
    expect(within(panel).queryByText(/removida do cofre/)).not.toBeInTheDocument();
    await act(async () => remove.gate.release());

    expect(callTauri).toHaveBeenCalledWith("vault_delete_secret", { key: "OPENROUTER_API_KEY" });
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(1);
    expect(await within(panel).findByText("Credencial OPENROUTER_API_KEY removida do cofre.")).toHaveAttribute("role", "status");
    expect(within(panel).queryByText("OPENROUTER_API_KEY")).not.toBeInTheDocument();
    expect(within(panel).getByText("GEMINI_API_KEY")).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: "Credenciais no cofre" })).toHaveFocus();
  });

  it("se a remoção confirmada falha, não declara nada removido e não marca o formulário de gravação", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(
      vaultHandlers(keys, {
        vault_delete_secret: () => {
          throw { code: -32602, message: "'key' deve ser um identificador em maiúsculas.", data: { field: "key", reason: "invalid_value" } };
        },
      })
    );
    const { user, panel } = openProvedores(callTauri);
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    passArmDelay();
    await user.click(within(panel).getByRole("button", { name: "Remover credencial" }));

    const alert = await within(panel).findByRole("alert");
    expect(alert).toHaveTextContent("Não foi possível remover a credencial OPENROUTER_API_KEY.");
    expect(alert).toHaveTextContent("'key' deve ser um identificador em maiúsculas.");
    // Next to the list it is about, not at the end of the panel (out of view when the list is long).
    expect(within(panel).getByRole("heading", { name: "Credenciais no cofre" }).closest("section")).toContainElement(alert);
    expect(within(panel).queryByText(/removida do cofre/)).not.toBeInTheDocument();
    expect(within(panel).getByText("OPENROUTER_API_KEY")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" })).toBeEnabled();
    // The error belongs to the removal, not to the unrelated (and empty) save form below.
    expect(within(panel).getByLabelText(/Nome/)).not.toHaveAttribute("aria-invalid");
    expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(1);
  });

  it("informa quando a credencial confirmada para remoção já não estava no cofre", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));

    keys.length = 0; // removed elsewhere while the confirmation was open
    passArmDelay();
    await user.click(within(panel).getByRole("button", { name: "Remover credencial" }));
    expect(
      await within(panel).findByText("A credencial OPENROUTER_API_KEY não estava mais no cofre; nada foi removido.")
    ).toBeInTheDocument();
    expect(within(panel).queryByText(/removida do cofre/)).not.toBeInTheDocument();
    expect(await within(panel).findByText("Nenhuma credencial gravada.")).toBeInTheDocument();
  });

  it("um clique de ponteiro que pertence ao gesto que abriu a pergunta não a responde", async () => {
    const keys = ["GEMINI_API_KEY", "OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);

    // Removal: second click of a double click, then a single click that lands before the question can be read.
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    const removeAnswer = within(panel).getByRole("button", { name: "Remover credencial" });
    fireEvent.click(removeAnswer, { detail: 2 });
    fireEvent.click(removeAnswer, { detail: 1 });
    expect(within(panel).getByRole("alertdialog")).toBeInTheDocument();
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(0);
    passArmDelay();
    fireEvent.click(removeAnswer, { detail: 2 }); // still a double click: never an answer
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(0);
    await user.click(within(panel).getByRole("button", { name: "Cancelar" }));

    // Replacement: same protection.
    await submit(user, panel, "GEMINI_API_KEY");
    const replaceAnswer = await within(panel).findByRole("button", { name: "Substituir credencial" });
    fireEvent.click(replaceAnswer, { detail: 1 });
    expect(within(panel).getByRole("alertdialog")).toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
    passArmDelay();
    fireEvent.click(replaceAnswer, { detail: 1 });
    expect(await within(panel).findByText(/Credencial GEMINI_API_KEY substituída no cofre/)).toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(1);
  });

  it("enquanto confere o cofre não declara que está gravando, não grava e mantém o foco no botão", async () => {
    const keys: string[] = [];
    const list = gated(() => ({ keys: [...keys].sort() }));
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_list_keys: list.handler }));
    const { user, panel } = openProvedores(callTauri);
    await fill(user, panel, "OPENAI_API_KEY");
    list.gate.hold = true;
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));

    const checking = await within(panel).findByRole("button", { name: "Conferindo o cofre…" });
    // Busy without `disabled`: the keyboard focus stays on the button instead of falling to the document.
    expect(checking).toHaveAttribute("aria-disabled", "true");
    expect(checking).toBeEnabled();
    expect(checking).toHaveFocus();
    expect(within(panel).getByRole("status")).toHaveTextContent("Conferindo o cofre…");
    expect(within(panel).queryByRole("button", { name: "Gravando…" })).not.toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);

    await user.click(checking); // a second submit while busy is refused
    await act(async () => list.gate.release());
    expect(await within(panel).findByText(/Credencial OPENAI_API_KEY gravada no cofre/)).toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(1);
  });

  it("Enter repetido não percorre o painel: uma substituição só, e o aviso de sucesso permanece", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");
    await within(panel).findByRole("alertdialog");

    await user.tab(); // from Cancelar to the destructive answer, by keyboard
    expect(within(panel).getByRole("button", { name: "Substituir credencial" })).toHaveFocus();
    await user.keyboard("{Enter>6/}"); // key held down: one real press plus auto-repeat
    expect(await within(panel).findByText(/Credencial OPENROUTER_API_KEY substituída no cofre/)).toBeInTheDocument();
    await waitFor(() => expect(within(panel).getByLabelText(/Nome/)).toHaveFocus());

    await user.keyboard("{Enter}"); // a second real press, now on the empty form
    await user.keyboard("{Enter}");
    expect(within(panel).getByText(/Credencial OPENROUTER_API_KEY substituída no cofre/)).toBeInTheDocument();
    expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(1);
  });

  it("Enter mantido pressionado em um campo abre a pergunta uma única vez e não a responde", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await fill(user, panel, "OPENROUTER_API_KEY");
    const listed = calls(callTauri, "vault_list_keys").length;

    // The focus is on Valor: one real press submits, the auto-repeat must not cancel and resubmit in a loop.
    await user.keyboard("{Enter>8/}");
    const confirmation = await within(panel).findByRole("alertdialog");
    expect(within(confirmation).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    expect(calls(callTauri, "vault_list_keys")).toHaveLength(listed + 1);
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
  });

  it("uma ativação dupla da confirmação executa a operação uma única vez", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    const confirm = within(panel).getByRole("button", { name: "Remover credencial" });

    // Two activations before React renders again: the button must still be mounted and enabled for the second.
    act(() => {
      fireEvent.click(confirm);
      expect(confirm.isConnected).toBe(true);
      expect(confirm).toBeEnabled();
      fireEvent.click(confirm);
    });
    expect(await within(panel).findByText("Credencial OPENROUTER_API_KEY removida do cofre.")).toBeInTheDocument();
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(1);
  });

  it("cancelar e confirmar no mesmo instante não executa a operação", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    const cancel = within(panel).getByRole("button", { name: "Cancelar" });
    const confirm = within(panel).getByRole("button", { name: "Remover credencial" });

    act(() => {
      fireEvent.click(cancel);
      expect(confirm.isConnected).toBe(true);
      fireEvent.click(confirm);
    });
    await waitFor(() => expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(0);
    expect(keys).toEqual(["OPENROUTER_API_KEY"]);
  });

  it("um envio duplo do formulário confere e grava uma única vez", async () => {
    const keys: string[] = [];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await fill(user, panel, "OPENAI_API_KEY");
    const form = within(panel).getByRole("form", { name: "Gravar credencial" });
    const listed = calls(callTauri, "vault_list_keys").length;

    act(() => {
      fireEvent.submit(form);
      expect(within(panel).getByRole("button", { name: "Gravar no cofre" })).toBeEnabled();
      fireEvent.submit(form);
    });
    expect(await within(panel).findByText(/Credencial OPENAI_API_KEY gravada no cofre/)).toBeInTheDocument();
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(1);
    // One check before writing and one reload after it; not two of each.
    expect(calls(callTauri, "vault_list_keys")).toHaveLength(listed + 2);
    expect(keys).toEqual(["OPENAI_API_KEY"]);
  });

  it("insistir no botão já armado responde, e o relógio do sistema não interfere", async () => {
    const keys = ["GEMINI_API_KEY", "OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    const answer = within(panel).getByRole("button", { name: "Remover credencial" });
    // Until the delay has passed, the destructive answer shows it is not available to the pointer yet.
    expect(answer).toHaveClass("btn--arming");
    await waitFor(() => expect(answer).not.toHaveClass("btn--arming"), { timeout: 3000 });

    // Someone who starts clicking too early and keeps clicking: the system counts one long multi-click.
    vi.spyOn(Date, "now").mockReturnValue(0); // a wall clock that was set back must not matter
    fireEvent.click(answer, { detail: 1 });
    now += CONFIRM_ARM_DELAY_MS / 2;
    fireEvent.click(answer, { detail: 2 });
    passArmDelay();
    fireEvent.click(answer, { detail: 3 }); // armed now, but the click before it was not: still the same gesture
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(0);
    expect(within(panel).getByRole("alertdialog")).toBeInTheDocument();
    now += 200;
    fireEvent.click(answer, { detail: 4 }); // insisting on the armed button
    expect(await within(panel).findByText("Credencial OPENROUTER_API_KEY removida do cofre.")).toBeInTheDocument();
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(1);
  });

  it("fechar Configurações durante a conferência não grava nada", async () => {
    const keys: string[] = [];
    const list = gated(() => ({ keys: [...keys].sort() }));
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_list_keys: list.handler }));
    const Host: React.FC = () => {
      const [open, setOpen] = useState(true);
      return (
        <SettingsDialog
          open={open}
          onClose={() => setOpen(false)}
          initialCategory="provedores"
          isTauri
          runtimeStatus={RUNTIME}
          connectionStatus="connected"
          activePanesCount={0}
          callTauri={callTauri}
        />
      );
    };
    const root = document.createElement("div");
    root.id = "root";
    document.body.appendChild(root);
    render(<Host />, { container: root });
    const user = userEvent.setup();
    const panel = screen.getByRole("tabpanel", { name: /Provedores/ });

    await fill(user, panel, "OPENAI_API_KEY");
    list.gate.hold = true;
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));
    await within(panel).findByRole("button", { name: "Conferindo o cofre…" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => list.gate.release());
    await act(async () => {});
    expect(calls(callTauri, "vault_set_secret")).toHaveLength(0);
    expect(keys).toEqual([]);
  });

  it("Enter mantido pressionado no botão Remover abre a pergunta uma única vez e não a responde", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    (await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" })).focus();

    for (const presses of [7, 8]) {
      await user.keyboard(`{Enter>${presses}/}`); // one real press plus auto-repeat, odd and even counts
      const confirmation = within(panel).getByRole("alertdialog");
      expect(within(confirmation).getByRole("button", { name: "Cancelar" })).toHaveFocus();
      expect(calls(callTauri, "vault_delete_secret")).toHaveLength(0);
      await user.keyboard("{Escape}");
      await waitFor(() => expect(within(panel).getByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" })).toHaveFocus());
    }
    expect(keys).toEqual(["OPENROUTER_API_KEY"]);
  });

  it("grava o par enviado mesmo que os campos mudem por fora com a pergunta aberta", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const callTauri = makeCallTauri(vaultHandlers(keys));
    const { user, panel } = openProvedores(callTauri);
    await submit(user, panel, "OPENROUTER_API_KEY");
    const confirmation = await within(panel).findByRole("alertdialog");

    // Not reachable by typing (the fields are disabled): something changing the fields directly, like an autofill.
    fireEvent.change(within(panel).getByLabelText("Valor"), { target: { value: "outro-valor-nao-enviado" } });
    fireEvent.change(within(panel).getByLabelText(/Nome/), { target: { value: "OUTRO_NOME" } });
    passArmDelay();
    await user.click(within(confirmation).getByRole("button", { name: "Substituir credencial" }));
    await within(panel).findByText(/Credencial OPENROUTER_API_KEY substituída no cofre/);
    expect(calls(callTauri, "vault_set_secret")).toEqual([["vault_set_secret", { key: "OPENROUTER_API_KEY", value: SECRET }]]);
  });

  it("se a remoção dá certo mas a lista não pode ser relida, tira a credencial da lista e não mostra erro", async () => {
    const keys = ["GEMINI_API_KEY", "OPENROUTER_API_KEY"];
    const state = { fail: false };
    const callTauri = makeCallTauri(
      vaultHandlers(keys, {
        vault_list_keys: () => {
          if (state.fail) throw { code: -32010, message: "Cofre indisponível.", data: { field: "vault", reason: "unavailable" } };
          return { keys: [...keys].sort() };
        },
        vault_delete_secret: (args) => {
          keys.splice(keys.indexOf(args!.key), 1);
          state.fail = true;
          return { deleted: true, key: args!.key };
        },
      })
    );
    const { user, panel } = openProvedores(callTauri);
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    passArmDelay();
    await user.click(within(panel).getByRole("button", { name: "Remover credencial" }));

    expect(await within(panel).findByText("Credencial OPENROUTER_API_KEY removida do cofre.")).toBeInTheDocument();
    await waitFor(() => expect(within(panel).queryByText("OPENROUTER_API_KEY")).not.toBeInTheDocument());
    expect(within(panel).getByText("GEMINI_API_KEY")).toBeInTheDocument();
    expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("retira a pergunta de remoção quando a credencial some da lista, sem deixar o painel travado", async () => {
    const keys = ["GEMINI_API_KEY", "OPENROUTER_API_KEY"];
    const list = gated(() => ({ keys: [...keys].sort() }));
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_list_keys: list.handler }));
    const { user, panel } = openProvedores(callTauri);
    await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" });

    // Back on the panel, the list on screen is the old one until the reload answers.
    await user.click(screen.getByRole("tab", { name: /Perfil/ }));
    keys.splice(keys.indexOf("OPENROUTER_API_KEY"), 1); // removed elsewhere meanwhile
    list.gate.hold = true;
    await user.click(screen.getByRole("tab", { name: /Provedores/ }));
    await waitFor(() => expect(list.gate.held).toBe(true));
    await user.click(within(panel).getByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    expect(within(panel).getByRole("alertdialog")).toBeInTheDocument();
    await act(async () => list.gate.release());

    await waitFor(() => expect(within(panel).queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(within(panel).getByText("A credencial OPENROUTER_API_KEY não está mais no cofre.")).toHaveAttribute("role", "status");
    expect(within(panel).getByRole("button", { name: "Remover a credencial GEMINI_API_KEY" })).toBeEnabled();
    expect(within(panel).getByRole("button", { name: "Gravar no cofre" })).toBeEnabled();
    await waitFor(() => expect(within(panel).getByRole("heading", { name: "Credenciais no cofre" })).toHaveFocus());
    expect(calls(callTauri, "vault_delete_secret")).toHaveLength(0);
  });

  it("um erro já visto sai ao deixar o painel, e a falha de leitura do cofre é informada à parte", async () => {
    const keys = ["OPENROUTER_API_KEY"];
    const state = { unreadable: false };
    const callTauri = makeCallTauri(
      vaultHandlers(keys, {
        vault_status: () => {
          if (state.unreadable) throw { code: -32603, message: "Cofre ilegível." };
          return { available: true, backend: "dpapi" };
        },
        vault_delete_secret: () => {
          throw { code: -32603, message: "Falha ao remover." };
        },
      })
    );
    const { user, panel } = openProvedores(callTauri);
    await user.click(await within(panel).findByRole("button", { name: "Remover a credencial OPENROUTER_API_KEY" }));
    passArmDelay();
    await user.click(within(panel).getByRole("button", { name: "Remover credencial" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Falha ao remover.");

    state.unreadable = true;
    await user.click(screen.getByRole("tab", { name: /Perfil/ }));
    await user.click(screen.getByRole("tab", { name: /Provedores/ }));
    await within(panel).findByText("Cofre ilegível.");
    const alerts = within(panel).getAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent("Não foi possível ler o cofre.");
    expect(within(panel).queryByText("Falha ao remover.")).not.toBeInTheDocument();
  });

  it("ao terminar uma gravação lenta não tira o foco de onde o operador o levou", async () => {
    const keys: string[] = [];
    const write = gated((args) => {
      keys.push(args!.key);
      return { stored: true, key: args!.key };
    });
    const callTauri = makeCallTauri(vaultHandlers(keys, { vault_set_secret: write.handler }));
    const { user, panel } = openProvedores(callTauri);
    await fill(user, panel, "OPENAI_API_KEY");
    write.gate.hold = true;
    await user.click(within(panel).getByRole("button", { name: "Gravar no cofre" }));
    await within(panel).findByRole("button", { name: "Gravando…" });

    const close = within(screen.getByRole("dialog", { name: "Configurações" })).getAllByRole("button", { name: /Fechar/ })[0];
    close.focus();
    await act(async () => write.gate.release());
    expect(await within(panel).findByText(/Credencial OPENAI_API_KEY gravada no cofre/)).toBeInTheDocument();
    await act(async () => {});
    expect(close).toHaveFocus();
  });

  it("sem cofre disponível não há formulário nem uma segunda região de estado", async () => {
    const { panel } = openProvedores(makeCallTauri(updaterHandlers(UP_TO_DATE)));
    expect(await within(panel).findByText(/Armazenamento persistente de credenciais indisponível/)).toBeInTheDocument();
    expect(within(panel).getAllByRole("status")).toHaveLength(1);
    expect(within(panel).queryByLabelText("Valor")).not.toBeInTheDocument();
  });
});
