import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SettingsDialog } from "./SettingsDialog";
import type { SettingsCategoryId } from "./categories";
import type { UpdateCheckResult } from "./UpdatesSection";
import type { RuntimeStatus } from "../shell/AppHeader";
import { applyInterfaceSettings, type SettingsSnapshot } from "./api";

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
