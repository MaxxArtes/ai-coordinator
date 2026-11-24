"""Read-only AI provider telemetry collector for Phase 4D.4.3 & 4D.4.4.

Consolidates real local sources without making paid network calls, hitting undocumented private endpoints,
or exposing credentials:
1. Launcher & route discovery (`desktop.ipc.discovery`)
2. Profile vault key-name presence (`has_vault_key` boolean only, never reading secret values)
3. Coordinator settings local mission budget (`local_limit`, marked unapplied until 4D.5)
4. Coordinator SQLite (`coordination.sqlite` read-only): `route_health`, `runs`, `cognitive_telemetry`
5. Optional validated local quota snapshot (`provider_quotas.json`), strictly honoring provenance,
   `data_origin` (`live_network` | `offline_cache` | `test_fixture`), TTL, and expiration (`stale`).

Missing metrics always remain `None` with `provenance="unknown"` — never invented.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ai_coordinator.telemetry import scrub_sensitive_text
from desktop.ipc.protocol import DataOrigin, MetricProvenance, TELEMETRY_SCHEMA_VERSION

DEFAULT_PROVIDER_TTL_MS = 300_000  # 5 minutes default freshness window for provider telemetry
SAFE_PROVIDER_ID = re.compile(r"^[a-z0-9_-]{2,40}$")

# Canonical providers inventoried across Core contracts & Desktop routes
CANONICAL_PROVIDERS: Tuple[Dict[str, Any], ...] = (
    {
        "provider": "claude",
        "display_name": "Claude",
        "quota_kind": "subscription_quota",
        "vault_keys": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        "agent_client": "claude",
    },
    {
        "provider": "codex",
        "display_name": "Codex",
        "quota_kind": "subscription_quota",
        "vault_keys": ("OPENAI_API_KEY", "CODEX_API_KEY"),
        "agent_client": "codex",
    },
    {
        "provider": "agy",
        "display_name": "Antigravity (AGY)",
        "quota_kind": "subscription_quota",
        "vault_keys": ("AGY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "agent_client": "agy",
    },
    {
        "provider": "opencode",
        "display_name": "OpenCode",
        "quota_kind": "api_rate_limit",
        "vault_keys": ("OPENCODE_API_KEY",),
        "agent_client": "opencode",
    },
    {
        "provider": "openrouter",
        "display_name": "OpenRouter",
        "quota_kind": "financial_credit",
        "vault_keys": ("OPENROUTER_API_KEY",),
        "agent_client": None,
    },
)

_CANONICAL_BY_ID = {item["provider"]: item for item in CANONICAL_PROVIDERS}

_VALID_QUOTA_KINDS = {"subscription_quota", "api_rate_limit", "financial_credit", "local_limit"}
_VALID_ORIGINS = {o.value for o in DataOrigin}
_VALID_PROVENANCES = {p.value for p in MetricProvenance}


def _finite_nonneg_float(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    if not math.isfinite(f) or f < 0.0:
        return None
    return f


def _finite_nonneg_int(val: Any) -> Optional[int]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    if not math.isfinite(f) or f < 0.0:
        return None
    return int(round(f))


def _valid_percent(val: Any) -> Optional[float]:
    num = _finite_nonneg_float(val)
    if num is None or num > 100.0:
        return None
    return round(num, 2)


def _valid_provenance(val: Any, default: str = MetricProvenance.UNKNOWN.value) -> str:
    if isinstance(val, str) and val in _VALID_PROVENANCES:
        return val
    return default


def _valid_origin(val: Any, default: str = DataOrigin.OFFLINE_CACHE.value) -> str:
    if isinstance(val, str) and val in _VALID_ORIGINS:
        return val
    return default


def _epoch_to_iso(epoch_sec: Any) -> Optional[str]:
    num = _finite_nonneg_float(epoch_sec)
    if num is None or num <= 0:
        return None
    try:
        return dt.datetime.fromtimestamp(num, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _unknown_window(window_name: str, label: str, origin: str = DataOrigin.OFFLINE_CACHE.value) -> Dict[str, Any]:
    return {
        "window": window_name,
        "label": label,
        "used_percent": None,
        "remaining_percent": None,
        "resets_at_iso": None,
        "provenance": MetricProvenance.UNKNOWN.value,
        "data_origin": origin,
        "observed_at_unix_ms": None,
        "ttl_ms": None,
        "stale": False,
    }


def _parse_quota_window(
    raw: Any,
    window_name: str,
    label: str,
    default_origin: str,
    default_observed_ms: Optional[int],
    default_ttl_ms: int,
    now_ms: int,
) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return _unknown_window(window_name, label, default_origin)

    used = _valid_percent(raw.get("used_percent"))
    remaining = _valid_percent(raw.get("remaining_percent"))
    if used is not None and remaining is None:
        remaining = round(max(0.0, 100.0 - used), 2)
    elif remaining is not None and used is None:
        used = round(max(0.0, 100.0 - remaining), 2)

    if used is None and remaining is None:
        return _unknown_window(window_name, label, _valid_origin(raw.get("data_origin"), default_origin))

    prov = _valid_provenance(raw.get("provenance"), MetricProvenance.MEASURED.value)
    if prov == MetricProvenance.UNKNOWN.value:
        return _unknown_window(window_name, label, _valid_origin(raw.get("data_origin"), default_origin))

    obs_ms = _finite_nonneg_int(raw.get("observed_at_unix_ms")) or default_observed_ms
    ttl_ms = _finite_nonneg_int(raw.get("ttl_ms")) or default_ttl_ms
    stale = bool(obs_ms is not None and ttl_ms > 0 and (now_ms - obs_ms) > ttl_ms)
    resets = raw.get("resets_at_iso") if isinstance(raw.get("resets_at_iso"), str) else None
    if resets:
        resets = scrub_sensitive_text(resets)[:40]

    return {
        "window": window_name,
        "label": label,
        "used_percent": used,
        "remaining_percent": remaining,
        "resets_at_iso": resets,
        "provenance": prov,
        "data_origin": _valid_origin(raw.get("data_origin"), default_origin),
        "observed_at_unix_ms": obs_ms,
        "ttl_ms": ttl_ms,
        "stale": stale,
    }


def _provider_of_run(client: Optional[str], model: Optional[str]) -> str:
    c = (client or "").strip().lower()
    m = (model or "").strip()
    if c == "opencode" and "/" in m:
        prefix = m.split("/", 1)[0].strip().lower()
        if SAFE_PROVIDER_ID.fullmatch(prefix):
            return prefix
    if SAFE_PROVIDER_ID.fullmatch(c):
        return c
    return "unknown"


def _empty_token_bucket() -> Dict[str, Any]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "reasoning_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "runs_count": 0,
        "has_cost": False,
        "any_estimated": False,
    }


def _add_opt(bucket: Dict[str, Any], key: str, value: Optional[int]) -> None:
    if value is None:
        return
    bucket[key] = (bucket[key] or 0) + value


def _accumulate_usage_dict(bucket: Dict[str, Any], usage: Dict[str, Any]) -> bool:
    """Extract normalized token/cost metrics from one run's usage dict. Returns True if any token/cost metric found."""
    in_t = _finite_nonneg_int(usage.get("input_tokens") if "input_tokens" in usage else usage.get("prompt_tokens"))
    out_t = _finite_nonneg_int(usage.get("output_tokens") if "output_tokens" in usage else usage.get("completion_tokens"))
    cr_t = _finite_nonneg_int(
        usage.get("cache_read_tokens")
        if "cache_read_tokens" in usage
        else usage.get("cache_read_input_tokens")
        if "cache_read_input_tokens" in usage
        else usage.get("cached_tokens")
    )
    cc_t = _finite_nonneg_int(
        usage.get("cache_creation_tokens")
        if "cache_creation_tokens" in usage
        else usage.get("cache_creation_input_tokens")
    )
    reas_t = _finite_nonneg_int(
        usage.get("reasoning_tokens")
        if "reasoning_tokens" in usage
        else usage.get("thinking_tokens")
    )
    tot_t = _finite_nonneg_int(usage.get("total_tokens"))
    cost_v = _finite_nonneg_float(
        usage.get("total_cost_usd") if "total_cost_usd" in usage else usage.get("cost")
    )

    # Check attempt-level usage only if top-level tokens are completely absent (prevent double-counting)
    if all(x is None for x in (in_t, out_t, cr_t, cc_t, reas_t, tot_t)) and isinstance(usage.get("iterations"), list):
        for it in usage["iterations"]:
            if isinstance(it, dict):
                _accumulate_usage_dict(bucket, it)

    if tot_t is None and (in_t is not None or out_t is not None):
        tot_t = (in_t or 0) + (out_t or 0) + (cr_t or 0) + (cc_t or 0) + (reas_t or 0)

    found = any(x is not None for x in (in_t, out_t, cr_t, cc_t, reas_t, tot_t, cost_v))
    _add_opt(bucket, "input_tokens", in_t)
    _add_opt(bucket, "output_tokens", out_t)
    _add_opt(bucket, "cache_read_tokens", cr_t)
    _add_opt(bucket, "cache_creation_tokens", cc_t)
    _add_opt(bucket, "reasoning_tokens", reas_t)
    _add_opt(bucket, "total_tokens", tot_t)
    if cost_v is not None:
        bucket["cost_usd"] = round((bucket["cost_usd"] or 0.0) + cost_v, 6)
        bucket["has_cost"] = True
    return found


