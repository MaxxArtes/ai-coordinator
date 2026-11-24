/**
 * Agentes, rotas de fallback and Segurança (Etapa 7) with the host's real answer shapes (desktop/ipc/discovery.py and
 * server.py): what could not be read is said, the policy is in plain Portuguese, and revoking a directory is confirmed,
 * keeps the focus inside the panel and tells the App.
 */
import React from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { InventoryPanel } from "./InventoryPanel";
import type { AgentsSnapshot, RoutesSnapshot, SecurityStatus } from "./api";

type Handler = (args?: Record<string, any>) => unknown;

function host(handlers: Record<string, Handler>) {
  return vi.fn(async (cmd: string, args?: Record<string, any>) => {
    const handler = handlers[cmd];
    if (!handler) throw new Error(`comando inesperado: ${cmd}`);
    return handler(args);
  });
}

const calls = (callTauri: ReturnType<typeof host>, cmd: string) => callTauri.mock.calls.filter(([c]) => c === cmd);

const AGENTS: AgentsSnapshot = {
  agents: [
    { name: "Codex", client: "codex", available: true, reason: "Launcher encontrado; autenticação e cota não verificadas." },
    { name: "AGY", client: "agy", available: false, reason: "Launcher indisponível ou configuração ilegível." },
  ],
  mcp_servers: ["docs"],
  mcp_complete: true,
  source: "configured",
  unsupported: [{ name: "Gemini CLI", reason: "Exige um novo contrato de cliente no Core." }],
};

const ROUTES: RoutesSnapshot = {
  routes: [{ category: "codigo", candidates: [{ client: "codex", model: "modelo-a", effort: "high" }, { client: "agy", model: "modelo-b" }] }],
  available: true,
  source: "configured",
  quota_status: "unknown",
  quota_milestone: "4D.4.3",
};

const POLICY = {
  project_fencing: true,
  human_terminal: "operator_permissions",
  governed_agent: "env_sanitized_only",
  human_terminal_filesystem_isolation: false,
  description: "A autorização limita a abertura de projetos. O terminal humano usa as permissões do operador do sistema.",
};

function security(projects: string[], policy: Record<string, boolean | string> = POLICY): SecurityStatus {
  return { authorized_projects: [...projects], policy };
}

function renderPanel(kind: "agentes" | "provedores" | "seguranca", callTauri: ReturnType<typeof host>, onAuthorizationChanged = vi.fn()) {
  render(<InventoryPanel kind={kind} active isTauri callTauri={callTauri} onAuthorizationChanged={onAuthorizationChanged} />);
  return { onAuthorizationChanged, user: userEvent.setup() };
}

