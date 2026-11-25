"""Local, read-only provider telemetry adapters (Phase 4D.4, consolidated round r2).

One adapter per provider turns what the provider CLIs and the quota warner (aviso-cota) already write on this
machine into numbers: quota windows (5 h / weekly) with reset time, tokens, context of the last turn, cost and plan.

- Read only: SQLite through ``?mode=ro`` plus ``PRAGMA query_only``; JSON files and ``.jsonl`` tails with byte caps.
- No process is started (``agy`` is never invoked) and no network call is made; the OpenRouter remote lookup is an
  opt-in that stays off.
- Conversation text never leaves the parsers: only counters, timestamps, model names and plan labels do.
- Every failure becomes an unavailable source with a short reason, never an exception for the UI.
- Readings are cached for 30 s (and per file by mtime/size), with bounded file counts and bytes per cycle.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ai_coordinator.telemetry import scrub_sensitive_text
from desktop.ipc.protocol import DataOrigin, MetricProvenance

LOCAL_CACHE_TTL_MS = 30_000
LOCAL_READING_STALE_MS = 15 * 60 * 1000
DEFAULT_LOOKBACK_HOURS = 48.0
MAX_SESSION_FILES = 8
MAX_TAIL_BYTES = 256 * 1024
MAX_JSON_BYTES = 256 * 1024
MAX_SQLITE_ROWS = 500
MAX_PROJECT_DIRS = 200
MAX_QUOTA_FILES = 16
MAX_FILE_CACHE_ENTRIES = 96
EXHAUSTED_FALLBACK_MS = 5 * 3600 * 1000

_MEASURED = MetricProvenance.MEASURED.value
_ESTIMATED = MetricProvenance.ESTIMATED.value
_UNKNOWN = MetricProvenance.UNKNOWN.value
_OFFLINE = DataOrigin.OFFLINE_CACHE.value

_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,95}$")
_SAFE_PROVIDER = re.compile(r"^[a-z0-9_-]{1,40}$")
_SAFE_PLAN = re.compile(r"^[A-Za-z0-9_. -]{1,32}$")
_NOT_ID = re.compile(r"[^a-z0-9_-]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_WINDOW_IDS = {"five_hour": ("5h", "Janela de 5 h"), "weekly": ("weekly", "Janela semanal")}
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
    "total_tokens",
)
_OPENCODE_SESSION_COLUMNS = (
    "cost",
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
)
_EXTRA_WINDOW_LABELS = {"openrouter-free-dia": "Cota diária :free"}
CLAUDE_SUBAGENTS_NOTE = "transcrições de subagentes não incluídas"

OPENROUTER_REMOTE_LOOKUP = {
    "enabled": False,
    "note": "Consulta remota (GET /api/v1/key e /api/v1/credits) desligada por padrão; exige autorização explícita.",
}


# ------------------------------------------------------------------ value helpers


def _num(val: Any) -> Optional[float]:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    f = float(val)
    return f if math.isfinite(f) and f >= 0.0 else None


def _int(val: Any) -> Optional[int]:
    f = _num(val)
    return int(round(f)) if f is not None else None


def _num_text(val: Any) -> Optional[float]:
    if isinstance(val, str) and len(val) <= 32:
        try:
            return _num(float(val.strip()))
        except ValueError:
            return None
    return _num(val)


def _epoch_ms(val: Any) -> Optional[int]:
    """Epoch in seconds or milliseconds, as milliseconds."""
    f = _num(val)
    if f is None or f <= 0.0:
        return None
    return int(f) if f >= 1e11 else int(f * 1000)


def _iso_to_ms(val: Any) -> Optional[int]:
    if not isinstance(val, str) or not val or len(val) > 40:
        return None
    text = val.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    try:
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _ms_to_iso(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _clean_label(val: Any, limit: int = 60) -> Optional[str]:
    if not isinstance(val, str):
        return None
    text = _CONTROL.sub(" ", val[:400]).strip()
    if not text:
        return None
    return (scrub_sensitive_text(text) or "")[:limit] or None


def _safe_model(val: Any) -> Optional[str]:
    if not isinstance(val, str):
        return None
    text = val.strip()
    if not _SAFE_MODEL.fullmatch(text):
        return None
    return scrub_sensitive_text(text)


def _safe_id(val: Any, default: str) -> str:
    if not isinstance(val, str) or not val.strip():
        return default
    return _NOT_ID.sub("_", val.strip().lower())[:40] or default


def _tokens(
    input_tokens: Any = None,
    output_tokens: Any = None,
    cache_read: Any = None,
    cache_creation: Any = None,
    reasoning: Any = None,
    total: Any = None,
) -> Optional[Dict[str, Optional[int]]]:
    values = {
        "input_tokens": _int(input_tokens),
        "output_tokens": _int(output_tokens),
        "cache_read_tokens": _int(cache_read),
        "cache_creation_tokens": _int(cache_creation),
        "reasoning_tokens": _int(reasoning),
        "total_tokens": _int(total),
    }
    if all(v is None for v in values.values()):
        return None
    if values["total_tokens"] is None:
        parts = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
        values["total_tokens"] = sum(values[k] or 0 for k in parts)
    return values


def _add_tokens(acc: Dict[str, Optional[int]], part: Optional[Dict[str, Optional[int]]]) -> None:
    if not part:
        return
    for key in _TOKEN_KEYS:
        value = part.get(key)
        if value is not None:
            acc[key] = (acc.get(key) or 0) + value


def _token_summary(acc: Optional[Dict[str, Optional[int]]], provenance: str, runs: int) -> Dict[str, Any]:
    acc = acc or {}
    summary: Dict[str, Any] = {key: acc.get(key) for key in _TOKEN_KEYS}
    known = any(summary[key] is not None for key in _TOKEN_KEYS)
    summary["provenance"] = provenance if known else _UNKNOWN
    summary["runs_count"] = runs
    return summary


def _models_list(per_model: Dict[str, Dict[str, Any]], provenance: str, limit: int = 8) -> List[Dict[str, Any]]:
    ordered = sorted(per_model.items(), key=lambda kv: kv[1]["tokens"].get("total_tokens") or 0, reverse=True)
    out = []
    for name, entry in ordered[:limit]:
        cost = entry.get("cost")
        out.append(
            {
                "model": name,
                "tokens": _token_summary(entry["tokens"], provenance, entry.get("runs", 0)),
                "cost_usd": round(cost, 6) if cost is not None else None,
                "cost_provenance": _MEASURED if cost is not None else _UNKNOWN,
            }
        )
    return out


# ------------------------------------------------------------------ quota windows


def _window_kind(window_id: str) -> Optional[str]:
    wid = window_id.strip().lower()
    if wid in ("five_hour", "5h", "w300") or wid.endswith("-5h"):
        return "five_hour"
    if wid in ("seven_day", "weekly", "w10080") or wid.endswith("-weekly"):
        return "weekly"
    return None


def _window(
    kind: str,
    label: Optional[str],
    used_pct: Any,
    now_ms: int,
    observed_ms: Optional[int],
    *,
    reset_ms: Optional[int] = None,
    group: Optional[str] = None,
    profile: Optional[str] = None,
    disabled: bool = False,
) -> Dict[str, Any]:
    window_id, default_label = _WINDOW_IDS.get(kind, (kind, kind))
    reading: Dict[str, Any] = {
        "window": window_id,
        "label": label or default_label,
        "used_percent": None,
        "remaining_percent": None,
        "resets_at_iso": _ms_to_iso(reset_ms),
        "provenance": _UNKNOWN,
        "data_origin": _OFFLINE,
        "observed_at_unix_ms": observed_ms,
        "ttl_ms": LOCAL_READING_STALE_MS,
        "stale": observed_ms is None or now_ms - observed_ms > LOCAL_READING_STALE_MS,
        "supported": True,
        "note": None,
        "group": group,
        "profile": profile,
        "disabled": bool(disabled),
    }
    used = _num(used_pct)
    if disabled:
        reading["note"] = "Desativada pelo provedor."
    elif reset_ms is not None and reset_ms <= now_ms:
        reading["resets_at_iso"] = None
        reading["note"] = "Janela renovada depois da última leitura; uso atual indisponível."
    elif used is not None and used <= 100.0:
        reading["used_percent"] = round(used, 2)
        reading["remaining_percent"] = round(100.0 - used, 2)
        reading["provenance"] = _MEASURED
    return reading


def _unsupported_window(kind: str, note: str) -> Dict[str, Any]:
    window_id, label = _WINDOW_IDS[kind]
    return {
        "window": window_id,
        "label": label,
        "used_percent": None,
        "remaining_percent": None,
        "resets_at_iso": None,
        "provenance": _UNKNOWN,
        "data_origin": _OFFLINE,
        "observed_at_unix_ms": None,
        "ttl_ms": None,
        "stale": False,
        "supported": False,
        "note": note,
        "group": None,
        "profile": None,
        "disabled": False,
    }


def _missing_window(kind: str) -> Dict[str, Any]:
    reading = _unsupported_window(kind, "")
    reading["supported"] = True
    reading["note"] = None
    return reading


def _exhausted(raw: Any, now_ms: int) -> Tuple[bool, Optional[str]]:
    if not isinstance(raw, dict):
        return False, None
    until = _epoch_ms(raw.get("ate"))
    if until is not None:
        return (True, _ms_to_iso(until)) if until > now_ms else (False, None)
    since = _epoch_ms(raw.get("em"))
    return (since is not None and now_ms - since < EXHAUSTED_FALLBACK_MS), None


def _parse_leitura(doc: Dict[str, Any], now_ms: int, fallback_profile: str) -> Dict[str, Any]:
    """aviso-cota normalized reading: ``{"perfil", "observado_em", "janelas": [...], "esgotada"}``."""
    observed = _epoch_ms(doc.get("observado_em"))
    profile = _safe_id(doc.get("perfil"), fallback_profile)
    main: Dict[str, Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    window_ids: List[str] = []
    raw_windows = doc.get("janelas") if isinstance(doc.get("janelas"), list) else []
    for raw in raw_windows[:12]:
        if not isinstance(raw, dict):
            continue
        wid = raw.get("id") if isinstance(raw.get("id"), str) else ""
        kind = _window_kind(wid)
        reset_ms = _epoch_ms(raw.get("reset_epoch"))
        if kind and kind not in main:
            main[kind] = _window(kind, None, raw.get("usado_pct"), now_ms, observed, reset_ms=reset_ms, profile=profile)
            window_ids.append(wid)
            continue
        safe_wid = _safe_id(wid, "janela")
        label = _EXTRA_WINDOW_LABELS.get(safe_wid) or _clean_label(raw.get("rotulo")) or safe_wid
        extras.append(_window(safe_wid, label, raw.get("usado_pct"), now_ms, observed, reset_ms=reset_ms, profile=profile))
    exhausted, until = _exhausted(doc.get("esgotada"), now_ms)
    return {
        "profile": profile,
        "observed_at_unix_ms": observed,
        "windows": main,
        "extra_windows": extras,
        "window_ids": window_ids,
        "exhausted": exhausted,
        "exhausted_until_iso": until,
    }


def _parse_agy_cache(doc: Dict[str, Any], now_ms: int, profile: str, preferred_ids: Iterable[str]) -> Dict[str, Any]:
    """quota-guard cache: ``{"observed_at", "buckets": {name: {group, window, remaining_fraction, reset_time, disabled}}}``."""
    observed = _epoch_ms(doc.get("observed_at"))
    buckets = doc.get("buckets") if isinstance(doc.get("buckets"), dict) else {}
    parsed: List[Tuple[str, Optional[str], str, Dict[str, Any]]] = []
    for name, bucket in list(buckets.items())[:16]:
        if not isinstance(name, str) or not isinstance(bucket, dict):
            continue
        group = _clean_label(bucket.get("group"), 40) or "Modelos"
        win = bucket.get("window")
        kind = "five_hour" if win == "5h" else "weekly" if win == "weekly" else None
        fraction = _num(bucket.get("remaining_fraction"))
        used = (1.0 - fraction) * 100.0 if fraction is not None and fraction <= 1.0 else None
        span = "5 h" if kind == "five_hour" else "semanal" if kind == "weekly" else (_clean_label(win, 16) or "janela")
        reading = _window(
            kind or _safe_id(name, "balde"),
            f"{group} · {span}",
            used,
            now_ms,
            observed,
            reset_ms=_iso_to_ms(bucket.get("reset_time")),
            group=group,
            profile=profile,
            disabled=bucket.get("disabled") is True,
        )
        parsed.append((name, kind, group, reading))

    preferred_group = None
    for wanted in preferred_ids:
        preferred_group = next((g for n, _k, g, _w in parsed if n == wanted), None)
        if preferred_group:
            break
    if preferred_group is None and parsed:
        preferred_group = parsed[0][2]

    main: Dict[str, Dict[str, Any]] = {}
    extras: List[Dict[str, Any]] = []
    for _name, kind, group, reading in parsed:
        if kind and group == preferred_group and kind not in main:
            main[kind] = dict(reading, window=_WINDOW_IDS[kind][0], label=_WINDOW_IDS[kind][1] + f" ({group})")
        else:
            extras.append(reading)
    esgotada = doc.get("esgotada")
    exhausted, until = _exhausted(esgotada, now_ms)
    return {
        "profile": profile,
        "observed_at_unix_ms": observed,
        "windows": main,
        "extra_windows": extras,
        "window_ids": [],
        "exhausted": exhausted,
        "exhausted_until_iso": until,
        "exhausted_model": _safe_model(esgotada.get("modelo")) if isinstance(esgotada, dict) else None,
    }


def _account(parsed: Dict[str, Any], now_ms: int) -> Dict[str, Any]:
    observed = parsed.get("observed_at_unix_ms")
    windows = parsed.get("windows") or {}
    return {
        "profile": parsed["profile"],
        "windows": {
            "five_hour": windows.get("five_hour") or _missing_window("five_hour"),
            "weekly": windows.get("weekly") or _missing_window("weekly"),
        },
        "observed_at_unix_ms": observed,
        "stale": observed is None or now_ms - observed > LOCAL_READING_STALE_MS,
        "exhausted": bool(parsed.get("exhausted")),
        "exhausted_until_iso": parsed.get("exhausted_until_iso"),
        "exhausted_model": parsed.get("exhausted_model") if parsed.get("exhausted") else None,
    }


def _source(kind: str, label: str, available: bool, reason: Optional[str] = None, observed_ms: Optional[int] = None) -> Dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "available": bool(available),
        "reason": None if available and not reason else reason,
        "observed_at_unix_ms": observed_ms,
    }


def _context(
    used: Any, limit: Any, model: Optional[str], observed_ms: Optional[int], profile: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    used_i = _int(used)
    if used_i is None:
        return None
    limit_i = _int(limit) or None
    return {
        "used_tokens": used_i,
        "limit_tokens": limit_i,
        "used_percent": round(min(100.0, used_i * 100.0 / limit_i), 1) if limit_i else None,
        "model": model,
        "provenance": _MEASURED,
        "observed_at_unix_ms": observed_ms,
        "profile": profile,
    }


def _add_profile_share(shares: Dict[str, Dict[str, Any]], profile: str, tokens: Optional[Dict[str, Optional[int]]], sessions: int = 0) -> None:
    share = shares.setdefault(profile, {"sessions": 0, "tokens": {}})
    share["sessions"] += sessions
    _add_tokens(share["tokens"], tokens)


def _scope(
    lookback_hours: float,
    sessions: int,
    truncated: bool,
    label: str,
    *,
    tail_truncated: Optional[bool] = None,
    files_read: Optional[int] = None,
    files_found: Optional[int] = None,
    profiles: Optional[Dict[str, Dict[str, Any]]] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """What the token totals cover: the totals add up every listed profile, and ``truncated`` marks a partial sample."""
    scope: Dict[str, Any] = {
        "label": label,
        "lookback_hours": lookback_hours,
        "sessions": sessions,
        "truncated": truncated,
    }
    if tail_truncated is not None:
        scope["tail_truncated"] = tail_truncated
    if files_found is not None:
        scope["files_read"] = files_read
        scope["files_found"] = files_found
    if profiles is not None:
        scope["profiles"] = [
            {"profile": name, "sessions": share["sessions"], "total_tokens": share["tokens"].get("total_tokens")}
            for name, share in sorted(profiles.items())
        ]
    if note:
        scope["note"] = note
    return scope


def _session_scope_label(lookback_hours: float, capped: bool) -> str:
    if capped:
        return f"sessões locais mais recentes das últimas {lookback_hours:g} h"
    return f"sessões locais atualizadas nas últimas {lookback_hours:g} h"


def _reading(
    provider: str,
    now_ms: int,
    sources: List[Dict[str, Any]],
    *,
    windows: Optional[Dict[str, Dict[str, Any]]] = None,
    extra_windows: Optional[List[Dict[str, Any]]] = None,
    accounts: Optional[List[Dict[str, Any]]] = None,
    tokens: Optional[Dict[str, Any]] = None,
    usage_scope: Optional[Dict[str, Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    cost: Optional[Dict[str, Any]] = None,
    models: Optional[List[Dict[str, Any]]] = None,
    plan: Optional[Dict[str, Any]] = None,
    exhausted: bool = False,
    exhausted_until_iso: Optional[str] = None,
    exhausted_model: Optional[str] = None,
    windows_supported: bool = True,
    remote_lookup: Optional[Dict[str, Any]] = None,
    last_activity_ms: Optional[int] = None,
) -> Dict[str, Any]:
    available = any(src["available"] for src in sources)
    windows = windows or {}
    observed = [w["observed_at_unix_ms"] for w in windows.values() if w and w.get("observed_at_unix_ms")]
    observed_ms = max(observed) if observed else (now_ms if available else None)
    reasons = [f"{src['label']}: {src['reason']}" for src in sources if not src["available"] and src.get("reason")]
    return {
        "provider": provider,
        "available": available,
        "reason": None if available else ("; ".join(reasons) or "Nenhuma fonte local encontrada."),
        "sources": sources,
        "windows": windows,
        "extra_windows": extra_windows or [],
        "accounts": accounts or [],
        "tokens": tokens,
        "usage_scope": usage_scope,
        "context": context,
        "cost": cost,
        "models": models or [],
        "plan": plan,
        "exhausted": bool(exhausted),
        "exhausted_until_iso": exhausted_until_iso,
        "exhausted_model": exhausted_model,
        "windows_supported": windows_supported,
        "remote_lookup": remote_lookup,
        "last_activity_unix_ms": last_activity_ms,
        "observed_at_unix_ms": observed_ms,
        "ttl_ms": LOCAL_READING_STALE_MS,
        "stale": observed_ms is not None and now_ms - observed_ms > LOCAL_READING_STALE_MS,
        "data_origin": _OFFLINE,
    }


def _freshest(parsed: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return max(parsed, key=lambda p: p.get("observed_at_unix_ms") or 0) if parsed else None


# ------------------------------------------------------------------ session-log parsers (numbers only)


def _claude_usage(usage: Dict[str, Any]) -> Optional[Dict[str, Optional[int]]]:
    details = usage.get("output_tokens_details")
    reasoning = details.get("thinking_tokens") if isinstance(details, dict) else None
    return _tokens(
        usage.get("input_tokens"),
        usage.get("output_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cache_creation_input_tokens"),
        reasoning,
    )


def parse_claude_session_tail(data: bytes, truncated: bool) -> Dict[str, Any]:
    """Claude Code ``projects/**/<session>.jsonl``: only ``message.usage`` of assistant entries, deduplicated by id."""
    by_id: Dict[str, Tuple[Dict[str, Optional[int]], str]] = {}
    last: Optional[Dict[str, Any]] = None
    for index, line in enumerate(data.split(b"\n")):
        if b'"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict) or msg.get("model") == "<synthetic>":
            continue
        nums = _claude_usage(usage)
        if nums is None:
            continue
        msg_id = msg.get("id") if isinstance(msg.get("id"), str) else f"#{index}"
        model = _safe_model(msg.get("model")) or "desconhecido"
        by_id[msg_id] = (nums, model)
        last = {
            "at_ms": _iso_to_ms(obj.get("timestamp")),
            "context_used": (nums["input_tokens"] or 0) + (nums["cache_read_tokens"] or 0) + (nums["cache_creation_tokens"] or 0),
            "model": model,
        }
    per_model: Dict[str, Dict[str, Any]] = {}
    for nums, model in by_id.values():
        entry = per_model.setdefault(model, {"tokens": {}, "runs": 0, "cost": None})
        _add_tokens(entry["tokens"], nums)
        entry["runs"] += 1
    return {"per_model": per_model, "messages": len(by_id), "last": last, "truncated": truncated}


def _codex_usage(raw: Any) -> Optional[Dict[str, Optional[int]]]:
    if not isinstance(raw, dict):
        return None
    total_in = _int(raw.get("input_tokens"))
    cached = _int(raw.get("cached_input_tokens"))
    uncached = max(0, total_in - cached) if total_in is not None and cached is not None else total_in
    return _tokens(
        uncached,
        raw.get("output_tokens"),
        cached,
        raw.get("cache_write_input_tokens"),
        raw.get("reasoning_output_tokens"),
        raw.get("total_tokens"),
    )


def _codex_limits(raw: Dict[str, Any]) -> Dict[str, Any]:
    windows = []
    for slot in ("primary", "secondary"):
        entry = raw.get(slot)
        if not isinstance(entry, dict):
            continue
        minutes = _int(entry.get("window_minutes"))
        windows.append(
            {
                "minutes": minutes,
                "used_percent": _num(entry.get("used_percent")),
                "resets_at_ms": _epoch_ms(entry.get("resets_at")),
            }
        )
    credits = raw.get("credits") if isinstance(raw.get("credits"), dict) else None
    plan_type = raw.get("plan_type") if isinstance(raw.get("plan_type"), str) and _SAFE_PLAN.fullmatch(raw["plan_type"]) else None
    plan = None
    if plan_type or credits:
        plan = {
            "plan_type": plan_type,
            "has_credits": credits.get("has_credits") if credits and isinstance(credits.get("has_credits"), bool) else None,
            "unlimited": credits.get("unlimited") if credits and isinstance(credits.get("unlimited"), bool) else None,
            "credits_balance": _num_text(credits.get("balance")) if credits else None,
            "provenance": _MEASURED,
        }
    return {"windows": windows, "plan": plan}


def parse_codex_session_tail(data: bytes, truncated: bool) -> Dict[str, Any]:
    """Codex ``sessions/YYYY/MM/DD/rollout-*.jsonl``: last ``token_count`` event and the model of the last turn."""
    info: Optional[Dict[str, Any]] = None
    info_at: Optional[int] = None
    limits: Optional[Dict[str, Any]] = None
    limits_at: Optional[int] = None
    model: Optional[str] = None
    for line in data.split(b"\n"):
        is_count = b'"token_count"' in line
        if not is_count and b'"turn_context"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "turn_context":
            model = _safe_model(payload.get("model")) or model
            continue
        if payload.get("type") != "token_count":
            continue
        at_ms = _iso_to_ms(obj.get("timestamp"))
        if isinstance(payload.get("info"), dict):
            info, info_at = payload["info"], at_ms
        if isinstance(payload.get("rate_limits"), dict):
            limits, limits_at = _codex_limits(payload["rate_limits"]), at_ms
    return {
        "totals": _codex_usage(info.get("total_token_usage")) if info else None,
        "last": _codex_usage(info.get("last_token_usage")) if info else None,
        "context_window": _int(info.get("model_context_window")) if info else None,
        "info_at_ms": info_at,
        "limits": limits,
        "limits_at_ms": limits_at,
        "model": model,
        "truncated": truncated,
    }


# ------------------------------------------------------------------ paths


@dataclass(frozen=True)
class LocalSourcePaths:
    """Where each local source lives. Everything is injectable; discovery never hard-codes a user folder."""

    quota_dir: Optional[Path] = None
    claude_dirs: Tuple[Tuple[str, Path], ...] = ()
    codex_homes: Tuple[Tuple[str, Path], ...] = ()
    opencode_dbs: Tuple[Tuple[str, Path], ...] = ()


def _base_homes(home: Path) -> List[Path]:
    homes = [home]
    if home.name.lower().startswith(".env-") and home.parent != home:
        homes.append(home.parent)
    return homes


def _profile_of_home(home: Path) -> str:
    name = home.name.lower()
    return _safe_id(name[5:], "padrao") if name.startswith(".env-") and len(name) > 5 else "padrao"


def _profile_homes(bases: List[Path]) -> List[Tuple[str, Path]]:
    """The home in use, the main home and its ``.env-<perfil>`` profile homes (launcher convention)."""
    homes: List[Tuple[str, Path]] = []
    for base in bases:
        homes.append((_profile_of_home(base), base))
        try:
            with os.scandir(base) as entries:
                siblings = sorted(e.name for e in entries if e.name.lower().startswith(".env-") and e.is_dir())
        except OSError:
            siblings = []
        homes += [(_profile_of_home(base / name), base / name) for name in siblings[:MAX_QUOTA_FILES]]
    return homes


def _has_projects(path: Path) -> bool:
    return (path / "projects").is_dir()


def _has_sessions(path: Path) -> bool:
    return (path / "sessions").is_dir()


class _Unique:
    """Ordered (profile, path) pairs, one per real location."""

    def __init__(self) -> None:
        self.items: List[Tuple[str, Path]] = []
        self._seen: set = set()

    def add(self, profile: str, path: Path, exists: Callable[[Path], bool]) -> None:
        try:
            if not exists(path):
                return
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            return
        if key not in self._seen:
            self._seen.add(key)
            self.items.append((profile, path))


def _small_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _configured_root(raw: Any, require_absolute: bool = False) -> Optional[Path]:
    """A folder named by the environment or by an aviso-cota reading; None for a network share (UNC) or device path."""
    if not isinstance(raw, str) or not raw or raw.startswith(("\\\\", "//")):
        return None
    path = Path(raw)
    if path.drive.startswith(("\\\\", "//")) or (require_absolute and not path.is_absolute()):
        return None
    return path


def discover_local_source_paths(env: Optional[Mapping[str, str]] = None, home: Optional[Path] = None) -> LocalSourcePaths:
    """Find the sources by the same conventions the CLIs and aviso-cota use (environment first, then home)."""
    env = os.environ if env is None else env
    bases = _base_homes(Path(home) if home is not None else Path.home())

    configured_quota = _configured_root(env.get("AVISO_COTA_DIR"))
    quota_candidates = [configured_quota] if configured_quota is not None else []
    quota_candidates += [b / ".local" / "share" / "ai-coordinator-runtime" / "state" / "cota" for b in bases]
    quota_dir = next((c for c in quota_candidates if c.is_dir()), None)

    homes = _profile_homes(bases)

    claude = _Unique()
    configured = _configured_root(env.get("CLAUDE_CONFIG_DIR"))
    if configured is not None:
        claude.add(_profile_of_home(configured.parent), configured, _has_projects)
    if quota_dir is not None:
        try:
            names = sorted(n for n in os.listdir(quota_dir) if n.startswith("leitura-claude-") and n.endswith(".json"))
        except OSError:
            names = []
        for name in names[:MAX_QUOTA_FILES]:
            doc = _small_json(quota_dir / name)
            userprofile = _configured_root(doc.get("userprofile") if doc else None, require_absolute=True)
            if doc and userprofile is not None:
                claude.add(_safe_id(doc.get("perfil"), "padrao"), userprofile / ".claude", _has_projects)
    for profile, home_dir in homes:
        claude.add(profile, home_dir / ".claude", _has_projects)

    codex = _Unique()
    configured = _configured_root(env.get("CODEX_HOME"))
    if configured is not None:
        codex.add(_profile_of_home(configured.parent), configured, _has_sessions)
    for profile, home_dir in homes:
        codex.add(profile, home_dir / ".codex", _has_sessions)

    opencode = _Unique()
    configured = _configured_root(env.get("XDG_DATA_HOME"))
    if configured is not None:
        opencode.add(_profile_of_home(bases[0]), configured / "opencode" / "opencode.db", Path.is_file)
    for profile, home_dir in homes:
        opencode.add(profile, home_dir / ".local" / "share" / "opencode" / "opencode.db", Path.is_file)

    return LocalSourcePaths(
        quota_dir=quota_dir,
        claude_dirs=tuple(claude.items),
        codex_homes=tuple(codex.items),
        opencode_dbs=tuple(opencode.items),
    )


# ------------------------------------------------------------------ reader


def _opencode_ids(provider_id: Any, model_id: Any) -> Tuple[str, str]:
    provider = provider_id.strip().lower() if isinstance(provider_id, str) else ""
    return (provider if _SAFE_PROVIDER.fullmatch(provider) else "desconhecido"), (_safe_model(model_id) or "desconhecido")


def _opencode_model(raw: Any) -> Tuple[str, str]:
    """Session ``model`` column: JSON ``{"id", "providerID"}`` or ``provider/model`` text."""
    if isinstance(raw, str) and raw.startswith("{") and len(raw) <= 2048:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return _opencode_ids(obj.get("providerID"), obj.get("id") or obj.get("modelID"))
    elif isinstance(raw, str) and "/" in raw and len(raw) <= 200:
        return _opencode_ids(*raw.split("/", 1))
    return _opencode_ids(None, None)


def _time_unit_ms(newest: Any) -> int:
    """OpenCode stores epoch milliseconds; older schemas may use seconds."""
    return 1 if (_num(newest) or 0.0) >= 1e11 else 1000


def _opencode_row(
    session: Any,
    ids: Tuple[str, str],
    input_tokens: Any,
    output_tokens: Any,
    reasoning: Any,
    cache_read: Any,
    cache_write: Any,
    cost: Any,
    updated: Any,
    unit_ms: int,
) -> Dict[str, Any]:
    parts = [_int(v) for v in (input_tokens, output_tokens, reasoning, cache_read, cache_write)]
    total = sum(v or 0 for v in parts) if any(v is not None for v in parts) else None
    return {
        "session": session,
        "provider_id": ids[0],
        "model": ids[1],
        "tokens": _tokens(input_tokens, output_tokens, cache_read, cache_write, reasoning, total),
        "cost": _num(cost),
        "updated_ms": int((_num(updated) or 0.0) * unit_ms) or None,
    }


class LocalProviderReader:
    """Collects the five local adapters with a TTL cache, a per-file cache and bounded reads."""

    def __init__(
        self,
        paths: Optional[LocalSourcePaths] = None,
        *,
        discover: Callable[[], LocalSourcePaths] = discover_local_source_paths,
        ttl_ms: int = LOCAL_CACHE_TTL_MS,
        lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
        max_files: int = MAX_SESSION_FILES,
        max_tail_bytes: int = MAX_TAIL_BYTES,
        max_json_bytes: int = MAX_JSON_BYTES,
        clock_ms: Optional[Callable[[], int]] = None,
    ) -> None:
        self._paths = paths
        self._discover = discover
        self.ttl_ms = int(ttl_ms)
        self.lookback_hours = float(lookback_hours)
        self.max_files = int(max_files)
        self.max_tail_bytes = int(max_tail_bytes)
        self.max_json_bytes = int(max_json_bytes)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._cached: Optional[Dict[str, Any]] = None
        self._cached_at: Optional[int] = None
        self._file_cache: Dict[Tuple[str, str], Tuple[Any, Any]] = {}
        self.stats = {"collections": 0, "files_read": 0, "bytes_read": 0}

    # ---- public

    def read(self, now_ms: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            now = self._clock_ms() if now_ms is None else int(now_ms)
            if (
                not force
                and self._cached is not None
                and self._cached_at is not None
                and 0 <= now - self._cached_at < self.ttl_ms
            ):
                return self._cached
            try:
                paths = self._paths if self._paths is not None else self._discover()
            except Exception:
                paths = LocalSourcePaths()
            result = self._collect(paths, now)
            self._cached, self._cached_at = result, now
            self.stats["collections"] += 1
            return result

    # ---- collection

    def _collect(self, paths: LocalSourcePaths, now_ms: int) -> Dict[str, Any]:
        providers: Dict[str, Dict[str, Any]] = {}
        for pid, adapter in (("claude", self._claude), ("codex", self._codex), ("agy", self._agy)):
            try:
                providers[pid] = adapter(paths, now_ms)
            except Exception:
                providers[pid] = _reading(pid, now_ms, [_source("adapter", "Leitura local", False, "falha inesperada na leitura")])
        try:
            providers.update(self._opencode_family(paths, now_ms))
        except Exception:
            for pid in ("opencode", "openrouter"):
                providers[pid] = _reading(pid, now_ms, [_source("adapter", "Leitura local", False, "falha inesperada na leitura")])
        return {
            "read_at_unix_ms": now_ms,
            "cache_ttl_ms": self.ttl_ms,
            "lookback_hours": self.lookback_hours,
            "providers": providers,
        }

    def _claude(self, paths: LocalSourcePaths, now_ms: int) -> Dict[str, Any]:
        readings, statusline = self._leituras(paths.quota_dir, "leitura-claude-", "statusline", "Statusline do Claude Code (aviso-cota)", now_ms)
        sources = [statusline]

        candidates: List[Tuple[int, int, Path, str]] = []
        dirs_capped = False
        for profile, config_dir in paths.claude_dirs:
            projects = config_dir / "projects"
            subdirs, total_dirs = self._subdirs(projects, MAX_PROJECT_DIRS)
            dirs_capped = dirs_capped or total_dirs > len(subdirs)
            candidates += [(m, s, p, profile) for m, s, p in self._recent_files([projects] + subdirs, now_ms)]
        candidates.sort(key=lambda c: c[0], reverse=True)
        picked = candidates[: self.max_files]
        capped = dirs_capped or len(candidates) > len(picked)

        per_model: Dict[str, Dict[str, Any]] = {}
        totals: Dict[str, Optional[int]] = {}
        by_profile: Dict[str, Dict[str, Any]] = {}
        tail_truncated = False
        sessions = 0
        last: Optional[Dict[str, Any]] = None
        last_activity: Optional[int] = None
        for mtime_ns, size, path, profile in picked:
            parsed = self._session_file("claude", path, mtime_ns, size, parse_claude_session_tail)
            if not parsed or not parsed["messages"]:
                continue
            sessions += 1
            tail_truncated = tail_truncated or parsed["truncated"]
            last_activity = max(last_activity or 0, mtime_ns // 1_000_000)
            _add_profile_share(by_profile, profile, None, 1)
            for model, entry in parsed["per_model"].items():
                target = per_model.setdefault(model, {"tokens": {}, "runs": 0, "cost": None})
                _add_tokens(target["tokens"], entry["tokens"])
                _add_tokens(totals, entry["tokens"])
                _add_profile_share(by_profile, profile, entry["tokens"])
                target["runs"] += entry["runs"]
            candidate = parsed["last"]
            if candidate and (last is None or (candidate.get("at_ms") or 0) > (last.get("at_ms") or 0)):
                last = dict(candidate, profile=profile)
        if not paths.claude_dirs:
            sources.append(_source("session_logs", "Sessões locais do Claude Code (.jsonl)", False, "pasta de perfis não encontrada"))
        else:
            sources.append(
                _source(
                    "session_logs",
                    "Sessões locais do Claude Code (.jsonl)",
                    sessions > 0,
                    None if sessions else f"nenhuma sessão com uso nas últimas {self.lookback_hours:g} h",
                    last_activity,
                )
            )

        main = _freshest(readings)
        partial = tail_truncated or capped
        provenance = _ESTIMATED if partial else _MEASURED
        scope = (
            _scope(
                self.lookback_hours,
                sessions,
                partial,
                _session_scope_label(self.lookback_hours, capped),
                tail_truncated=tail_truncated,
                files_read=len(picked),
                files_found=len(candidates),
                profiles=by_profile,
                note=CLAUDE_SUBAGENTS_NOTE,
            )
            if sessions
            else None
        )
        return _reading(
            "claude",
            now_ms,
            sources,
            windows=dict(main["windows"]) if main else {},
            extra_windows=main["extra_windows"] if main else [],
            accounts=[_account(r, now_ms) for r in sorted(readings, key=lambda r: r["profile"])],
            tokens=_token_summary(totals, provenance, sessions) if sessions else None,
            usage_scope=scope,
            context=_context(last["context_used"], None, last["model"], last.get("at_ms"), last["profile"]) if last else None,
            models=_models_list(per_model, provenance),
            exhausted=bool(main and main["exhausted"]),
            exhausted_until_iso=main["exhausted_until_iso"] if main else None,
            last_activity_ms=last_activity,
        )

    def _codex(self, paths: LocalSourcePaths, now_ms: int) -> Dict[str, Any]:
        readings, quota_source = self._leituras(paths.quota_dir, "leitura-codex-", "quota_reading", "Leitura de cota do Codex (aviso-cota)", now_ms)
        day_dirs: List[Path] = []
        candidates: List[Tuple[int, int, Path, str]] = []
        for profile, codex_home in paths.codex_homes:
            home_dirs = self._codex_day_dirs(codex_home / "sessions", now_ms)
            day_dirs += home_dirs
            candidates += [(m, s, p, profile) for m, s, p in self._recent_files(home_dirs, now_ms)]
        candidates.sort(key=lambda c: c[0], reverse=True)
        picked = candidates[: self.max_files]
        capped = len(candidates) > len(picked)

        per_model: Dict[str, Dict[str, Any]] = {}
        totals: Dict[str, Optional[int]] = {}
        by_profile: Dict[str, Dict[str, Any]] = {}
        sessions = 0
        latest_info: Optional[Dict[str, Any]] = None
        latest_limits: Optional[Dict[str, Any]] = None
        last_activity: Optional[int] = None
        limits_profile = "padrao"
        info_profile = "padrao"
        for mtime_ns, size, path, profile in picked:
            parsed = self._session_file("codex", path, mtime_ns, size, parse_codex_session_tail)
            if not parsed:
                continue
            if parsed["totals"]:
                sessions += 1
                last_activity = max(last_activity or 0, mtime_ns // 1_000_000)
                _add_tokens(totals, parsed["totals"])
                _add_profile_share(by_profile, profile, parsed["totals"], 1)
                entry = per_model.setdefault(parsed["model"] or "desconhecido", {"tokens": {}, "runs": 0, "cost": None})
                _add_tokens(entry["tokens"], parsed["totals"])
                entry["runs"] += 1
                if latest_info is None or (parsed["info_at_ms"] or 0) > (latest_info["info_at_ms"] or 0):
                    latest_info, info_profile = parsed, profile
            if parsed["limits"] and (latest_limits is None or (parsed["limits_at_ms"] or 0) > (latest_limits["limits_at_ms"] or 0)):
                latest_limits, limits_profile = parsed, profile

        windows: Dict[str, Dict[str, Any]] = {}
        extras: List[Dict[str, Any]] = []
        plan = None
        if latest_limits:
            observed = latest_limits["limits_at_ms"]
            for entry in latest_limits["limits"]["windows"]:
                minutes = entry["minutes"]
                kind = "five_hour" if minutes == 300 else "weekly" if minutes == 10080 else None
                if kind and kind not in windows:
                    windows[kind] = _window(kind, None, entry["used_percent"], now_ms, observed, reset_ms=entry["resets_at_ms"], profile=limits_profile)
                elif minutes:
                    extras.append(_window(f"w{minutes}", f"Janela de {minutes} min", entry["used_percent"], now_ms, observed, reset_ms=entry["resets_at_ms"], profile=limits_profile))
            plan = latest_limits["limits"]["plan"]
        main = _freshest(readings)
        if main:
            for kind, reading in main["windows"].items():
                current = windows.get(kind)
                if current is None or (reading.get("observed_at_unix_ms") or 0) > (current.get("observed_at_unix_ms") or 0):
                    windows[kind] = reading

        if not day_dirs:
            session_source = _source("session_logs", "Sessões locais do Codex (.jsonl)", False, "pasta de sessões não encontrada")
        else:
            session_source = _source(
                "session_logs",
                "Sessões locais do Codex (.jsonl)",
                sessions > 0,
                None if sessions else f"nenhum token_count nas últimas {self.lookback_hours:g} h",
                last_activity,
            )
            if sessions and latest_limits is None:
                session_source["reason"] = "rate_limits ausente nas sessões lidas (comum em codex exec)"

        accounts = [_account(r, now_ms) for r in sorted(readings, key=lambda r: r["profile"])]
        provenance = _ESTIMATED if capped else _MEASURED
        scope = (
            _scope(
                self.lookback_hours,
                sessions,
                capped,
                _session_scope_label(self.lookback_hours, capped),
                tail_truncated=False,
                files_read=len(picked),
                files_found=len(candidates),
                profiles=by_profile,
            )
            if sessions
            else None
        )
        return _reading(
            "codex",
            now_ms,
            [session_source, quota_source],
            windows=windows,
            extra_windows=extras + (main["extra_windows"] if main else []),
            accounts=accounts,
            tokens=_token_summary(totals, provenance, sessions) if sessions else None,
            usage_scope=scope,
            context=_context(
                latest_info["last"].get("total_tokens") if latest_info and latest_info["last"] else None,
                latest_info["context_window"] if latest_info else None,
                latest_info["model"] if latest_info else None,
                latest_info["info_at_ms"] if latest_info else None,
                info_profile if latest_info else None,
            ),
            models=_models_list(per_model, provenance),
            plan=plan,
            exhausted=bool(main and main["exhausted"]),
            exhausted_until_iso=main["exhausted_until_iso"] if main else None,
            last_activity_ms=last_activity,
        )

    def _agy(self, paths: LocalSourcePaths, now_ms: int) -> Dict[str, Any]:
        readings, reading_source = self._leituras(paths.quota_dir, "leitura-agy-", "quota_reading", "Leitura de cota do AGY (aviso-cota)", now_ms)
        by_profile = {r["profile"]: r for r in readings}
        parsed: List[Dict[str, Any]] = []
        reasons: List[str] = []
        files = self._quota_files(paths.quota_dir, "agy-quota-cache-") if paths.quota_dir is not None else []
        for profile, path in files:
            doc, reason = self._json_file(path)
            if doc is None:
                reasons.append(f"{profile}: {reason}")
                continue
            preferred = by_profile.get(profile, {}).get("window_ids", [])
            parsed.append(_parse_agy_cache(doc, now_ms, profile, preferred))
        cached_profiles = {p["profile"] for p in parsed}
        parsed += [r for r in readings if r["profile"] not in cached_profiles]

        observed = max((p["observed_at_unix_ms"] or 0 for p in parsed), default=0) or None
        if paths.quota_dir is None:
            cache_source = _source("quota_cache", "Cache de cotas do AGY (quota-guard)", False, "pasta do aviso-cota não encontrada")
        elif not files:
            cache_source = _source("quota_cache", "Cache de cotas do AGY (quota-guard)", False, "nenhum cache de cota registrado")
        else:
            cache_source = _source(
                "quota_cache",
                "Cache de cotas do AGY (quota-guard)",
                len(cached_profiles) > 0,
                "; ".join(reasons) or None,
                observed,
            )
        main = _freshest(parsed)
        return _reading(
            "agy",
            now_ms,
            [cache_source, reading_source],
            windows=dict(main["windows"]) if main else {},
            extra_windows=main["extra_windows"] if main else [],
            accounts=[_account(p, now_ms) for p in sorted(parsed, key=lambda p: p["profile"])],
            exhausted=bool(main and main["exhausted"] and not main.get("exhausted_model")),
            exhausted_until_iso=main["exhausted_until_iso"] if main else None,
            exhausted_model=main.get("exhausted_model") if main and main["exhausted"] else None,
        )

    def _opencode_family(self, paths: LocalSourcePaths, now_ms: int) -> Dict[str, Dict[str, Any]]:
        rows: Optional[List[Dict[str, Any]]] = None
        db_reasons: List[str] = []
        seen_sessions: set = set()
        for profile, db in paths.opencode_dbs:
            db_rows, reason = self._opencode_rows(db, now_ms)
            if db_rows is None:
                db_reasons.append(f"{profile}: {reason}")
                continue
            rows = rows or []
            for row in db_rows:
                if row["session"] not in seen_sessions:
                    seen_sessions.add(row["session"])
                    rows.append(dict(row, profile=profile))
        db_reason = "; ".join(db_reasons) or "banco local não encontrado"
        readings, quota_source = self._leituras(paths.quota_dir, "leitura-opencode-", "quota_reading", "Contagem local do OpenCode (aviso-cota)", now_ms)
        main = _freshest(readings)
        extras = main["extra_windows"] if main else []
        openrouter_extras = [w for w in extras if "openrouter" in str(w.get("window"))]
        opencode_extras = [w for w in extras if w not in openrouter_extras]

        out: Dict[str, Dict[str, Any]] = {}
        for pid in ("opencode", "openrouter"):
            mine = [r for r in rows or [] if (r["provider_id"] == "openrouter") == (pid == "openrouter")]
            per_model: Dict[str, Dict[str, Any]] = {}
            totals: Dict[str, Optional[int]] = {}
            by_profile: Dict[str, Dict[str, Any]] = {}
            spent: Optional[float] = None
            last_activity: Optional[int] = None
            for row in mine:
                label = row["model"] if pid == "openrouter" else f"{row['provider_id']}/{row['model']}"
                entry = per_model.setdefault(label, {"tokens": {}, "runs": 0, "cost": None})
                _add_tokens(entry["tokens"], row["tokens"])
                _add_tokens(totals, row["tokens"])
                _add_profile_share(by_profile, row["profile"], row["tokens"], 1)
                entry["runs"] += 1
                if row["cost"] is not None:
                    entry["cost"] = (entry["cost"] or 0.0) + row["cost"]
                    spent = (spent or 0.0) + row["cost"]
                last_activity = max(last_activity or 0, row["updated_ms"] or 0) or None
            label = "Banco local do OpenCode (somente leitura)"
            if rows is None:
                db_source = _source("opencode_db", label, False, db_reason)
            else:
                db_source = _source(
                    "opencode_db",
                    label,
                    bool(mine),
                    None if mine else f"nenhuma sessão nas últimas {self.lookback_hours:g} h",
                    last_activity,
                )
            sources = [db_source]
            if pid == "openrouter":
                sources.append(dict(quota_source, available=quota_source["available"] and bool(openrouter_extras)))
            elif opencode_extras:
                sources.append(quota_source)
            note = (
                "OpenRouter não tem janela de 5 h nem semanal de assinatura."
                if pid == "openrouter"
                else "OpenCode não tem janela própria de 5 h nem semanal; depende do provedor conectado."
            )
            out[pid] = _reading(
                pid,
                now_ms,
                sources,
                windows={"five_hour": _unsupported_window("five_hour", note), "weekly": _unsupported_window("weekly", note)},
                extra_windows=openrouter_extras if pid == "openrouter" else opencode_extras,
                tokens=_token_summary(totals, _MEASURED, len(mine)) if mine else None,
                usage_scope=_scope(
                    self.lookback_hours,
                    len(mine),
                    False,
                    f"sessões do OpenCode atualizadas nas últimas {self.lookback_hours:g} h",
                    profiles=by_profile,
                )
                if mine
                else None,
                cost={"spent_usd": round(spent, 6), "provenance": _MEASURED} if spent is not None else None,
                models=_models_list(per_model, _MEASURED),
                windows_supported=False,
                remote_lookup=dict(OPENROUTER_REMOTE_LOOKUP) if pid == "openrouter" else None,
                last_activity_ms=last_activity,
            )
        return out

    # ---- sources

    def _leituras(
        self, quota_dir: Optional[Path], prefix: str, kind: str, label: str, now_ms: int
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if quota_dir is None:
            return [], _source(kind, label, False, "pasta do aviso-cota não encontrada")
        files = self._quota_files(quota_dir, prefix)
        if not files:
            return [], _source(kind, label, False, "nenhuma leitura registrada")
        parsed: List[Dict[str, Any]] = []
        reasons: List[str] = []
        for profile, path in files:
            doc, reason = self._json_file(path)
            if doc is None:
                reasons.append(f"{profile}: {reason}")
                continue
            parsed.append(_parse_leitura(doc, now_ms, profile))
        observed = max((p["observed_at_unix_ms"] or 0 for p in parsed), default=0) or None
        return parsed, _source(kind, label, bool(parsed), "; ".join(reasons) or None, observed)

    def _quota_files(self, quota_dir: Path, prefix: str) -> List[Tuple[str, Path]]:
        try:
            with os.scandir(quota_dir) as entries:
                names = sorted(e.name for e in entries if e.name.startswith(prefix) and e.name.endswith(".json"))
        except OSError:
            return []
        out = []
        for name in names[:MAX_QUOTA_FILES]:
            profile = name[len(prefix) : -len(".json")]
            if profile and "." not in profile:
                out.append((_safe_id(profile, "padrao"), quota_dir / name))
        return out

    def _subdirs(self, root: Path, limit: int) -> Tuple[List[Path], int]:
        """Subfolders changed most recently first (the cap keeps the active projects), and how many there are."""
        found: List[Tuple[int, str]] = []
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            found.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.path))
                    except OSError:
                        continue
        except OSError:
            return [], 0
        found.sort(key=lambda d: (-d[0], d[1]))
        return [Path(p) for _, p in found[:limit]], len(found)

    def _recent_files(self, dirs: Iterable[Path], now_ms: int) -> List[Tuple[int, int, Path]]:
        cutoff_ns = int((now_ms - self.lookback_hours * 3600 * 1000) * 1_000_000)
        found: List[Tuple[int, int, Path]] = []
        for directory in dirs:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".jsonl"):
                            continue
                        try:
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if st.st_mtime_ns >= cutoff_ns:
                            found.append((st.st_mtime_ns, st.st_size, Path(entry.path)))
            except OSError:
                continue
        found.sort(key=lambda f: f[0], reverse=True)
        return found

    def _codex_day_dirs(self, root: Path, now_ms: int) -> List[Path]:
        if not root.is_dir():
            return []
        dirs = [root]
        now = dt.datetime.fromtimestamp(now_ms / 1000.0, tz=dt.timezone.utc)
        for offset in range(-1, int(self.lookback_hours // 24) + 2):
            day = (now - dt.timedelta(days=offset)).date()
            candidate = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            if candidate not in dirs and candidate.is_dir():
                dirs.append(candidate)
        return dirs

    def _remember(self, key: Tuple[str, str], stamp: Any, value: Any) -> None:
        self._file_cache.pop(key, None)
        self._file_cache[key] = (stamp, value)
        while len(self._file_cache) > MAX_FILE_CACHE_ENTRIES:
            self._file_cache.pop(next(iter(self._file_cache)))

    def _json_file(self, path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            st = path.stat()
        except FileNotFoundError:
            return None, "arquivo ausente"
        except OSError:
            return None, "arquivo inacessível"
        key = ("json", str(path))
        stamp = (st.st_mtime_ns, st.st_size)
        hit = self._file_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        result: Tuple[Optional[Dict[str, Any]], Optional[str]]
        if st.st_size > self.max_json_bytes:
            result = (None, "arquivo excede o limite de leitura")
        else:
            try:
                with open(path, "rb") as fh:
                    raw = fh.read(self.max_json_bytes + 1)
            except OSError:
                return None, "arquivo bloqueado ou inacessível"
            self.stats["files_read"] += 1
            self.stats["bytes_read"] += len(raw)
            try:
                data = json.loads(raw.decode("utf-8"))
                result = (data, None) if isinstance(data, dict) else (None, "formato inesperado")
            except (UnicodeDecodeError, ValueError, RecursionError):
                result = (None, "arquivo ilegível ou corrompido")
        self._remember(key, stamp, result)
        return result

    def _session_file(
        self, kind: str, path: Path, mtime_ns: int, size: int, parser: Callable[[bytes, bool], Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        key = (kind, str(path))
        stamp = (mtime_ns, size)
        hit = self._file_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        try:
            with open(path, "rb") as fh:
                if size > self.max_tail_bytes:
                    fh.seek(size - self.max_tail_bytes)
                    data = fh.read(self.max_tail_bytes)
                    cut = data.find(b"\n")
                    data = data[cut + 1 :] if cut >= 0 else b""
                    truncated = True
                else:
                    data = fh.read(self.max_tail_bytes)
                    truncated = False
        except OSError:
            return None
        self.stats["files_read"] += 1
        self.stats["bytes_read"] += len(data)
        try:
            result: Optional[Dict[str, Any]] = parser(data, truncated)
        except Exception:
            result = None
        self._remember(key, stamp, result)
        return result

    def _opencode_rows(self, db: Path, now_ms: int) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        try:
            st = db.stat()
        except OSError:
            return None, "banco local não encontrado"
        try:
            wal = Path(str(db) + "-wal").stat()
            wal_stamp: Tuple[int, int] = (wal.st_mtime_ns, wal.st_size)
        except OSError:
            wal_stamp = (0, 0)
        cutoff_ms = int(now_ms - self.lookback_hours * 3600 * 1000)
        key = ("opencode_db", str(db))
        stamp = (st.st_mtime_ns, st.st_size, wal_stamp, cutoff_ms // 600_000)
        hit = self._file_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        result = self._query_opencode(db, cutoff_ms)
        if result[0] is not None:
            self._remember(key, stamp, result)
        return result

    def _query_opencode(self, db: Path, cutoff_ms: int) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
            conn.execute("PRAGMA query_only = ON")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows: List[Dict[str, Any]] = []
            seen: set = set()
            used_table = False
            for table in ("session_v2", "session"):
                if table not in tables:
                    continue
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                metrics = [c for c in _OPENCODE_SESSION_COLUMNS if c in columns]
                if not {"id", "model", "time_updated"} <= columns or not metrics:
                    continue
                used_table = True
                unit_ms = _time_unit_ms(conn.execute(f"SELECT MAX(time_updated) FROM {table}").fetchone()[0])
                query = (
                    f"SELECT id, model, time_updated, {', '.join(metrics)} FROM {table} "
                    "WHERE time_updated >= ? ORDER BY time_updated DESC LIMIT ?"
                )
                for row in conn.execute(query, (cutoff_ms / unit_ms, MAX_SQLITE_ROWS)):
                    if row[0] in seen:
                        continue
                    seen.add(row[0])
                    values = dict(zip(metrics, row[3:]))
                    rows.append(
                        _opencode_row(
                            row[0],
                            _opencode_model(row[1]),
                            values.get("tokens_input"),
                            values.get("tokens_output"),
                            values.get("tokens_reasoning"),
                            values.get("tokens_cache_read"),
                            values.get("tokens_cache_write"),
                            values.get("cost"),
                            row[2],
                            unit_ms,
                        )
                    )
            if not used_table and "message" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(message)")}
                if {"id", "data", "time_created"} <= columns:
                    used_table = True
                    unit_ms = _time_unit_ms(conn.execute("SELECT MAX(time_created) FROM message").fetchone()[0])
                    query = (
                        "SELECT id, json_extract(data, '$.providerID'), json_extract(data, '$.modelID'), "
                        "json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.output'), "
                        "json_extract(data, '$.tokens.reasoning'), json_extract(data, '$.tokens.cache.read'), "
                        "json_extract(data, '$.tokens.cache.write'), json_extract(data, '$.cost'), time_created "
                        "FROM message WHERE time_created >= ? AND json_extract(data, '$.role') = 'assistant' "
                        "ORDER BY time_created DESC LIMIT ?"
                    )
                    for row in conn.execute(query, (cutoff_ms / unit_ms, MAX_SQLITE_ROWS * 4)):
                        rows.append(_opencode_row(row[0], _opencode_ids(row[1], row[2]), *row[3:], unit_ms))
            if not used_table:
                return None, "esquema do banco local não reconhecido"
            return rows, None
        except (sqlite3.Error, OSError, ValueError):
            return None, "banco local ilegível ou bloqueado"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass


_DEFAULT_READER: Optional[LocalProviderReader] = None
_DEFAULT_READER_LOCK = threading.Lock()


def read_local_provider_telemetry(now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Shared reader for the IPC handler: one discovery and one 30 s cache per Core process."""
    global _DEFAULT_READER
    with _DEFAULT_READER_LOCK:
        if _DEFAULT_READER is None:
            _DEFAULT_READER = LocalProviderReader()
        reader = _DEFAULT_READER
    return reader.read(now_ms=now_ms)
