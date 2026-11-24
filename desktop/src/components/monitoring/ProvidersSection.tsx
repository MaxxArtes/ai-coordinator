import React from "react";
import type {
  ProviderTelemetryItem,
  ProvidersTelemetrySnapshot,
  QuotaWindowReading,
} from "./telemetryContract";
import { formatSampleAge, resolveOriginBadge } from "./telemetryContract";
import { formatCountPtBR, formatPercentPtBR, provenanceBadgeText, provenanceToneClass } from "./history";

export interface ProvidersSectionProps {
  telemetry: ProvidersTelemetrySnapshot | null;
  loading?: boolean;
  error?: string | null;
  onRefresh?: () => void;
}

const AVAILABILITY_LABEL: Record<string, { text: string; className: string }> = {
  available: { text: "disponível", className: "badge badge--ok" },
  cooldown: { text: "em pausa", className: "badge badge--warn" },
  quota_exhausted: { text: "cota esgotada", className: "badge badge--err" },
  rate_limit: { text: "limite de taxa", className: "badge badge--warn" },
  auth_error: { text: "erro de autenticação", className: "badge badge--err" },
  offline: { text: "offline", className: "badge badge--err" },
  unknown: { text: "não verificado", className: "badge badge--unknown" },
};

const QUOTA_KIND_LABEL: Record<string, string> = {
  subscription_quota: "Cota de assinatura",
  api_rate_limit: "Limite de API",
  financial_credit: "Créditos e custos",
  local_limit: "Limite local",
};

