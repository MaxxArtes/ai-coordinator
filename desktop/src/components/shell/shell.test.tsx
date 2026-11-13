import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppHeader } from "./AppHeader";
import { AuthorizationScreen } from "./AuthorizationScreen";
import { NoticeBanner } from "./NoticeBanner";
import { StartupStatus } from "./StartupStatus";

describe("AuthorizationScreen", () => {
  it("associa o rótulo ao campo e envia com Enter", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(
      <AuthorizationScreen profileName="default" inputPath={"D:\\proj"} onInputPathChange={vi.fn()} onSubmit={onSubmit} />
    );
    const input = screen.getByLabelText("Caminho do projeto no host");
    expect(input).toHaveFocus();
    await user.type(input, "{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("D:\\proj");
  });

  it("propaga a digitação para o estado do aplicativo", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<AuthorizationScreen profileName="default" inputPath="" onInputPathChange={onChange} onSubmit={vi.fn()} />);
    await user.type(screen.getByLabelText("Caminho do projeto no host"), "D");
    expect(onChange).toHaveBeenCalledWith("D");
  });
});

describe("NoticeBanner", () => {
  it("anuncia erros como alerta e permite dispensar pelo teclado", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(<NoticeBanner message="Falha ao salvar sessão" tone="error" onDismiss={onDismiss} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Falha ao salvar sessão");
    screen.getByRole("button", { name: "Dispensar aviso" }).focus();
    await user.keyboard("{Enter}");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("anuncia confirmações como status, não como alerta", () => {
    render(<NoticeBanner message="Sessão salva" tone="success" onDismiss={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Sessão salva");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("StartupStatus", () => {
  it("mostra a falha de inicialização como alerta com o detalhe recebido", () => {
    render(<StartupStatus state="failed" detail="runtime ausente" />);
    expect(screen.getByRole("alert")).toHaveTextContent("runtime ausente");
  });

  it("mostra a conexão em andamento como status", () => {
    render(<StartupStatus state="connecting" />);
    expect(screen.getByRole("status")).toHaveTextContent("Inicializando runtime do Core");
  });
});

describe("AppHeader", () => {
  const base = {
    status: { core_version: "0.6.0", host_os: "windows", is_isolated: true, python_path: "C:\\app\\runtime\\python.exe" },
    appVersion: "0.6.1-alpha.50",
    isTauri: true,
    connectionStatus: "connected" as const,
    metrics: null,
    hasUpdateAvailable: false,
    latestAvailableVersion: "",
  };

  it("abre as atualizações pelo botão do cabeçalho", async () => {
    const user = userEvent.setup();
    const onOpenUpdates = vi.fn();
    render(<AppHeader {...base} onOpenUpdates={onOpenUpdates} />);
    await user.click(screen.getByRole("button", { name: "Atualizações" }));
    expect(onOpenUpdates).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Python: python\.exe \(isolado\)/)).toBeInTheDocument();
  });

  it("indica atualização disponível no rótulo do botão", () => {
    render(<AppHeader {...base} hasUpdateAvailable latestAvailableVersion="0.6.1-alpha.51" onOpenUpdates={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Atualização v0.6.1-alpha.51" })).toBeInTheDocument();
  });

  it("sinaliza o modo de visualização sem host Tauri", () => {
    render(<AppHeader {...base} isTauri={false} onOpenUpdates={vi.fn()} />);
    expect(screen.getByText("Vite Preview (não homologado)")).toBeInTheDocument();
  });
});
