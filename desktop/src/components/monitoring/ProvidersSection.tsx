import React from "react";
import type {
  ProviderAccountReading,
  ProviderContextReading,
  ProviderLocalSource,
  ProviderTelemetryItem,
  ProvidersTelemetrySnapshot,
  QuotaWindowReading,
} from "./telemetryContract";
import { formatResetPtBR, formatSampleAge, formatUsdPtBR, resolveOriginBadge } from "./telemetryContract";
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

function fillClass(pct: number): string {
  if (pct >= 85) return "provider-window__fill provider-window__fill--high";
  if (pct >= 60) return "provider-window__fill provider-window__fill--mid";
  return "provider-window__fill provider-window__fill--ok";
}

function boundedPercent(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
}

const NO_WINDOW_TEXT = "este provedor não tem esta janela.";
const NO_WINDOWS_TEXT = "este provedor não tem janela de 5 h nem semanal.";

const QuotaWindowRow: React.FC<{ reading: QuotaWindowReading }> = ({ reading }) => {
  if (reading.supported === false) {
    return (
      <div className="provider-window">
        <div className="provider-window__head">
          <span className="provider-window__label">{reading.label}</span>
        </div>
        <p className="provider-card__muted">Não se aplica: {reading.note || NO_WINDOW_TEXT}</p>
      </div>
    );
  }
  const used = formatPercentPtBR(reading.used_percent);
  const rem = formatPercentPtBR(reading.remaining_percent);
  const reset = formatResetPtBR(reading.resets_at_iso);
  const age = reading.observed_at_unix_ms ? formatSampleAge(reading.observed_at_unix_ms, reading.ttl_ms ?? undefined) : null;

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
        <div className={fillClass(boundedPercent(reading.used_percent))} style={{ width: `${boundedPercent(reading.used_percent)}%` }} />
      </div>
      <div className="provider-window__meta">
        <span className={provenanceToneClass(reading.provenance)}>{provenanceBadgeText(reading.provenance)}</span>
        {reading.stale && <span className="badge badge--warn">expirado</span>}
        {reset && <span>Renova: {reset}</span>}
        {reading.profile && <span>Perfil: {reading.profile}</span>}
        {age && <span>Leitura {age.text}</span>}
      </div>
      {reading.note && <p className="provider-card__muted">{reading.note}</p>}
    </div>
  );
};

const ContextRow: React.FC<{ context: ProviderContextReading }> = ({ context }) => {
  const used = formatCountPtBR(context.used_tokens);
  const limit = formatCountPtBR(context.limit_tokens);
  const pct = formatPercentPtBR(context.used_percent);
  return (
    <div className="provider-window" aria-label="Contexto do último turno">
      <div className="provider-window__head">
        <span className="provider-window__label">Contexto (último turno)</span>
        <span className="provider-window__value">
          {used ? `${used}${limit ? ` de ${limit}` : ""} tokens` : "indisponível"}
          {pct ? ` · ${pct}` : ""}
        </span>
      </div>
      {limit && (
        <div className="provider-window__bar" aria-hidden="true">
          <div className={fillClass(boundedPercent(context.used_percent))} style={{ width: `${boundedPercent(context.used_percent)}%` }} />
        </div>
      )}
      <div className="provider-window__meta">
        <span className={provenanceToneClass(context.provenance)}>{provenanceBadgeText(context.provenance)}</span>
        {!limit && <span>Limite da janela indisponível na fonte local</span>}
        {context.model && <span>Modelo: {context.model}</span>}
      </div>
    </div>
  );
};

function accountText(account: ProviderAccountReading): string {
  const fiveHour = formatPercentPtBR(account.windows?.five_hour?.used_percent) ?? "indisponível";
  const weekly = formatPercentPtBR(account.windows?.weekly?.used_percent) ?? "indisponível";
  const age = formatSampleAge(account.observed_at_unix_ms).text;
  const parts = [`5 h: ${fiveHour}`, `semanal: ${weekly}`, `leitura ${age}`];
  if (account.stale) parts.push("expirada");
  if (account.exhausted) {
    const until = formatResetPtBR(account.exhausted_until_iso);
    const scope = account.exhausted_model ? ` no modelo ${account.exhausted_model}` : "";
    parts.push(`cota esgotada${scope}${until ? ` até ${until}` : ""}`);
  }
  return parts.join(" · ");
}

