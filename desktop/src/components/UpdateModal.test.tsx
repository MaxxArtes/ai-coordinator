import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UpdateModal, type UpdateCheckResult } from "./UpdateModal";

vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn(async () => () => {}) }));

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

const AVAILABLE: UpdateCheckResult = {
  ...UP_TO_DATE,
  available: true,
  latest_version: "0.6.1-alpha.51",
  notes: "Notas da versão de teste.",
};

type Handler = (args?: Record<string, any>) => unknown;

function makeCallTauri(handlers: Record<string, Handler>) {
  return vi.fn(async (cmd: string, args?: Record<string, any>) => {
    const handler = handlers[cmd];
    if (!handler) throw new Error(`comando Tauri inesperado: ${cmd}`);
    return handler(args);
  });
}

function baseHandlers(check: UpdateCheckResult): Record<string, Handler> {
  return {
    updater_get_channel_info: () => ({ current_version: "0.6.1-alpha.50", channel: "alpha" }),
    updater_check: () => check,
  };
}

function renderModal(callTauri: ReturnType<typeof makeCallTauri>, onClose = vi.fn(), activePanesCount = 0) {
  const root = document.createElement("div");
  root.id = "root";
  document.body.appendChild(root);
  render(
    <UpdateModal isOpen onClose={onClose} activePanesCount={activePanesCount} isTauri callTauri={callTauri} />,
    { container: root }
  );
  return { onClose, user: userEvent.setup() };
}

describe("UpdateModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("abre como diálogo acessível e consulta os comandos Tauri do atualizador", async () => {
    const callTauri = makeCallTauri(baseHandlers(UP_TO_DATE));
    renderModal(callTauri);

    expect(screen.getByRole("dialog", { name: "Atualizações do sistema" })).toHaveAttribute("aria-modal", "true");
    expect(await screen.findByText("O AI Coordinator Desktop está atualizado.")).toBeInTheDocument();
    expect(callTauri).toHaveBeenCalledWith("updater_check");
    expect(callTauri).toHaveBeenCalledWith("updater_get_channel_info");
  });

  it("fecha com Esc quando nenhuma instalação está em andamento", async () => {
    const callTauri = makeCallTauri(baseHandlers(UP_TO_DATE));
    const { onClose, user } = renderModal(callTauri);
    await screen.findByText("O AI Coordinator Desktop está atualizado.");
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("exige autorização explícita para a contagem atual de sessões antes de instalar", async () => {
    const install = vi.fn(() => true);
    const callTauri = makeCallTauri({
      ...baseHandlers({ ...AVAILABLE, active_sessions_count: 2 }),
      updater_download_and_install: install,
    });
    const { user } = renderModal(callTauri, vi.fn(), 2);

    const installButton = await screen.findByRole("button", { name: "Atualizar e reiniciar" });
    expect(installButton).toBeDisabled();

    await user.click(screen.getByRole("checkbox", { name: /Autorizo o encerramento de todos os 2/ }));
    expect(installButton).toBeEnabled();
    await user.click(installButton);

    expect(callTauri).toHaveBeenCalledWith("updater_download_and_install", {
      force: true,
      expectedActiveSessions: 2,
    });
  });

  it("mantém a mensagem de uma falha recuperável depois da nova verificação", async () => {
    const callTauri = makeCallTauri({
      ...baseHandlers(AVAILABLE),
      updater_download_and_install: () => {
        throw { code: "DOWNLOAD_OR_SIGNATURE_FAILED", message: "assinatura inválida", runtime_stopped: false };
      },
    });
    const { user } = renderModal(callTauri);

    await user.click(await screen.findByRole("button", { name: "Atualizar e reiniciar" }));

    expect(await screen.findByText("Erro ao instalar atualização: assinatura inválida")).toBeInTheDocument();
    await waitFor(() => expect(callTauri.mock.calls.filter(([cmd]) => cmd === "updater_check")).toHaveLength(2));
    expect(screen.getByText("Erro ao instalar atualização: assinatura inválida")).toBeInTheDocument();
    expect(screen.queryByText(/Runtime Core encerrado/)).not.toBeInTheDocument();
  });

  it("após falha pós-desligamento trava o diálogo e oferece apenas o reinício", async () => {
    const callTauri = makeCallTauri({
      ...baseHandlers(AVAILABLE),
      updater_download_and_install: () => {
        throw { code: "INSTALL_FAILED_RUNTIME_STOPPED", message: "instalador não iniciou", runtime_stopped: true };
      },
      updater_restart_app: () => undefined,
    });
    const { onClose, user } = renderModal(callTauri);

    await user.click(await screen.findByRole("button", { name: "Atualizar e reiniciar" }));
    expect(await screen.findByText(/Runtime Core encerrado/)).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Fechar atualizações" })).toBeDisabled();
    // Only the restart is offered: no postpone/install actions and no new check against a dead runtime.
    expect(screen.queryByRole("button", { name: "Adiar" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Atualizar e reiniciar" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verificar atualizações" })).toBeDisabled();
    expect(callTauri.mock.calls.filter(([cmd]) => cmd === "updater_check")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Reiniciar aplicativo" }));
    expect(callTauri).toHaveBeenCalledWith("updater_restart_app");
  });

  it("mostra o caminho de reinício quando o host informa runtime parado na verificação", async () => {
    const callTauri = makeCallTauri(baseHandlers({ ...UP_TO_DATE, runtime_stopped: true, sessions_known: false }));
    renderModal(callTauri);
    expect(await screen.findByText(/Runtime Core encerrado/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reiniciar aplicativo" })).toBeEnabled();
    // No contradictory "up to date" message next to the restart requirement.
    expect(screen.queryByText("O AI Coordinator Desktop está atualizado.")).not.toBeInTheDocument();
  });
});