def _finalize_tokens(bucket: Dict[str, Any]) -> Dict[str, Any]:
    any_tokens = any(
        bucket[k] is not None
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "reasoning_tokens", "total_tokens")
    )
    if not any_tokens:
        prov = MetricProvenance.UNKNOWN.value
    elif bucket["any_estimated"]:
        prov = MetricProvenance.ESTIMATED.value
    else:
        prov = MetricProvenance.MEASURED.value
    return {
        "input_tokens": bucket["input_tokens"],
        "output_tokens": bucket["output_tokens"],
        "cache_read_tokens": bucket["cache_read_tokens"],
        "cache_creation_tokens": bucket["cache_creation_tokens"],
        "reasoning_tokens": bucket["reasoning_tokens"],
        "total_tokens": bucket["total_tokens"],
        "provenance": prov,
        "runs_count": bucket["runs_count"],
    }


def _map_health_status(kind: str, retry_at: Optional[float], expires: Optional[float], now_sec: float) -> str:
    k = (kind or "").strip().lower()
    if k == "success":
        return "available" if (expires is None or expires <= 0 or now_sec <= expires) else "unknown"
    if k == "quota":
        return "quota_exhausted" if (retry_at is None or retry_at <= 0 or now_sec <= retry_at) else "unknown"
    if k == "rate_limit":
        return "rate_limit" if (retry_at is None or retry_at <= 0 or now_sec <= retry_at) else "unknown"
    if k == "auth":
        return "auth_error" if (retry_at is None or retry_at <= 0 or now_sec <= retry_at) else "unknown"
    if k == "offline":
        return "offline" if (retry_at is None or retry_at <= 0 or now_sec <= retry_at) else "unknown"
    if retry_at is not None and now_sec <= retry_at:
        return "cooldown"
    return "unknown"