const USD_FMT = new Intl.NumberFormat("pt-BR", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

function formatUsd(val: number | null | undefined): string | null {
  if (typeof val !== "number" || !Number.isFinite(val) || val < 0) return null;
  return USD_FMT.format(val);
}

function formatIsoShort(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  if (!Number.isFinite(parsed)) return iso;
  return new Date(parsed).toLocaleString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const QuotaWindowRow: React.FC<{ reading: QuotaWindowReading }> = ({ reading }) => {
  const used = formatPercentPtBR(reading.used_percent);
  const rem = formatPercentPtBR(reading.remaining_percent);
  const reset = formatIsoShort(reading.resets_at_iso);
  const pctWidth =
    typeof reading.used_percent === "number" && Number.isFinite(reading.used_percent)
      ? Math.max(0, Math.min(100, reading.used_percent))
      : 0;

  return (
    <div className="provider-window">
      <div className="provider-window__head">
        <span className="provider-window__label">{reading.label}</span>
        <span className="provider-window__value">
          {used ?? "indisponível"}
          {rem ? ` · restante ${rem}` : ""}
        </span>
      </div>
      <div className="provider-window__bar" aria-hidden="true">
        <div
          className={`provider-window__fill ${
            pctWidth >= 85
              ? "provider-window__fill--high"
              : pctWidth >= 60
                ? "provider-window__fill--mid"
                : "provider-window__fill--ok"
          }`}
          style={{ width: `${pctWidth}%` }}
        />
      </div>
      <div className="provider-window__meta">
        <span className={provenanceToneClass(reading.provenance)}>{provenanceBadgeText(reading.provenance)}</span>
        {reading.stale && <span className="badge badge--warn">expirado</span>}
        {reset && <span>Renova: {reset}</span>}
      </div>
    </div>
  );
};

const EMPTY_TOKENS: ProviderTelemetryItem["tokens"] = {
  input_tokens: null,
  output_tokens: null,
  cache_read_tokens: null,
  cache_creation_tokens: null,
  reasoning_tokens: null,
  total_tokens: null,
  provenance: "unknown",
};

const EMPTY_WINDOW_5H: QuotaWindowReading = {
  window: "5h",
  label: "Janela de 5 h",
  used_percent: null,
  remaining_percent: null,
  resets_at_iso: null,
  provenance: "unknown",
};

const EMPTY_WINDOW_WEEKLY: QuotaWindowReading = {
  window: "weekly",
  label: "Janela semanal",
  used_percent: null,
  remaining_percent: null,
  resets_at_iso: null,
  provenance: "unknown",
};

const ProviderCard: React.FC<{ item: ProviderTelemetryItem }> = ({ item }) => {
  const avail = AVAILABILITY_LABEL[item.availability_status] ?? AVAILABILITY_LABEL.unknown;
  const kindLabel = QUOTA_KIND_LABEL[item.quota_kind] ?? item.quota_kind;
  const age = formatSampleAge(item.observed_at_unix_ms, item.ttl_ms ?? undefined);
  const tokens = item.tokens ?? EMPTY_TOKENS;
  const win5h = item.windows?.five_hour ?? EMPTY_WINDOW_5H;
  const winWeekly = item.windows?.weekly ?? EMPTY_WINDOW_WEEKLY;
  const models = Array.isArray(item.models) ? item.models : [];

  const tokenParts = [
    tokens.input_tokens !== null ? `Entrada: ${formatCountPtBR(tokens.input_tokens)}` : null,
    tokens.output_tokens !== null ? `Saída: ${formatCountPtBR(tokens.output_tokens)}` : null,
    tokens.cache_read_tokens !== null ? `Cache leit.: ${formatCountPtBR(tokens.cache_read_tokens)}` : null,
    tokens.cache_creation_tokens !== null ? `Cache escr.: ${formatCountPtBR(tokens.cache_creation_tokens)}` : null,
    tokens.reasoning_tokens !== null ? `Raciocínio: ${formatCountPtBR(tokens.reasoning_tokens)}` : null,
    tokens.total_tokens !== null ? `Total: ${formatCountPtBR(tokens.total_tokens)}` : null,
  ].filter(Boolean) as string[];

  const cc = item.cost_credits;
  const costParts = cc
    ? [
        formatUsd(cc.balance_usd) ? `Saldo: ${formatUsd(cc.balance_usd)}` : null,
        formatUsd(cc.credits_remaining_usd) ? `Créditos: ${formatUsd(cc.credits_remaining_usd)}` : null,
        formatUsd(cc.spent_usd) ? `Gasto: ${formatUsd(cc.spent_usd)}` : null,
        formatUsd(cc.estimated_cost_usd) ? `Estimado: ${formatUsd(cc.estimated_cost_usd)}` : null,
      ].filter(Boolean) as string[]
    : [];

  const al = item.api_limits;
  const apiParts = al
    ? [
        al.requests_remaining !== null && al.requests_limit !== null
          ? `Req.: ${formatCountPtBR(al.requests_remaining)}/${formatCountPtBR(al.requests_limit)}`
          : null,
        al.tokens_remaining !== null && al.tokens_limit !== null
          ? `Tokens API: ${formatCountPtBR(al.tokens_remaining)}/${formatCountPtBR(al.tokens_limit)}`
          : null,
      ].filter(Boolean) as string[]
    : [];

  return (
    <article className="provider-card" aria-label={`Provedor ${item.display_name}`}>
      <div className="provider-card__head">
        <div>
          <h4 className="provider-card__title">{item.display_name}</h4>
          <span className="provider-card__sub">
            Perfil ativo: <strong>{item.profile_name}</strong> · {kindLabel}
          </span>
        </div>
        <div className="provider-card__badges">
          <span className={avail.className}>{avail.text}</span>
          {item.stale && <span className="badge badge--warn">expirado</span>}
        </div>
      </div>

      <div className="provider-card__windows">
        <QuotaWindowRow reading={win5h} />
        <QuotaWindowRow reading={winWeekly} />
      </div>

      <div className="provider-card__section">
        <div className="provider-card__rowhead">
          <span>Tokens</span>
          <span className={provenanceToneClass(tokens.provenance)}>{provenanceBadgeText(tokens.provenance)}</span>
        </div>
        <p className="provider-card__detail">
          {tokenParts.length > 0 ? tokenParts.join(" · ") : "Sem tokens reportados nas fontes locais."}
        </p>
      </div>

      {(costParts.length > 0 || apiParts.length > 0 || item.local_limit) && (
        <div className="provider-card__section">
          {costParts.length > 0 && (
            <p className="provider-card__detail">
              <strong>Custos/Créditos:</strong> {costParts.join(" · ")} (
              {provenanceBadgeText(cc?.provenance)})
            </p>
          )}
          {apiParts.length > 0 && (
            <p className="provider-card__detail">
              <strong>Limites de API:</strong> {apiParts.join(" · ")} (
              {provenanceBadgeText(al?.provenance)})
            </p>
          )}
          {item.local_limit && (
            <p className="provider-card__detail">
              <strong>Limite local (orçamento salvo):</strong> até{" "}
              {String((item.local_limit as Record<string, unknown>).max_steps ?? "—")} passos (não aplicado até 4D.5)
            </p>
          )}
        </div>
      )}

      {models.length > 0 && (
        <div className="provider-card__models">
          {models.slice(0, 4).map((m) => (
            <span key={m.model} className="badge" title={`Tokens: ${m.tokens?.total_tokens ?? "indisponível"}`}>
              {m.model}
              {m.tokens && m.tokens.total_tokens !== null ? ` (${formatCountPtBR(m.tokens.total_tokens)})` : ""}
            </span>
          ))}
        </div>
      )}

      <div className="provider-card__foot">
        <span>{item.has_vault_key ? "Chave no cofre: sim" : "Chave no cofre: não"}</span>
        {item.launcher_available !== null && (
          <span>{item.launcher_available ? "Launcher: encontrado" : "Launcher: ausente"}</span>
        )}
        <span>Leitura: {age.text}</span>
      </div>
      {item.availability_detail && <p className="provider-card__note">{item.availability_detail}</p>}
    </article>
  );
};

export const ProvidersSection: React.FC<ProvidersSectionProps> = ({
  telemetry,
  loading = false,
  error = null,
  onRefresh,
}) => {
  const origin = telemetry ? resolveOriginBadge(telemetry.data_origin) : null;
  const age = telemetry ? formatSampleAge(telemetry.sampled_at_unix_ms, telemetry.ttl_ms) : null;

  return (
    <section className="monitoring-section" aria-label="Provedores de Inteligência Artificial">
      <div className="monitoring-section__head">
        <div>
          <h3 className="monitoring-section__title">Provedores de Inteligência Artificial</h3>
          <span className="monitoring-panel__meta">
            Monitoramento por provedor, perfil, modelo, cotas (5 h e semanal), tokens, custos e disponibilidade
            {age ? ` · leitura ${age.text}` : ""}.
          </span>
        </div>
        <div className="provider-section__controls">
          {origin && <span className={origin.className}>{origin.text}</span>}
          {telemetry?.stale && <span className="badge badge--warn">dados expirados</span>}
          {onRefresh && (
            <button type="button" className="btn btn--sm" onClick={onRefresh} aria-disabled={loading || undefined}>
              {loading ? "Atualizando…" : "Atualizar provedores"}
            </button>
          )}
        </div>
      </div>

      {origin?.isFixture && (
        <div className="monitoring-alert monitoring-alert--warn" role="status">
          <div className="monitoring-alert__body">
            <strong className="monitoring-alert__title">Provedores em modo Fixture de teste</strong>
            <span className="monitoring-alert__detail">
              Origem declarada como test_fixture; valores nunca são tratados como medições reais de produção.
            </span>
          </div>
        </div>
      )}

      {telemetry?.notice && (
        <div className="monitoring-alert monitoring-alert--warn" role="status">
          <div className="monitoring-alert__body">
            <strong className="monitoring-alert__title">Aviso na leitura de cotas locais</strong>
            <span className="monitoring-alert__detail">{telemetry.notice}</span>
          </div>
        </div>
      )}

      {error && (
        <p className="monitoring-empty" role="alert">
          Falha ao consultar telemetria de provedores: {error}
        </p>
      )}

      {telemetry && telemetry.providers.length > 0 ? (
        <div className="providers-grid">
          {telemetry.providers.map((item) => (
            <ProviderCard key={item.provider} item={item} />
          ))}
        </div>
      ) : !error ? (
        <p className="monitoring-empty">
          Nenhuma leitura de provedores recebida ainda. Credenciais armazenadas não comprovam conexão ou saldo.
        </p>
      ) : null}
    </section>
  );
};
