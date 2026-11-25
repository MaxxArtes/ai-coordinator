/**
 * Consolidated telemetry contracts for AI Coordinator Desktop (Phase 4D.4.1 – 4D.4.4).
 *
 * Strict provenance & origin rules:
 * - MetricProvenance: "measured" | "estimated" | "unknown"
 * - DataOrigin: "live_os" | "live_network" | "offline_cache" | "test_fixture"
 * - Test fixtures are always clearly separated and never labeled as live production readings.
 */

export type MetricProvenance = "measured" | "estimated" | "unknown";
export type DataOrigin = "live_os" | "live_network" | "offline_cache" | "test_fixture";
export type ProcessRole = "host" | "core" | "webview" | "terminal" | "agent" | "child";

export interface StorageVolumeMetrics {
  name: string;
  mount_point: string;
  total_bytes: number;
  used_bytes: number;
  available_bytes: number;
  usage_percent: number | null;
}

export interface SystemResourcesMetrics {
  logical_cpu_count: number;
  cpu_usage_percent?: number | null;
  cpu_provenance?: MetricProvenance;
  ram_total_bytes?: number;
  ram_used_bytes?: number;
  ram_available_bytes?: number;
  ram_usage_percent?: number | null;
  ram_provenance?: MetricProvenance;
  storage_total_bytes?: number;
  storage_used_bytes?: number;
  storage_available_bytes?: number;
  storage_usage_percent?: number | null;
  storage_provenance?: MetricProvenance;
  storage_volumes?: StorageVolumeMetrics[];
  disk_activity_percent?: number | null;
  disk_activity_provenance?: MetricProvenance;
  disk_read_bytes_per_sec?: number | null;
  disk_write_bytes_per_sec?: number | null;
  disk_io_provenance?: MetricProvenance;
  network_rx_bytes_per_sec?: number | null;
  network_tx_bytes_per_sec?: number | null;
  network_total_bytes_per_sec?: number | null;
  network_provenance?: MetricProvenance;
  gpu_usage_percent?: number | null;
  gpu_provenance?: MetricProvenance;
}

export interface ProcessNodeMetrics {
  pid: number;
  ppid: number | null;
  start_time?: number;
  create_time_ns?: number | null;
  role: ProcessRole | string;
  name: string;
  ram_bytes: number | null;
  ram_provenance: MetricProvenance;
  private_ram_bytes?: number | null;
  private_ram_provenance?: MetricProvenance;
  working_set_ram_bytes?: number | null;
  shared_ram_bytes?: number | null;
  cpu_usage_percent: number | null;
  cpu_provenance: MetricProvenance;
  agent_kind?: string | null;
  session_id?: string | null;
}

export interface CategoryResourceMetrics {
  role: ProcessRole | string;
  process_count: number;
  ram_bytes: number;
  ram_provenance: MetricProvenance;
  private_ram_bytes?: number | null;
  private_ram_provenance?: MetricProvenance;
  working_set_ram_bytes?: number;
  shared_ram_bytes?: number | null;
  cpu_usage_percent: number | null;
  cpu_provenance: MetricProvenance;
}

export interface ProcessTreeCategories {
  host: CategoryResourceMetrics;
  core: CategoryResourceMetrics;
  webview: CategoryResourceMetrics;
  terminal: CategoryResourceMetrics;
  agent?: CategoryResourceMetrics;
  child: CategoryResourceMetrics;
}

export interface RuntimeMetrics {
  schema_version?: number;
  data_origin?: DataOrigin | string;
  ttl_ms?: number;
  stale?: boolean;
  host_pid: number;
  core_pid?: number | null;
  host_ram_bytes: number;
  host_private_ram_bytes?: number | null;
  total_tree_ram_bytes: number;
  ram_provenance?: MetricProvenance;
  total_private_ram_bytes?: number | null;
  private_ram_provenance?: MetricProvenance;
  total_working_set_ram_bytes?: number;
  total_shared_ram_bytes?: number | null;
  shared_ram_note?: string;
  cpu_usage_percent?: number | null;
  cpu_provenance?: MetricProvenance;
  logical_cpu_count?: number;
  sample_interval_ms?: number | null;
  process_count?: number;
  sampled_at_unix_ms?: number;
  categories?: ProcessTreeCategories;
  system?: SystemResourcesMetrics | null;
  nodes?: ProcessNodeMetrics[];
}