def _read_sqlite_telemetry(
    sqlite_path: Path,
    now_sec: float,
) -> Tuple[bool, Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[Tuple[str, str], Dict[str, Any]]]:
    """Read runs, route_health, and cognitive_telemetry strictly read-only."""
    health_by_provider: Dict[str, Dict[str, Any]] = {}
    prov_buckets: Dict[str, Dict[str, Any]] = {}
    model_buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}

    if not sqlite_path.is_file():
        return False, health_by_provider, prov_buckets, model_buckets

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True, timeout=1.0)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        if "route_health" in tables:
            rows = conn.execute(
                "SELECT client, provider, model, kind, detail, observed, retry_at, expires "
                "FROM route_health ORDER BY observed DESC LIMIT 500"
            ).fetchall()
            for client, provider, _model, kind, detail, observed, retry_at, expires in rows:
                prov = (provider or "").strip().lower() or _provider_of_run(client, _model)
                if not SAFE_PROVIDER_ID.fullmatch(prov):
                    continue
                status = _map_health_status(kind, _finite_nonneg_float(retry_at), _finite_nonneg_float(expires), now_sec)
                existing = health_by_provider.get(prov)
                obs_f = _finite_nonneg_float(observed) or 0.0
                # Prefer an active block or the freshest observation
                if existing is None or (
                    existing["status"] == "unknown" and status != "unknown"
                ) or obs_f > existing["observed"]:
                    clean_detail = scrub_sensitive_text(str(detail or "")) or None
                    if clean_detail:
                        clean_detail = clean_detail[:180]
                    health_by_provider[prov] = {
                        "status": status,
                        "detail": clean_detail,
                        "observed": obs_f,
                        "observed_ms": int(obs_f * 1000) if obs_f > 0 else None,
                        "retry_at_iso": _epoch_to_iso(retry_at) if status != "available" else None,
                    }

        if "cognitive_telemetry" in tables:
            cog_rows = conn.execute(
                "SELECT provider, model_name, input_tokens, output_tokens, cache_read_tokens, "
                "cache_creation_tokens, total_tokens, tokens_provenance, total_cost_usd, cost_provenance "
                "FROM cognitive_telemetry ORDER BY id DESC LIMIT 1000"
            ).fetchall()
            for prov_raw, model_raw, in_t, out_t, cr_t, cc_t, tot_t, t_prov, cost_v, _c_prov in cog_rows:
                prov = _provider_of_run(prov_raw, model_raw)
                model = str(model_raw or "").strip() or "desconhecido"
                for bucket in (
                    prov_buckets.setdefault(prov, _empty_token_bucket()),
                    model_buckets.setdefault((prov, model), _empty_token_bucket()),
                ):
                    bucket["runs_count"] += 1
                    _add_opt(bucket, "input_tokens", _finite_nonneg_int(in_t))
                    _add_opt(bucket, "output_tokens", _finite_nonneg_int(out_t))
                    _add_opt(bucket, "cache_read_tokens", _finite_nonneg_int(cr_t))
                    _add_opt(bucket, "cache_creation_tokens", _finite_nonneg_int(cc_t))
                    _add_opt(bucket, "total_tokens", _finite_nonneg_int(tot_t))
                    cv = _finite_nonneg_float(cost_v)
                    if cv is not None:
                        bucket["cost_usd"] = round((bucket["cost_usd"] or 0.0) + cv, 6)
                        bucket["has_cost"] = True
                    if t_prov == MetricProvenance.ESTIMATED.value:
                        bucket["any_estimated"] = True

        if "runs" in tables:
            run_rows = conn.execute(
                "SELECT client, model, usage FROM runs ORDER BY rowid DESC LIMIT 1000"
            ).fetchall()
            for client, model_raw, usage_raw in run_rows:
                prov = _provider_of_run(client, model_raw)
                if prov == "unknown":
                    continue
                model = str(model_raw or "").strip() or "desconhecido"
                pb = prov_buckets.setdefault(prov, _empty_token_bucket())
                mb = model_buckets.setdefault((prov, model), _empty_token_bucket())
                pb["runs_count"] += 1
                mb["runs_count"] += 1
                if usage_raw and isinstance(usage_raw, str):
                    try:
                        parsed = json.loads(usage_raw)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        _accumulate_usage_dict(pb, parsed)
                        _accumulate_usage_dict(mb, parsed)

        return True, health_by_provider, prov_buckets, model_buckets
    except (sqlite3.Error, OSError, ValueError):
        return False, {}, {}, {}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _read_quota_snapshot_file(path: Path) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    if not path.is_file():
        return False, {}, None
    try:
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > 512 * 1024:
            return False, {}, "Arquivo provider_quotas.json excede o limite de tamanho."
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            return False, {}, "Formato inválido em provider_quotas.json."
        return True, data, None
    except (OSError, UnicodeDecodeError, ValueError):
        return False, {}, "Arquivo provider_quotas.json ilegível ou corrompido; ignorado com segurança."


