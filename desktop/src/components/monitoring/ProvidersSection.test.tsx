/**
 * Provider cards fed by the local read-only adapters (4D.4 r2): reset times, context of the last turn, profiles,
 * extra quota windows, windows that do not apply, plan, scope of the token count, sources and origin.
 */
import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { ProvidersSection } from "./ProvidersSection";
import type { ProviderTelemetryItem, ProvidersTelemetrySnapshot, QuotaWindowReading } from "./telemetryContract";

const NOW = Date.now();

function win(window: string, label: string, used: number | null, extra: Partial<QuotaWindowReading> = {}): QuotaWindowReading {
  return {
    window,
    label,
    used_percent: used,
    remaining_percent: used === null ? null : 100 - used,
    resets_at_iso: used === null ? null : new Date(NOW + 3_600_000).toISOString(),
    provenance: used === null ? "unknown" : "measured",
    data_origin: "offline_cache",
    observed_at_unix_ms: NOW - 60_000,
    ttl_ms: 900_000,
    stale: false,
    ...extra,
  };
}

const NO_TOKENS = {
  input_tokens: null,
  output_tokens: null,
  cache_read_tokens: null,
  cache_creation_tokens: null,
  reasoning_tokens: null,
  total_tokens: null,
  provenance: "unknown" as const,
};

function item(provider: string, display: string, extra: Partial<ProviderTelemetryItem>): ProviderTelemetryItem {
  return {
    provider,
    display_name: display,
    profile_name: "default",
    launcher_available: true,
    has_vault_key: false,
    availability_status: "unknown",
    quota_kind: "subscription_quota",
    windows: { five_hour: win("5h", "Janela de 5 h", null), weekly: win("weekly", "Janela semanal", null) },
    tokens: NO_TOKENS,
    models: [],
    data_origin: "offline_cache",
    observed_at_unix_ms: NOW - 60_000,
    ttl_ms: 900_000,
    stale: false,
    ...extra,
  };
}

const NOT_APPLICABLE = "OpenCode não tem janela própria de 5 h nem semanal; depende do provedor conectado.";

const SNAPSHOT: ProvidersTelemetrySnapshot = {
  schema_version: 1,
  profile_name: "default",
  sampled_at_unix_ms: NOW - 1000,
  ttl_ms: 300_000,
  stale: false,
  data_origin: "offline_cache",
  provenance: "measured",
  sources: {
    sqlite_available: true,
    quota_snapshot_available: false,
    vault_keys_checked: true,
    local_sources_checked: true,
    local_providers: { claude: true, codex: true, agy: true, opencode: true, openrouter: true },
    local_read_at_unix_ms: NOW - 1000,
  },
  providers: [
    item("claude", "Claude", {
      windows: {
        five_hour: win("5h", "Janela de 5 h", 24, { profile: "carvalima" }),
        weekly: win("weekly", "Janela semanal", 4, { profile: "carvalima" }),
      },
      tokens: { ...NO_TOKENS, input_tokens: 16, output_tokens: 301, cache_read_tokens: 3000, total_tokens: 3367, provenance: "estimated" },
      usage_scope: { label: "sessões locais atualizadas nas últimas 48 h", sessions: 2, truncated: true },
      context: { used_tokens: 2005, limit_tokens: null, used_percent: null, model: "claude-test-opus", provenance: "measured" },
      accounts: [
        { profile: "carvalima", windows: { five_hour: win("5h", "Janela de 5 h", 24), weekly: win("weekly", "Janela semanal", 4) }, observed_at_unix_ms: NOW - 60_000, stale: false },
        { profile: "max", windows: { five_hour: win("5h", "Janela de 5 h", 49), weekly: win("weekly", "Janela semanal", null) }, observed_at_unix_ms: NOW - 3_600_000, stale: true },
      ],
      local_sources: [
        { kind: "statusline", label: "Statusline do Claude Code (aviso-cota)", available: true },
        { kind: "session_logs", label: "Sessões locais do Claude Code (.jsonl)", available: true },
      ],
      local_available: true,
    }),
    item("codex", "Codex", {
      windows: { five_hour: win("5h", "Janela de 5 h", 22), weekly: win("weekly", "Janela semanal", 3) },
      context: { used_tokens: 420, limit_tokens: 1000, used_percent: 42, model: "gpt-test-codex", provenance: "measured" },
      plan: { plan_type: "team", has_credits: false, unlimited: false, credits_balance: 0, provenance: "measured" },
    }),
    item("agy", "Antigravity (AGY)", {
      availability_status: "quota_exhausted",
      availability_retry_at_iso: new Date(NOW + 7_200_000).toISOString(),
      exhausted_model: "gemini-test-high",
      exhausted_until_iso: new Date(NOW + 7_200_000).toISOString(),
      extra_windows: [win("3p-weekly", "Claude and GPT models · semanal", 100, { group: "Claude and GPT models" })],
    }),
    item("opencode", "OpenCode", {
      quota_kind: "api_rate_limit",
      windows_supported: false,
      windows: {
        five_hour: win("5h", "Janela de 5 h", null, { supported: false, note: NOT_APPLICABLE }),
        weekly: win("weekly", "Janela semanal", null, { supported: false, note: NOT_APPLICABLE }),
      },
      cost_credits: { kind: "api_rate_limit", currency: "USD", balance_usd: null, credits_remaining_usd: null, spent_usd: 0.5, estimated_cost_usd: null, provenance: "measured" },
    }),
    item("openrouter", "OpenRouter", {
      quota_kind: "financial_credit",
      windows_supported: false,
      windows: {
        five_hour: win("5h", "Janela de 5 h", null, { supported: false }),
        weekly: win("weekly", "Janela semanal", null, { supported: false }),
      },
      extra_windows: [win("openrouter-free-dia", "Cota diária :free", 2)],
      remote_lookup: { enabled: false, note: "exige autorização explícita." },
    }),
  ],
};