export type ProviderAvailabilityStatus =
  | "available"
  | "cooldown"
  | "quota_exhausted"
  | "auth_error"
  | "rate_limit"
  | "offline"
  | "unknown";

export type QuotaCategoryKind =
  | "subscription_quota"
  | "api_rate_limit"
  | "financial_credit"
  | "local_limit";

export interface QuotaWindowReading {
  window: "5h" | "weekly" | string;
  label: string;
  used_percent: number | null;
  remaining_percent: number | null;
  resets_at_iso: string | null;
  provenance: MetricProvenance;
  data_origin?: DataOrigin | string;
  observed_at_unix_ms?: number | null;
  ttl_ms?: number | null;
  stale?: boolean;
  /** false: the provider has no such window (OpenCode, OpenRouter); shown as "não se aplica", never as an empty bar. */
  supported?: boolean;
  note?: string | null;
  group?: string | null;
  profile?: string | null;
  disabled?: boolean;
}

/** Context of the last turn read from local session logs. `limit_tokens` stays null when the source does not say it. */
export interface ProviderContextReading {
  used_tokens: number | null;
  limit_tokens: number | null;
  used_percent: number | null;
  model?: string | null;
  provenance: MetricProvenance;
  observed_at_unix_ms?: number | null;
  /** Local profile whose session gave the last turn. */
  profile?: string | null;
}

export interface ProviderPlanReading {
  plan_type: string | null;
  has_credits: boolean | null;
  unlimited: boolean | null;
  credits_balance: number | null;
  provenance: MetricProvenance;
}

/** One local profile/account of the same CLI (carvalima, max, padrao); nothing is summed between profiles. */
export interface ProviderAccountReading {
  profile: string;
  windows: { five_hour: QuotaWindowReading; weekly: QuotaWindowReading };
  observed_at_unix_ms: number | null;
  stale: boolean;
  exhausted?: boolean;
  exhausted_until_iso?: string | null;
  exhausted_model?: string | null;
}

/** A local, read-only source and whether it could be read; never a path or a value from the file. */
export interface ProviderLocalSource {
  kind: string;
  label: string;
  available: boolean;
  reason?: string | null;
  observed_at_unix_ms?: number | null;
}

/** One profile's share of the token totals (the totals add up every listed profile). */
export interface ProviderUsageProfileShare {
  profile: string;
  sessions: number;
  total_tokens: number | null;
}

export interface ProviderUsageScope {
  label: string;
  lookback_hours?: number | null;
  sessions?: number | null;
  /** Partial sample: a cut tail, or more session files than the reader takes per cycle. */
  truncated?: boolean;
  tail_truncated?: boolean;
  files_read?: number | null;
  files_found?: number | null;
  profiles?: ProviderUsageProfileShare[];
  note?: string | null;
}

export interface TokenUsageSummary {
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_creation_tokens: number | null;
  reasoning_tokens: number | null;
  total_tokens: number | null;
  provenance: MetricProvenance;
  runs_count?: number;
}

export interface ProviderModelTelemetry {
  model: string;
  tokens: TokenUsageSummary;
  cost_usd: number | null;
  cost_provenance: MetricProvenance;
  status?: ProviderAvailabilityStatus | string;
}

export interface ProviderCostCreditSnapshot {
  kind: QuotaCategoryKind | string;
  currency: "USD" | string;
  balance_usd: number | null;
  credits_remaining_usd: number | null;
  spent_usd: number | null;
  estimated_cost_usd: number | null;
  provenance: MetricProvenance;
  note?: string | null;
}

export interface ProviderApiRateLimitSnapshot {
  requests_remaining: number | null;
  requests_limit: number | null;
  tokens_remaining: number | null;
  tokens_limit: number | null;
  resets_at_iso: string | null;
  provenance: MetricProvenance;
}