function sourceText(source: ProviderLocalSource): string {
  const state = source.available ? "lida" : "indisponível";
  return `${source.label}: ${state}${source.reason ? ` (${source.reason})` : ""}`;
}

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
  const origin = resolveOriginBadge(item.data_origin);
  const tokens = item.tokens ?? EMPTY_TOKENS;
  const win5h = item.windows?.five_hour ?? EMPTY_WINDOW_5H;
  const winWeekly = item.windows?.weekly ?? EMPTY_WINDOW_WEEKLY;
  const windowsNotApplicable = item.windows_supported === false && win5h.supported === false && winWeekly.supported === false;
  const extraWindows = Array.isArray(item.extra_windows) ? item.extra_windows.slice(0, 6) : [];
  const accounts = Array.isArray(item.accounts) ? item.accounts : [];
  const sources = Array.isArray(item.local_sources) ? item.local_sources : [];
  const models = Array.isArray(item.models) ? item.models : [];
  const scope = item.usage_scope;
  const retry = formatResetPtBR(item.availability_retry_at_iso);
  const modelUntil = formatResetPtBR(item.exhausted_until_iso);

  const tokenParts = [
    tokens.input_tokens !== null ? `Entrada: ${formatCountPtBR(tokens.input_tokens)}` : null,
    tokens.output_tokens !== null ? `Saída: ${formatCountPtBR(tokens.output_tokens)}` : null,
    tokens.cache_read_tokens !== null ? `Cache leit.: ${formatCountPtBR(tokens.cache_read_tokens)}` : null,
    tokens.cache_creation_tokens !== null ? `Cache escr.: ${formatCountPtBR(tokens.cache_creation_tokens)}` : null,
    tokens.reasoning_tokens !== null ? `Raciocínio: ${formatCountPtBR(tokens.reasoning_tokens)}` : null,
    tokens.total_tokens !== null ? `Total: ${formatCountPtBR(tokens.total_tokens)}` : null,
  ].filter(Boolean) as string[];

  const scopeParts = scope
    ? ([
        scope.label,
        typeof scope.sessions === "number"
          ? `${formatCountPtBR(scope.sessions)} ${scope.sessions === 1 ? "sessão" : "sessões"}`
          : null,
        scope.truncated ? "amostra parcial (cauda limitada dos registros)" : null,
      ].filter(Boolean) as string[])
    : [];

  const cc = item.cost_credits;
  const costParts = cc
    ? [
        formatUsdPtBR(cc.balance_usd) ? `Saldo: ${formatUsdPtBR(cc.balance_usd)}` : null,
        formatUsdPtBR(cc.credits_remaining_usd) ? `Créditos: ${formatUsdPtBR(cc.credits_remaining_usd)}` : null,
        formatUsdPtBR(cc.spent_usd) ? `Gasto: ${formatUsdPtBR(cc.spent_usd)}` : null,
        formatUsdPtBR(cc.estimated_cost_usd) ? `Estimado: ${formatUsdPtBR(cc.estimated_cost_usd)}` : null,
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

  const plan = item.plan;
  const planParts = plan
    ? ([
        plan.plan_type ? `Plano: ${plan.plan_type}` : null,
        plan.unlimited === true ? "créditos ilimitados" : null,
        plan.unlimited !== true && typeof plan.credits_balance === "number"
          ? `Créditos: ${formatCountPtBR(plan.credits_balance)}`
          : null,
      ].filter(Boolean) as string[])
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

      {windowsNotApplicable ? (
        <div className="provider-window">
          <div className="provider-window__head">
            <span className="provider-window__label">Janelas de 5 h e semanal</span>
          </div>
          <p className="provider-card__detail">Não se aplica: {win5h.note || NO_WINDOWS_TEXT}</p>
        </div>
      ) : (
        <div className="provider-card__windows">
          <QuotaWindowRow reading={win5h} />
          <QuotaWindowRow reading={winWeekly} />
        </div>
      )}

      {extraWindows.length > 0 && (
        <div className="provider-card__windows" aria-label={`Outras cotas de ${item.display_name}`}>
          {extraWindows.map((reading) => (
            <QuotaWindowRow key={`${reading.profile ?? ""}-${reading.window}-${reading.label}`} reading={reading} />
          ))}
        </div>
      )}

      {item.context && <ContextRow context={item.context} />}

      <div className="provider-card__section">
        <div className="provider-card__rowhead">
          <span>Tokens</span>
          <span className={provenanceToneClass(tokens.provenance)}>{provenanceBadgeText(tokens.provenance)}</span>
        </div>
        <p className="provider-card__detail">
          {tokenParts.length > 0 ? tokenParts.join(" · ") : "Sem tokens reportados nas fontes locais."}
        </p>
        {scopeParts.length > 0 && <p className="provider-card__muted">Escopo: {scopeParts.join(" · ")}</p>}
      </div>

      {(costParts.length > 0 || apiParts.length > 0 || planParts.length > 0 || item.local_limit) && (
        <div className="provider-card__section">
          {costParts.length > 0 && (
            <p className="provider-card__detail">
              <strong>Custos/Créditos:</strong> {costParts.join(" · ")} (
              {provenanceBadgeText(cc?.provenance)})
            </p>
          )}
          {planParts.length > 0 && (
            <p className="provider-card__detail">
              <strong>Assinatura:</strong> {planParts.join(" · ")} ({provenanceBadgeText(plan?.provenance)})
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

      {accounts.length > 1 && (
        <div className="provider-card__section">
          <div className="provider-card__rowhead">
            <span>Perfis locais (sem soma entre perfis)</span>
          </div>
          <ul className="provider-card__accounts">
            {accounts.map((account) => (
              <li key={account.profile} className="provider-card__detail">
                <strong>{account.profile}</strong>: {accountText(account)}
              </li>
            ))}
          </ul>
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

      {sources.length > 0 && (
        <p className="provider-card__muted">Fontes locais (somente leitura): {sources.map(sourceText).join(" · ")}</p>
      )}
      {item.remote_lookup && !item.remote_lookup.enabled && (
        <p className="provider-card__muted">Consulta remota: desligada (opt-in). {item.remote_lookup.note ?? ""}</p>
      )}

      <div className="provider-card__foot">
        <span>{item.has_vault_key ? "Chave no cofre: sim" : "Chave no cofre: não"}</span>
        {item.launcher_available !== null && (
          <span>{item.launcher_available ? "Launcher: encontrado" : "Launcher: ausente"}</span>
        )}
        <span>Origem: {origin.text}</span>
        <span>Leitura: {age.text}</span>
      </div>
      {item.availability_detail && <p className="provider-card__note">{item.availability_detail}</p>}
      {item.availability_status === "quota_exhausted" && retry && (
        <p className="provider-card__note">Cota esgotada; liberação prevista: {retry}.</p>
      )}
      {item.exhausted_model && (
        <p className="provider-card__note">
          Cota esgotada no modelo {item.exhausted_model}
          {modelUntil ? ` até ${modelUntil}` : ""}; outros grupos de modelos podem ter cota.
        </p>
      )}
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