function card(name: string) {
  return within(screen.getByRole("article", { name: `Provedor ${name}` }));
}

describe("ProvidersSection — leituras locais dos provedores", () => {
  it("janelas com renovação, perfis sem soma, contexto sem limite inventado e escopo parcial", () => {
    render(<ProvidersSection telemetry={SNAPSHOT} />);
    const claude = card("Claude");
    expect(claude.getAllByText(/Renova:/).length).toBeGreaterThanOrEqual(2);
    expect(claude.getByText("Perfis locais (sem soma entre perfis)")).toBeInTheDocument();
    expect(claude.getByText("max").closest("li")).toHaveTextContent("5 h: 49,0% · semanal: indisponível");
    expect(claude.getByText("max").closest("li")).toHaveTextContent("expirada");
    const context = claude.getByLabelText("Contexto do último turno");
    expect(context).toHaveTextContent("2.005 tokens");
    expect(context).toHaveTextContent("Limite da janela indisponível na fonte local");
    expect(claude.getByText(/Escopo:/)).toHaveTextContent("2 sessões · amostra parcial (cauda limitada dos registros)");
    expect(claude.getByText(/Fontes locais \(somente leitura\)/)).toHaveTextContent("Statusline do Claude Code (aviso-cota): lida");
    expect(claude.getByText("Origem: cache offline")).toBeInTheDocument();
  });

  it("contexto com limite e plano do Codex", () => {
    render(<ProvidersSection telemetry={SNAPSHOT} />);
    const codex = card("Codex");
    expect(codex.getByLabelText("Contexto do último turno")).toHaveTextContent("420 de 1.000 tokens · 42,0%");
    expect(codex.getByText(/Assinatura:/).closest("p")).toHaveTextContent("Plano: team · Créditos: 0");
  });

  it("janelas que não se aplicam não viram barra vazia; cotas extras e consulta remota desligada aparecem", () => {
    render(<ProvidersSection telemetry={SNAPSHOT} />);
    const opencode = card("OpenCode");
    expect(opencode.getByText("Janelas de 5 h e semanal")).toBeInTheDocument();
    expect(opencode.getByText(`Não se aplica: ${NOT_APPLICABLE}`)).toBeInTheDocument();
    expect(opencode.queryByText(/^não (se aplica|suportado)$/i)).not.toBeInTheDocument();
    expect(screen.getByRole("article", { name: "Provedor OpenCode" }).querySelector(".provider-window__bar")).toBeNull();
    expect(opencode.queryByText("Janela de 5 h")).not.toBeInTheDocument();
    expect(opencode.queryByText("Janela semanal")).not.toBeInTheDocument();
    expect(opencode.getByText(/Gasto:/)).toHaveTextContent("US$");

    const openrouter = card("OpenRouter");
    expect(openrouter.getByText("Não se aplica: este provedor não tem janela de 5 h nem semanal.")).toBeInTheDocument();
    expect(openrouter.getByLabelText("Outras cotas de OpenRouter")).toHaveTextContent("Cota diária :free");
    expect(openrouter.getByLabelText("Outras cotas de OpenRouter")).toHaveTextContent("2,0%");
    expect(openrouter.getByText(/Consulta remota: desligada \(opt-in\)/)).toBeInTheDocument();
  });

  it("uma janela isolada que não se aplica explica o motivo e não desenha barra vazia", () => {
    const only5h: ProvidersTelemetrySnapshot = {
      ...SNAPSHOT,
      providers: [
        item("parcial", "Parcial", {
          windows: {
            five_hour: win("5h", "Janela de 5 h", null, { supported: false }),
            weekly: win("weekly", "Janela semanal", 30),
          },
        }),
      ],
    };
    render(<ProvidersSection telemetry={only5h} />);
    const article = screen.getByRole("article", { name: "Provedor Parcial" });
    const partial = within(article);
    expect(partial.getByText("Não se aplica: este provedor não tem esta janela.")).toBeInTheDocument();
    expect(article.querySelectorAll(".provider-window__bar")).toHaveLength(1);
    expect(partial.getByText("Janela semanal").closest(".provider-window")).toHaveTextContent("30,0%");
    expect(partial.getByText("Janela de 5 h").closest(".provider-window")).not.toHaveTextContent("indisponível");
  });

  it("tokens somados entre perfis mostram a parte de cada perfil, o teto de arquivos e o perfil do contexto", () => {
    const summed: ProvidersTelemetrySnapshot = {
      ...SNAPSHOT,
      providers: [
        item("claude", "Claude", {
          tokens: { ...NO_TOKENS, input_tokens: 1500, total_tokens: 1500, provenance: "estimated" },
          usage_scope: {
            label: "sessões locais mais recentes das últimas 48 h",
            sessions: 6,
            truncated: true,
            tail_truncated: false,
            files_read: 8,
            files_found: 19,
            profiles: [
              { profile: "carvalima", sessions: 4, total_tokens: 1200 },
              { profile: "max", sessions: 2, total_tokens: 300 },
            ],
            note: "transcrições de subagentes não incluídas",
          },
          context: { used_tokens: 900, limit_tokens: null, used_percent: null, model: "claude-test-opus", provenance: "measured", profile: "max" },
        }),
        item("codex", "Codex", {
          tokens: { ...NO_TOKENS, total_tokens: 110, provenance: "measured" },
          usage_scope: {
            label: "sessões locais atualizadas nas últimas 48 h",
            sessions: 1,
            truncated: false,
            tail_truncated: false,
            files_read: 1,
            files_found: 1,
            profiles: [{ profile: "padrao", sessions: 1, total_tokens: 110 }],
          },
        }),
      ],
    };
    render(<ProvidersSection telemetry={summed} />);
    const claude = card("Claude");
    expect(claude.getByText(/Escopo:/)).toHaveTextContent(
      "Escopo: sessões locais mais recentes das últimas 48 h · 6 sessões · soma dos perfis (carvalima: 1.200; max: 300) · " +
        "amostra parcial (8 arquivos mais recentes de 19) · transcrições de subagentes não incluídas"
    );
    expect(claude.getByLabelText("Contexto do último turno")).toHaveTextContent("Perfil: max");
    const codex = card("Codex");
    expect(codex.getByText(/Escopo:/)).toHaveTextContent("Escopo: sessões locais atualizadas nas últimas 48 h · 1 sessão · perfil padrao");
    expect(codex.getByText(/Escopo:/)).not.toHaveTextContent("amostra parcial");
  });

  it("cota esgotada mostra a liberação prevista e as cotas por grupo", () => {
    render(<ProvidersSection telemetry={SNAPSHOT} />);
    const agy = card("Antigravity (AGY)");
    expect(agy.getByText("cota esgotada")).toBeInTheDocument();
    expect(agy.getByText(/Cota esgotada; liberação prevista:/)).toBeInTheDocument();
    expect(agy.getByText(/Cota esgotada no modelo gemini-test-high até/)).toHaveTextContent(
      "outros grupos de modelos podem ter cota."
    );
    expect(agy.getByLabelText("Outras cotas de Antigravity (AGY)")).toHaveTextContent("100,0%");
  });
});