export interface ProviderTelemetryItem {
  provider: string;
  display_name: string;
  profile_name: string;
  launcher_available: boolean | null;
  has_vault_key: boolean;
  availability_status: ProviderAvailabilityStatus | string;
  availability_detail?: string | null;
  availability_retry_at_iso?: string | null;
  quota_kind: QuotaCategoryKind | string;
  windows: {
    five_hour: QuotaWindowReading;
    weekly: QuotaWindowReading;
  };
  api_limits?: ProviderApiRateLimitSnapshot | null;
  cost_credits?: ProviderCostCreditSnapshot | null;
  local_limit?: Record<string, unknown> | null;
  tokens: TokenUsageSummary;
  coordinator_tokens?: TokenUsageSummary | null;
  usage_scope?: ProviderUsageScope | null;
  context?: ProviderContextReading | null;
  plan?: ProviderPlanReading | null;
  windows_supported?: boolean;
  extra_windows?: QuotaWindowReading[];
  accounts?: ProviderAccountReading[];
  local_sources?: ProviderLocalSource[];
  local_available?: boolean;
  remote_lookup?: { enabled: boolean; note?: string | null } | null;
  /** A quota mark scoped to one model (AGY 429): other model groups may still have quota. */
  exhausted_model?: string | null;
  exhausted_until_iso?: string | null;
  last_activity_unix_ms?: number | null;
  models: ProviderModelTelemetry[];
  data_origin: DataOrigin | string;
  observed_at_unix_ms: number | null;
  ttl_ms: number | null;
  stale: boolean;
}

export interface ProvidersTelemetrySnapshot {
  schema_version: number;
  profile_name: string;
  sampled_at_unix_ms: number;
  ttl_ms: number;
  stale: boolean;
  data_origin: DataOrigin | string;
  provenance: MetricProvenance;
  sources: {
    sqlite_available: boolean;
    quota_snapshot_available: boolean;
    vault_keys_checked: boolean;
    local_sources_checked?: boolean;
    local_providers?: Record<string, boolean>;
    local_read_at_unix_ms?: number | null;
    /** The local reader answers from its cache until this long after local_read_at_unix_ms. */
    local_cache_ttl_ms?: number | null;
  };
  providers: ProviderTelemetryItem[];
  notice?: string | null;
}

export function resolveOriginBadge(origin?: string): { text: string; className: string; isFixture: boolean } {
  switch (origin) {
    case "live_os":
      return { text: "ao vivo (SO)", className: "badge badge--ok", isFixture: false };
    case "live_network":
      return { text: "ao vivo (rede)", className: "badge badge--ok", isFixture: false };
    case "offline_cache":
      return { text: "cache offline", className: "badge badge--warn", isFixture: false };
    case "test_fixture":
      return { text: "Fixture de teste (não produção)", className: "badge badge--warn", isFixture: true };
    default:
      return { text: "origem não informada", className: "badge badge--unknown", isFixture: false };
  }
}

const USD_FMT = new Intl.NumberFormat("pt-BR", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

export function formatUsdPtBR(val: number | null | undefined): string | null {
  if (typeof val !== "number" || !Number.isFinite(val) || val < 0) return null;
  return USD_FMT.format(val);
}

export function formatResetPtBR(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  if (!Number.isFinite(parsed)) return null;
  return new Date(parsed).toLocaleString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatSampleAge(
  sampledAtUnixMs?: number | null,
  ttlMs?: number | null,
  nowMs: number = Date.now()
): { text: string; expired: boolean; ageSeconds: number | null } {
  if (typeof sampledAtUnixMs !== "number" || !Number.isFinite(sampledAtUnixMs) || sampledAtUnixMs <= 0) {
    return { text: "sem horário registrado", expired: false, ageSeconds: null };
  }
  const ageMs = Math.max(0, nowMs - sampledAtUnixMs);
  const ageSeconds = Math.round(ageMs / 1000);
  const expired = typeof ttlMs === "number" && ttlMs > 0 ? ageMs > ttlMs : false;
  if (ageSeconds <= 2) return { text: "agora", expired, ageSeconds };
  if (ageSeconds < 60) return { text: `há ${ageSeconds} s`, expired, ageSeconds };
  const mins = Math.floor(ageSeconds / 60);
  return { text: `há ${mins} min`, expired, ageSeconds };
}