describe("InventoryPanel — inventário honesto (K6)", () => {
  it("rotas ilegíveis: diz que não puderam ser lidas, nunca uma lista vazia", async () => {
    const callTauri = host({
      providers_routes: () => ({
        routes: [],
        available: false,
        source: "unreadable",
        reason: "Configuração de rotas ilegível; nada foi alterado.",
        quota_status: "unknown",
        quota_milestone: "4D.4.3",
      }),
    });
    renderPanel("provedores", callTauri);
    expect(await screen.findByRole("alert")).toHaveTextContent("Configuração de rotas ilegível; nada foi alterado.");
    expect(screen.queryByText(/Nenhuma rota/)).not.toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument();
    expect(screen.getByText(/Cotas: indisponíveis até a 4D\.4\.3/)).toBeInTheDocument();
  });

  it("rotas padrão empacotadas: diz a origem; candidatos só com cliente, modelo e esforço", async () => {
    renderPanel("provedores", host({ providers_routes: () => ({ ...ROUTES, source: "packaged_defaults" }) }));
    expect(await screen.findByText("Rotas padrão empacotadas (nenhum routes.json do usuário).")).toBeInTheDocument();
    expect(screen.getByRole("listitem")).toHaveTextContent("codigo: codex / modelo-a / high → agy / modelo-b");
  });

  it("provedores com telemetria 4D.4.3 integra resumo honesto sem prometer saldo por credencial", async () => {
    renderPanel(
      "provedores",
      host({
        providers_routes: () => ROUTES,
        providers_telemetry: () => ({
          schema_version: 1,
          profile_name: "default",
          sampled_at_unix_ms: Date.now(),
          ttl_ms: 300_000,
          stale: false,
          data_origin: "offline_cache",
          provenance: "measured",
          sources: { sqlite_available: true, quota_snapshot_available: false, vault_keys_checked: true },
          providers: [
            {
              provider: "claude",
              display_name: "Claude",
              profile_name: "default",
              launcher_available: true,
              has_vault_key: true,
              availability_status: "available",
              quota_kind: "subscription_quota",
              windows: {
                five_hour: { window: "5h", label: "5 horas", used_percent: 25.0, remaining_percent: 75.0, resets_at_iso: null, provenance: "measured" },
                weekly: { window: "weekly", label: "Semanal", used_percent: null, remaining_percent: null, resets_at_iso: null, provenance: "unknown" },
              },
              tokens: { input_tokens: 100, output_tokens: 50, cache_read_tokens: null, cache_creation_tokens: null, reasoning_tokens: null, total_tokens: 150, provenance: "measured" },
              models: [],
              data_origin: "offline_cache",
              observed_at_unix_ms: Date.now(),
              ttl_ms: 300_000,
              stale: false,
            },
          ],
        }),
      })
    );
    expect(await screen.findByLabelText("Resumo de cotas e tokens dos provedores")).toHaveTextContent("5h: 25,0%");
    expect(screen.getByLabelText("Resumo de cotas e tokens dos provedores")).toHaveTextContent("Tokens: 150");
  });

  it("provedores com leitura local: renovação, contexto, custo, idade e origem; janelas que não se aplicam", async () => {
    const now = Date.now();
    const unknownWindow = { used_percent: null, remaining_percent: null, resets_at_iso: null, provenance: "unknown" as const };
    const noTokens = { input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_creation_tokens: null, reasoning_tokens: null, total_tokens: null, provenance: "unknown" as const };
    renderPanel(
      "provedores",
      host({
        providers_routes: () => ROUTES,
        providers_telemetry: () => ({
          schema_version: 1,
          profile_name: "default",
          sampled_at_unix_ms: now,
          ttl_ms: 300_000,
          stale: false,
          data_origin: "offline_cache",
          provenance: "measured",
          sources: { sqlite_available: false, quota_snapshot_available: false, vault_keys_checked: true, local_sources_checked: true },
          providers: [
            {
              provider: "codex",
              display_name: "Codex",
              profile_name: "default",
              launcher_available: true,
              has_vault_key: false,
              availability_status: "unknown",
              quota_kind: "subscription_quota",
              windows: {
                five_hour: { window: "5h", label: "Janela de 5 h", used_percent: 22, remaining_percent: 78, resets_at_iso: new Date(now + 3_600_000).toISOString(), provenance: "measured" },
                weekly: { window: "weekly", label: "Janela semanal", used_percent: 3, remaining_percent: 97, resets_at_iso: null, provenance: "measured" },
              },
              tokens: { ...noTokens, total_tokens: 1160, provenance: "measured" },
              context: { used_tokens: 420, limit_tokens: 1000, used_percent: 42, provenance: "measured" },
              models: [],
              data_origin: "offline_cache",
              observed_at_unix_ms: now - 120_000,
              ttl_ms: 900_000,
              stale: false,
            },
            {
              provider: "opencode",
              display_name: "OpenCode",
              profile_name: "default",
              launcher_available: true,
              has_vault_key: false,
              availability_status: "unknown",
              quota_kind: "api_rate_limit",
              windows_supported: false,
              windows: {
                five_hour: { window: "5h", label: "Janela de 5 h", ...unknownWindow, supported: false },
                weekly: { window: "weekly", label: "Janela semanal", ...unknownWindow, supported: false },
              },
              tokens: noTokens,
              cost_credits: { kind: "api_rate_limit", currency: "USD", balance_usd: null, credits_remaining_usd: null, spent_usd: 0.5, estimated_cost_usd: null, provenance: "measured" },
              models: [],
              data_origin: "offline_cache",
              observed_at_unix_ms: now,
              ttl_ms: 900_000,
              stale: false,
            },
          ],
        }),
      })
    );
    const summary = await screen.findByLabelText("Resumo de cotas e tokens dos provedores");
    const codex = within(summary).getByText("Codex").closest("div") as HTMLElement;
    expect(codex).toHaveTextContent("5h: 22,0% (renova");
    expect(codex).toHaveTextContent("Semanal: 3,0%");
    expect(codex).toHaveTextContent("Tokens: 1.160 · Contexto: 420 de 1.000 · leitura há 2 min · cache offline");
    const opencode = within(summary).getByText("OpenCode").closest("div") as HTMLElement;
    expect(opencode).toHaveTextContent("5h: não se aplica");
    expect(opencode).toHaveTextContent("Semanal: não se aplica");
    expect(opencode).toHaveTextContent("Gasto: US$");
  });

  it("sem rotas configuradas: diz isso, sem alerta", async () => {
    renderPanel("provedores", host({ providers_routes: () => ({ ...ROUTES, routes: [] }) }));
    expect(await screen.findByText("Nenhuma rota de fallback configurada.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("MCP incompleto: avisa que a lista pode estar incompleta e não afirma que não há servidores", async () => {
    renderPanel("agentes", host({ discovery_agents: () => ({ ...AGENTS, mcp_servers: [], mcp_complete: false }) }));
    expect(await screen.findByText("Lista de MCP incompleta: alguma fonte não pôde ser lida.")).toBeInTheDocument();
    expect(screen.queryByText(/Nenhum servidor MCP encontrado/)).not.toBeInTheDocument();
  });

  it("MCP completo e vazio: diz que nenhum foi encontrado", async () => {
    renderPanel("agentes", host({ discovery_agents: () => ({ ...AGENTS, mcp_servers: [] }) }));
    expect(await screen.findByText("Nenhum servidor MCP encontrado nas fontes consultadas.")).toBeInTheDocument();
    expect(screen.queryByText(/incompleta/)).not.toBeInTheDocument();
  });

  it("configuração do Core ilegível: os launchers aparecem como não verificados, com o motivo", async () => {
    const unreadable = { ...AGENTS, source: "unreadable", agents: AGENTS.agents.map((a) => ({ ...a, available: false, reason: "Launcher indisponível ou configuração ilegível." })) };
    renderPanel("agentes", host({ discovery_agents: () => unreadable }));
    expect(await screen.findByText(/Configuração do Core ilegível: a disponibilidade dos launchers não foi verificada/)).toBeInTheDocument();
    expect(screen.getByText("Codex").closest("li")).toHaveTextContent("indisponível — Launcher indisponível ou configuração ilegível.");
  });

  it("agentes: disponibilidade e motivo de cada um, só nomes de MCP", async () => {
    renderPanel("agentes", host({ discovery_agents: () => AGENTS }));
    expect((await screen.findByText("Codex")).closest("li")).toHaveTextContent("disponível — Launcher encontrado");
    expect(screen.getByText("AGY").closest("li")).toHaveTextContent("indisponível");
    expect(screen.getByText("docs")).toBeInTheDocument();
    expect(screen.getByText(/Gemini CLI: não suportado/)).toBeInTheDocument();
  });

  it("a recarga descarta respostas antigas e fica travada enquanto lê", async () => {
    const answers: Array<(value: RoutesSnapshot) => void> = [];
    const callTauri = host({ providers_routes: () => new Promise<RoutesSnapshot>((resolve) => answers.push(resolve)) });
    const { user } = renderPanel("provedores", callTauri);
    const reload = screen.getByRole("button", { name: "Recarregar rotas de fallback" });
    expect(reload).toHaveAttribute("aria-disabled", "true");
    expect(reload).toBeEnabled(); // never a real `disabled`: the focus would drop out of the dialog
    expect(screen.getByRole("status")).toHaveTextContent("Carregando…");
    await user.click(reload); // ignored while the first read is in flight
    expect(calls(callTauri, "providers_routes")).toHaveLength(1);
    await act(async () => answers[0]({ ...ROUTES, routes: [{ category: "primeira", candidates: [] }] }));
    expect(await screen.findByText("primeira")).toBeInTheDocument();

    await user.click(reload);
    await waitFor(() => expect(calls(callTauri, "providers_routes")).toHaveLength(2));
    expect(screen.getByRole("status")).toHaveTextContent("Carregando…");
    await act(async () => answers[1]({ ...ROUTES, routes: [{ category: "segunda", candidates: [] }] }));
    expect(await screen.findByText("segunda")).toBeInTheDocument();
    expect(reload).not.toHaveAttribute("aria-disabled");
  });

  it("uma resposta que chega depois de sair do painel é descartada", async () => {
    let answer: (value: RoutesSnapshot) => void = () => undefined;
    const callTauri = host({ providers_routes: () => new Promise<RoutesSnapshot>((resolve) => (answer = resolve)) });
    const view = render(<InventoryPanel kind="provedores" active isTauri callTauri={callTauri} />);
    view.rerender(<InventoryPanel kind="provedores" active={false} isTauri callTauri={callTauri} />);
    await act(async () => answer({ ...ROUTES, routes: [{ category: "velha", candidates: [] }] }));
    expect(screen.queryByText("velha")).not.toBeInTheDocument();
  });
});

describe("InventoryPanel — política em português (K7)", () => {
  it("rótulos e valores legíveis, nunca as chaves internas", async () => {
    renderPanel("seguranca", host({ security_status: () => security(["D:\\proj"]) }));
    const terms = await screen.findAllByRole("term");
    expect(terms.map((t) => t.textContent)).toEqual([
      "Só diretórios autorizados podem ser abertos como projeto",
      "Permissões do terminal humano",
      "Isolamento de arquivos do terminal humano",
      "Agentes governados abertos pelo Desktop",
    ]);
    expect(screen.getAllByRole("definition").map((d) => d.textContent)).toEqual([
      "Sim",
      "as do seu usuário do Windows (sem sandbox)",
      "Não",
      "só recebem um ambiente filtrado (variáveis de segredo removidas); sem sandbox",
    ]);
    expect(screen.getByText(POLICY.description)).toBeInTheDocument();
    const text = document.body.textContent ?? "";
    for (const raw of ["project_fencing", "human_terminal", "operator_permissions", "governed_agent", "env_sanitized_only", "description", "true", "false"]) {
      expect(text).not.toContain(raw);
    }
  });

  it("valor ou item desconhecido é dito desconhecido, sem mostrar a chave crua", async () => {
    renderPanel(
      "seguranca",
      host({ security_status: () => security([], { project_fencing: true, governed_agent: "novo_modo", sandbox_level: "strict" }) })
    );
    expect(await screen.findByText("Valor não reconhecido por esta versão da interface")).toBeInTheDocument();
    expect(screen.getByText("1 item da política não é reconhecido por esta versão da interface.")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/novo_modo|sandbox_level|strict/);
  });
});

describe("InventoryPanel — revogação (K4)", () => {
  function revokeHost(projects: string[], revoke: (args: { projectPath: string; list: string[] }) => unknown) {
    const list = [...projects];
    const callTauri = host({
      security_status: () => security(list),
      profile_revoke_project: (args) => revoke({ projectPath: args?.projectPath, list }),
    });
    return { callTauri, list };
  }

  it("pergunta antes de revogar; Cancelar não chama nada e devolve o foco ao botão que abriu", async () => {
    const { callTauri } = revokeHost(["D:\\proj", "D:\\outro"], () => ({ revoked: true, authorized_projects: [], last_project: null }));
    const { user } = renderPanel("seguranca", callTauri);
    const revogar = await screen.findByRole("button", { name: "Revogar D:\\proj" });
    await user.click(revogar);
    const dialog = screen.getByRole("alertdialog", { name: "Revogar a autorização de D:\\proj?" });
    expect(dialog).toHaveAccessibleDescription(/Arquivos e histórico não são apagados/);
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toHaveFocus();
    expect(calls(callTauri, "profile_revoke_project")).toHaveLength(0);
    // Everything else waits for the answer, without a real `disabled` that would drop the focus.
    expect(screen.getByRole("button", { name: "Revogar D:\\outro" })).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("button", { name: "Revogar D:\\outro" }));
    expect(screen.getByRole("alertdialog", { name: "Revogar a autorização de D:\\proj?" })).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Cancelar" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(revogar).toHaveFocus());
    expect(calls(callTauri, "profile_revoke_project")).toHaveLength(0);
  });

  it("Esc cancela a pergunta e devolve o foco", async () => {
    const { callTauri } = revokeHost(["D:\\proj"], () => ({ revoked: true, authorized_projects: [], last_project: null }));
    const { user } = renderPanel("seguranca", callTauri);
    const revogar = await screen.findByRole("button", { name: "Revogar D:\\proj" });
    await user.click(revogar);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(revogar).toHaveFocus());
    expect(calls(callTauri, "profile_revoke_project")).toHaveLength(0);
  });

  it("confirmar revoga uma vez, anuncia, relê a lista, avisa o App e leva o foco ao título da lista", async () => {
    let release: () => void = () => undefined;
    const { callTauri } = revokeHost(["D:\\proj", "D:\\outro"], ({ projectPath, list }) => {
      return new Promise((resolve) => {
        release = () => {
          list.splice(list.indexOf(projectPath), 1);
          resolve({ revoked: true, authorized_projects: [...list], last_project: null });
        };
      });
    });
    const { user, onAuthorizationChanged } = renderPanel("seguranca", callTauri);
    await user.click(await screen.findByRole("button", { name: "Revogar D:\\proj" }));
    const confirm = screen.getByRole("button", { name: "Confirmar revogação" });
    await user.click(confirm);
    await user.click(confirm); // a second activation while the first is in flight is ignored
    expect(screen.getByRole("status")).toHaveTextContent("Revogando…");
    expect(confirm).toHaveAttribute("aria-disabled", "true");
    expect(confirm).toBeEnabled();
    await act(async () => release());

    expect(calls(callTauri, "profile_revoke_project")).toEqual([["profile_revoke_project", { projectPath: "D:\\proj" }]]);
    expect(await screen.findByText("Autorização de D:\\proj revogada. Arquivos e histórico foram preservados.")).toHaveAttribute("role", "status");
    expect(onAuthorizationChanged).toHaveBeenCalledWith({ revoked: true, authorized_projects: ["D:\\outro"], last_project: null });
    expect(screen.queryByRole("button", { name: "Revogar D:\\proj" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Revogar D:\\outro" })).toBeInTheDocument();
    expect(calls(callTauri, "security_status")).toHaveLength(2);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Diretórios autorizados" })).toHaveFocus());
  });

  it("recusa por sessões ativas: mostra o motivo, mantém a pergunta com o foco em Cancelar e não muda nada", async () => {
    const { callTauri } = revokeHost(["D:\\proj"], () => {
      throw {
        code: -32003,
        message: "Feche os terminais deste diretório antes de revogar a autorização.",
        data: { field: "project_path", reason: "active_sessions" },
      };
    });
    const { user, onAuthorizationChanged } = renderPanel("seguranca", callTauri);
    await user.click(await screen.findByRole("button", { name: "Revogar D:\\proj" }));
    await user.click(screen.getByRole("button", { name: "Confirmar revogação" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Feche os terminais deste diretório antes de revogar a autorização.");
    const dialog = screen.getByRole("alertdialog");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancelar" })).toHaveFocus());
    expect(screen.getByRole("button", { name: "Revogar D:\\proj" })).toBeInTheDocument();
    expect(onAuthorizationChanged).not.toHaveBeenCalled();
    expect(calls(callTauri, "security_status")).toHaveLength(1);
  });

  it("perfil ocupado por outra janela: mostra a mensagem e permite tentar de novo", async () => {
    let busy = true;
    const { callTauri } = revokeHost(["D:\\proj"], ({ projectPath, list }) => {
      if (busy) throw { code: -32603, message: "Outra janela está usando o perfil/cofre. Tente novamente.", data: { field: "profile", reason: "busy" } };
      list.splice(list.indexOf(projectPath), 1);
      return { revoked: true, authorized_projects: [...list], last_project: null };
    });
    const { user } = renderPanel("seguranca", callTauri);
    await user.click(await screen.findByRole("button", { name: "Revogar D:\\proj" }));
    await user.click(screen.getByRole("button", { name: "Confirmar revogação" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Outra janela está usando o perfil/cofre. Tente novamente.");
    busy = false;
    await user.click(screen.getByRole("button", { name: "Confirmar revogação" }));
    expect(await screen.findByText(/revogada/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("revogação não confirmada pelo host: diz isso e relê a lista", async () => {
    const { callTauri } = revokeHost(["D:\\proj"], () => ({ revoked: false, authorized_projects: ["D:\\proj"], last_project: null }));
    const { user, onAuthorizationChanged } = renderPanel("seguranca", callTauri);
    await user.click(await screen.findByRole("button", { name: "Revogar D:\\proj" }));
    await user.click(screen.getByRole("button", { name: "Confirmar revogação" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A revogação não foi confirmada; a lista foi relida.");
    expect(onAuthorizationChanged).not.toHaveBeenCalled();
    expect(calls(callTauri, "security_status")).toHaveLength(2);
    expect(screen.queryByText(/revogada\./)).not.toBeInTheDocument();
  });

  it("a região de estado existe antes de qualquer aviso (é anunciada ao mudar)", async () => {
    renderPanel("seguranca", host({ security_status: () => security([]) }));
    const status = screen.getByRole("status");
    expect(await screen.findByText("Nenhum diretório autorizado.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toBeEmptyDOMElement();
  });
});

describe("InventoryPanel — diretórios amplos (L5/L7)", () => {
  const withBroad = (projects: string[], broad?: string[]): SecurityStatus => ({
    ...security(projects),
    ...(broad ? { broad_projects: broad } : {}),
  });

  it("marca só as entradas que o host diz amplas e explica o que isso significa", async () => {
    renderPanel("seguranca", host({ security_status: () => withBroad(["C:\\", "D:\\proj"], ["C:\\"]) }));
    const broadItem = (await screen.findByRole("button", { name: "Revogar C:\\" })).closest("li")!;
    const plainItem = screen.getByRole("button", { name: "Revogar D:\\proj" }).closest("li")!;
    expect(within(broadItem).getByText("Acesso amplo")).toHaveClass("badge", "badge--warn");
    expect(within(plainItem).queryByText("Acesso amplo")).not.toBeInTheDocument();
    expect(screen.getByText(/^1 diretório autorizado dá acesso amplo/)).toHaveTextContent(
      "1 diretório autorizado dá acesso amplo (raiz de um disco ou compartilhamento, ou a pasta pessoal inteira): os terminais podem abrir qualquer pasta dentro dele, com as permissões do seu usuário do Windows. Se não for necessário, revogue e autorize só a pasta do projeto."
    );
    // A mark, not an alarm: nothing is announced as an error, and nothing was changed.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("várias entradas amplas (raiz de compartilhamento, pasta pessoal) e a mesma entrada com outra grafia", async () => {
    renderPanel(
      "seguranca",
      host({ security_status: () => withBroad(["\\\\servidor\\pasta", "c:\\users\\ana", "D:\\proj"], ["\\\\servidor\\pasta", "C:\\Users\\Ana"]) })
    );
    await screen.findByRole("button", { name: "Revogar D:\\proj" });
    expect(screen.getAllByText("Acesso amplo")).toHaveLength(2);
    expect(screen.getByText(/^2 diretórios autorizados dão acesso amplo/)).toHaveTextContent("qualquer pasta dentro deles,");
  });

  it("host anterior (sem broad_projects) ou nenhuma entrada ampla: nada é marcado", async () => {
    renderPanel("seguranca", host({ security_status: () => withBroad(["C:\\", "D:\\proj"]) }));
    await screen.findByRole("button", { name: "Revogar C:\\" });
    expect(screen.queryByText("Acesso amplo")).not.toBeInTheDocument();
    expect(screen.queryByText(/acesso amplo \(raiz/)).not.toBeInTheDocument();
  });
});
