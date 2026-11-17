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

  it("abre as Configurações pelo botão do cabeçalho", async () => {
    const user = userEvent.setup();
    const onOpenSettings = vi.fn();
    render(<AppHeader {...base} onOpenSettings={onOpenSettings} />);
    await user.click(screen.getByRole("button", { name: "Configurações" }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Python:/)).not.toBeInTheDocument();
  });

  it("indica atualização disponível no rótulo do botão de Configurações", () => {
    render(<AppHeader {...base} hasUpdateAvailable latestAvailableVersion="0.6.1-alpha.51" onOpenSettings={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Configurações · atualização v0.6.1-alpha.51" })).toBeInTheDocument();
  });

  it("sinaliza o modo de visualização sem host Tauri", () => {
    render(<AppHeader {...base} isTauri={false} onOpenSettings={vi.fn()} />);
    expect(screen.getByText("Preview: sem Core")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/conectado/i);
  });

  it("mostra a marca decorativa, o título e a versão real", () => {
    const { container } = render(<AppHeader {...base} onOpenSettings={vi.fn()} />);
    expect(container.querySelector(".brand__mark")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("heading", { level: 1, name: "AI Coordinator Desktop" })).toBeInTheDocument();
    expect(screen.getByText("v0.6.1-alpha.50")).toBeInTheDocument();
  });

  it.each(["", "indisponível", "  "])("sem versão conhecida (%j) não mostra 'v' solto", (appVersion) => {
    const { container } = render(<AppHeader {...base} appVersion={appVersion} onOpenSettings={vi.fn()} />);
    expect(container.querySelector(".app-header__version")).toBeNull();
    expect(screen.queryByText(/^v/)).toBeNull();
  });

  it("um texto e um tom por estado de conexão", () => {
    const seen = new Set<string>();
    for (const [connectionStatus, text, tone] of [
      ["connected", "Core conectado", "ok"],
      ["connecting", "Conectando…", "warn"],
      ["authorizing", "Aguardando autorização", "info"],
      ["failed", "Falha", "err"],
    ] as const) {
      const { container, unmount } = render(<AppHeader {...base} connectionStatus={connectionStatus} onOpenSettings={vi.fn()} />);
      const pill = container.querySelector(".status-pill");
      expect(pill).toHaveTextContent(text);
      expect(pill).toHaveClass(`status-pill--${tone}`);
      seen.add(pill!.textContent ?? "");
      unmount();
    }
    expect(seen.size).toBe(4);
  });

  it("não mostra Python, CPU, métricas nem Vite Preview, no host e no preview", () => {
    for (const isTauri of [true, false]) {
      const { unmount } = render(<AppHeader {...base} isTauri={isTauri} onOpenSettings={vi.fn()} />);
      expect(document.body.textContent).not.toMatch(/Python:|CPU|PID|RAM|Vite Preview|Ativa/);
      unmount();
    }
  });

  it("'Atualização' só aparece com atualização real; o nome do botão é estável", () => {
    const { rerender } = render(<AppHeader {...base} onOpenSettings={vi.fn()} />);
    expect(screen.queryByText("Atualização")).toBeNull();
    expect(screen.getByRole("button", { name: "Configurações" })).toBeInTheDocument();
    rerender(<AppHeader {...base} hasUpdateAvailable latestAvailableVersion="0.6.1-alpha.51" onOpenSettings={vi.fn()} />);
    expect(screen.getByText("Atualização")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Configurações · atualização v0.6.1-alpha.51" })).toHaveClass("btn--attention");
  });

  it("atualização sem versão válida não deixa 'v' solto no nome", () => {
    render(<AppHeader {...base} hasUpdateAvailable latestAvailableVersion="" onOpenSettings={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Configurações · atualização disponível" })).toBeInTheDocument();
  });

  it("é a região do F6 e põe a navegação recebida no centro", () => {
    render(<AppHeader {...base} onOpenSettings={vi.fn()} nav={<nav aria-label="Seções principais" />} />);
    const banner = screen.getByRole("banner");
    expect(banner).toHaveAttribute("data-f6-region", "header");
    expect(banner).toContainElement(screen.getByRole("navigation", { name: "Seções principais" }));
  });
});