def collect_providers_telemetry(
    *,
    profile_name: str = "default",
    vault_keys: Optional[Sequence[str]] = None,
    data_dir: Optional[Path] = None,
    coordinator_budget: Optional[Dict[str, Any]] = None,
    agents_data: Optional[Dict[str, Any]] = None,
    routes_data: Optional[Dict[str, Any]] = None,
    now_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Build sanitized, provenance-tagged provider telemetry snapshot for 4D.4.3 / 4D.4.4."""
    from ai_coordinator.data import default_data_dir
    from desktop.ipc.discovery import agents_snapshot, routes_snapshot

    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    current_sec = current_ms / 1000.0
    resolved_dir = Path(data_dir) if data_dir is not None else default_data_dir()

    if agents_data is None:
        agents_data = agents_snapshot()
    if routes_data is None:
        routes_data = routes_snapshot()

    vault_key_set = {str(k).upper() for k in (vault_keys or []) if isinstance(k, str)}

    launcher_by_client: Dict[str, Optional[bool]] = {}
    for ag in agents_data.get("agents") or []:
        c = ag.get("client")
        if isinstance(c, str):
            launcher_by_client[c] = ag.get("available") if isinstance(ag.get("available"), bool) else None

    configured_models_by_provider: Dict[str, List[str]] = {}
    for route in routes_data.get("routes") or []:
        for cand in route.get("candidates") or []:
            c = cand.get("client")
            m = cand.get("model")
            if isinstance(c, str) and isinstance(m, str):
                prov = _provider_of_run(c, m)
                configured_models_by_provider.setdefault(prov, [])
                if m not in configured_models_by_provider[prov]:
                    configured_models_by_provider[prov].append(m)

    sqlite_ok, health_by_prov, prov_buckets, model_buckets = _read_sqlite_telemetry(
        resolved_dir / "coordination.sqlite",
        current_sec,
    )
    quota_ok, quota_doc, quota_notice = _read_quota_snapshot_file(resolved_dir / "provider_quotas.json")

    doc_origin = _valid_origin(quota_doc.get("data_origin"), DataOrigin.OFFLINE_CACHE.value) if quota_ok else DataOrigin.OFFLINE_CACHE.value
    doc_observed_ms = _finite_nonneg_int(quota_doc.get("observed_at_unix_ms")) if quota_ok else None
    doc_ttl_ms = _finite_nonneg_int(quota_doc.get("ttl_ms")) or DEFAULT_PROVIDER_TTL_MS
    doc_providers = quota_doc.get("providers") if (quota_ok and isinstance(quota_doc.get("providers"), dict)) else {}

    provider_ids: List[str] = [p["provider"] for p in CANONICAL_PROVIDERS]
    for extra_pid in sorted(set(health_by_prov) | set(prov_buckets) | set(doc_providers)):
        if SAFE_PROVIDER_ID.fullmatch(extra_pid) and extra_pid not in provider_ids:
            provider_ids.append(extra_pid)

    items: List[Dict[str, Any]] = []
    any_fixture = doc_origin == DataOrigin.TEST_FIXTURE.value
    any_live_net = doc_origin == DataOrigin.LIVE_NETWORK.value
    any_stale = False
    any_measured = False
    any_estimated = False

    for pid in provider_ids:
        meta = _CANONICAL_BY_ID.get(
            pid,
            {"provider": pid, "display_name": pid.capitalize(), "quota_kind": "api_rate_limit", "vault_keys": (), "agent_client": pid},
        )
        q_entry = doc_providers.get(pid) if isinstance(doc_providers.get(pid), dict) else {}
        q_kind = q_entry.get("quota_kind") if q_entry.get("quota_kind") in _VALID_QUOTA_KINDS else meta["quota_kind"]

        entry_origin = _valid_origin(q_entry.get("data_origin"), doc_origin if q_entry else DataOrigin.OFFLINE_CACHE.value)
        if entry_origin == DataOrigin.TEST_FIXTURE.value:
            any_fixture = True
        elif entry_origin == DataOrigin.LIVE_NETWORK.value:
            any_live_net = True

        entry_obs_ms = _finite_nonneg_int(q_entry.get("observed_at_unix_ms")) or doc_observed_ms
        entry_ttl_ms = _finite_nonneg_int(q_entry.get("ttl_ms")) or doc_ttl_ms

        raw_windows = q_entry.get("windows") if isinstance(q_entry.get("windows"), dict) else {}
        win_5h = _parse_quota_window(
            raw_windows.get("five_hour") or raw_windows.get("5h"),
            "5h",
            "5 horas",
            entry_origin,
            entry_obs_ms,
            entry_ttl_ms,
            current_ms,
        )
        win_weekly = _parse_quota_window(
            raw_windows.get("weekly"),
            "weekly",
            "Semanal",
            entry_origin,
            entry_obs_ms,
            entry_ttl_ms,
            current_ms,
        )

        # API rate limits (requests / tokens remaining)
        api_limits = None
        if isinstance(q_entry.get("api_limits"), dict):
            al = q_entry["api_limits"]
            req_rem = _finite_nonneg_int(al.get("requests_remaining"))
            req_lim = _finite_nonneg_int(al.get("requests_limit"))
            tok_rem = _finite_nonneg_int(al.get("tokens_remaining"))
            tok_lim = _finite_nonneg_int(al.get("tokens_limit"))
            al_prov = _valid_provenance(
                al.get("provenance"),
                MetricProvenance.MEASURED.value if any(x is not None for x in (req_rem, req_lim, tok_rem, tok_lim)) else MetricProvenance.UNKNOWN.value,
            )
            api_limits = {
                "requests_remaining": req_rem,
                "requests_limit": req_lim,
                "tokens_remaining": tok_rem,
                "tokens_limit": tok_lim,
                "resets_at_iso": scrub_sensitive_text(str(al["resets_at_iso"]))[:40] if isinstance(al.get("resets_at_iso"), str) else None,
                "provenance": al_prov,
            }

        # Cost / balance / credits
        p_bucket = prov_buckets.get(pid, _empty_token_bucket())
        tokens_summary = _finalize_tokens(p_bucket)

        cost_credits = None
        raw_cc = q_entry.get("cost_credits") if isinstance(q_entry.get("cost_credits"), dict) else {}
        bal_usd = _finite_nonneg_float(raw_cc.get("balance_usd"))
        cred_rem = _finite_nonneg_float(raw_cc.get("credits_remaining_usd"))
        spent_usd = _finite_nonneg_float(raw_cc.get("spent_usd"))
        est_usd = _finite_nonneg_float(raw_cc.get("estimated_cost_usd"))
        if spent_usd is None and p_bucket["has_cost"]:
            spent_usd = p_bucket["cost_usd"]
        if any(x is not None for x in (bal_usd, cred_rem, spent_usd, est_usd)):
            cc_prov = _valid_provenance(
                raw_cc.get("provenance"),
                MetricProvenance.MEASURED.value if any(x is not None for x in (bal_usd, cred_rem, spent_usd)) else MetricProvenance.ESTIMATED.value,
            )
            cost_credits = {
                "kind": q_kind,
                "currency": "USD",
                "balance_usd": bal_usd,
                "credits_remaining_usd": cred_rem,
                "spent_usd": spent_usd,
                "estimated_cost_usd": est_usd,
                "provenance": cc_prov,
                "note": scrub_sensitive_text(str(raw_cc.get("note")))[:120] if isinstance(raw_cc.get("note"), str) else None,
            }

        # Health / availability
        h = health_by_prov.get(pid)
        avail_status = q_entry.get("availability_status") if isinstance(q_entry.get("availability_status"), str) else (h["status"] if h else "unknown")
        avail_detail = scrub_sensitive_text(str(q_entry.get("availability_detail")))[:180] if isinstance(q_entry.get("availability_detail"), str) else (h["detail"] if h else None)
        retry_iso = h["retry_at_iso"] if h else None

        # Combine per-model breakdown (runs/cognitive telemetry + configured route models)
        models_list: List[Dict[str, Any]] = []
        seen_models: set[str] = set()
        for (mp, model_name), mb in sorted(model_buckets.items(), key=lambda kv: (kv[1]["total_tokens"] or 0), reverse=True):
            if mp != pid:
                continue
            seen_models.add(model_name)
            models_list.append(
                {
                    "model": model_name,
                    "tokens": _finalize_tokens(mb),
                    "cost_usd": mb["cost_usd"] if mb["has_cost"] else None,
                    "cost_provenance": MetricProvenance.MEASURED.value if mb["has_cost"] else MetricProvenance.UNKNOWN.value,
                }
            )
        for conf_model in configured_models_by_provider.get(pid, ()):
            if conf_model not in seen_models:
                seen_models.add(conf_model)
                models_list.append(
                    {
                        "model": conf_model,
                        "tokens": _finalize_tokens(_empty_token_bucket()),
                        "cost_usd": None,
                        "cost_provenance": MetricProvenance.UNKNOWN.value,
                    }
                )

        # Freshness for provider item
        if entry_obs_ms is None and h and h.get("observed_ms"):
            entry_obs_ms = h["observed_ms"]
        item_stale = bool(
            win_5h["stale"]
            or win_weekly["stale"]
            or (entry_obs_ms is not None and entry_ttl_ms > 0 and (current_ms - entry_obs_ms) > entry_ttl_ms)
        )
        if item_stale:
            any_stale = True

        # Track overall provenance
        provs_used = {
            win_5h["provenance"],
            win_weekly["provenance"],
            tokens_summary["provenance"],
            cost_credits["provenance"] if cost_credits else MetricProvenance.UNKNOWN.value,
        }
        if MetricProvenance.MEASURED.value in provs_used:
            any_measured = True
        if MetricProvenance.ESTIMATED.value in provs_used:
            any_estimated = True

        agent_client = meta["agent_client"]
        has_key = any(k.upper() in vault_key_set for k in meta["vault_keys"])

        items.append(
            {
                "provider": pid,
                "display_name": meta["display_name"],
                "profile_name": profile_name,
                "launcher_available": launcher_by_client.get(agent_client) if agent_client else None,
                "has_vault_key": has_key,
                "availability_status": avail_status,
                "availability_detail": avail_detail,
                "availability_retry_at_iso": retry_iso,
                "quota_kind": q_kind,
                "windows": {
                    "five_hour": win_5h,
                    "weekly": win_weekly,
                },
                "api_limits": api_limits,
                "cost_credits": cost_credits,
                "local_limit": coordinator_budget if coordinator_budget else None,
                "tokens": tokens_summary,
                "models": models_list[:12],
                "data_origin": entry_origin,
                "observed_at_unix_ms": entry_obs_ms,
                "ttl_ms": entry_ttl_ms,
                "stale": item_stale,
            }
        )

    if any_fixture:
        overall_origin = DataOrigin.TEST_FIXTURE.value
    elif any_live_net:
        overall_origin = DataOrigin.LIVE_NETWORK.value
    else:
        overall_origin = DataOrigin.OFFLINE_CACHE.value

    if any_measured and not any_estimated:
        overall_prov = MetricProvenance.MEASURED.value
    elif any_measured or any_estimated:
        overall_prov = MetricProvenance.ESTIMATED.value
    else:
        overall_prov = MetricProvenance.UNKNOWN.value

    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "profile_name": profile_name,
        "sampled_at_unix_ms": current_ms,
        "ttl_ms": doc_ttl_ms,
        "stale": any_stale,
        "data_origin": overall_origin,
        "provenance": overall_prov,
        "sources": {
            "sqlite_available": sqlite_ok,
            "quota_snapshot_available": quota_ok,
            "vault_keys_checked": vault_keys is not None,
        },
        "providers": items,
        "notice": quota_notice,
    }
