"""Cognitive Telemetry Module for tracking model tokens, latency, cost, and tool usage.

Provides observational, passive instrumentation for AI Coordinator missions and workers,
with strict data provenance differentiation (measured, estimated, unknown), vendor-specific
cache accounting contracts, audit-grade record immutability, and invariant non-interference.
"""
from __future__ import annotations

import contextlib
import dataclasses
from dataclasses import dataclass, field
import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("ai_coordinator.telemetry")

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_ISO_FMT)


class MetricProvenance:
    """Provenance classifications for accounting metrics."""
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"

    ALL = {MEASURED, ESTIMATED, UNKNOWN}

    @classmethod
    def validate(cls, value: str) -> str:
        if not isinstance(value, str) or value not in cls.ALL:
            raise ValueError(f"Proveniência inválida: '{value}'. Permitidos: {sorted(cls.ALL)}")
        return value


# Prefixed key-value assignments: replace the secret value, preserving key and separator
_PREFIXED_SENSITIVE_PATTERNS = [
    (re.compile(r"""(password|secret|token|api[_-]?key|credential)\s*([:=])\s*['"]?[^'"\s]+['"]?""", re.IGNORECASE), r"\1\2[REDACTED_SECRET]"),
    (re.compile(r"""(bearer\s+)[A-Za-z0-9_\-\.]{10,}""", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
    (re.compile(r"""(basic\s+)[A-Za-z0-9+/=]{10,}""", re.IGNORECASE), r"\1[REDACTED_SECRET]"),
]

# Standalone high-entropy secret patterns (no prefix: whole match is redacted)
_STANDALONE_SENSITIVE_PATTERNS = [
    re.compile(r"""\b(sk-[A-Za-z0-9_\-]{20,})\b""", re.IGNORECASE),
    re.compile(r"""\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b""", re.IGNORECASE),
    re.compile(r"""\b(AIza[0-9A-Za-z\-_]{35})\b"""),
    re.compile(r"""\b(AKIA[0-9A-Z]{16})\b"""),
    re.compile(r"""\b(xox[baprs]-[0-9A-Za-z\-]{10,})\b"""),
]


def scrub_sensitive_text(text: Optional[str]) -> Optional[str]:
    """Scrub sensitive credentials, passwords, and tokens from telemetry text.

    Guarantees complete redaction whether secrets appear as key-value pairs or as isolated tokens.
    """
    if text is None:
        return None
    scrubbed = str(text)
    for pat, repl in _PREFIXED_SENSITIVE_PATTERNS:
        scrubbed = pat.sub(repl, scrubbed)
    for pat in _STANDALONE_SENSITIVE_PATTERNS:
        scrubbed = pat.sub("[REDACTED_SECRET]", scrubbed)
    return scrubbed


def _disambiguate_key(key: str, existing_keys: Set[str]) -> str:
    """Disambiguate key collisions during redaction deterministically."""
    if key not in existing_keys:
        return key
    counter = 1
    candidate = f"{key}_{counter}"
    while candidate in existing_keys:
        counter += 1
        candidate = f"{key}_{counter}"
    return candidate


def scrub_sensitive_value(value: Any) -> Any:
    """Recursively scrub sensitive text and data structures across any nesting depth.

    Normalizes all sequences (lists, tuples, sets) into standard JSON-compatible lists,
    preventing serialization exceptions during database persistence.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return scrub_sensitive_text(value)
    if isinstance(value, dict):
        return scrub_sensitive_dict(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        if isinstance(value, (set, frozenset)):
            try:
                items = sorted(list(value), key=lambda x: str(x))
            except Exception:
                items = list(value)
        else:
            items = list(value)
        return [scrub_sensitive_value(x) for x in items]
    if isinstance(value, (int, bool)):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return scrub_sensitive_dict(value.to_dict())
    return scrub_sensitive_text(str(value))


def scrub_sensitive_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Recursively scrub sensitive keys and token values from telemetry metadata across all nested containers.

    Sanitizes both keys and values, ensures safe collision resolution, and produces
    100% JSON-compliant structures safe for database serialization.
    """
    if not data or not isinstance(data, dict):
        return {}
    scrubbed: Dict[str, Any] = {}
    for k, v in data.items():
        k_str = str(k)
        clean_k = scrub_sensitive_text(k_str) or "[REDACTED_SECRET]"
        if any(term in k_str.lower() for term in ("secret", "token", "password", "key", "auth", "credential", "api_key", "apikey")):
            clean_v = "[REDACTED_SECRET]"
        else:
            clean_v = scrub_sensitive_value(v)
        final_k = _disambiguate_key(clean_k, set(scrubbed.keys()))
        scrubbed[final_k] = clean_v
    return scrubbed


@dataclass(frozen=True)
class TokenMetrics:
    """Immutable token accounting metrics with explicit data provenance.

    Differentiates actually measured/estimated zero tokens (0) from unknown/omitted tokens (None).
    """
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    provenance: str = MetricProvenance.UNKNOWN

    def __post_init__(self) -> None:
        MetricProvenance.validate(self.provenance)
        in_t = None if self.input_tokens is None else max(0, int(self.input_tokens))
        out_t = None if self.output_tokens is None else max(0, int(self.output_tokens))
        read_t = None if self.cache_read_tokens is None else max(0, int(self.cache_read_tokens))
        creat_t = None if self.cache_creation_tokens is None else max(0, int(self.cache_creation_tokens))

        tot_t = self.total_tokens
        if tot_t is not None:
            tot_t = max(0, int(tot_t))
        elif in_t is not None or out_t is not None:
            parts = [p for p in (in_t, out_t, read_t, creat_t) if p is not None]
            tot_t = sum(parts) if parts else None

        object.__setattr__(self, "input_tokens", in_t)
        object.__setattr__(self, "output_tokens", out_t)
        object.__setattr__(self, "cache_read_tokens", read_t)
        object.__setattr__(self, "cache_creation_tokens", creat_t)
        object.__setattr__(self, "total_tokens", tot_t)
        object.__setattr__(self, "provenance", self.provenance)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TokenMetrics:
        if not data:
            return cls()
        return cls(
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            cache_read_tokens=data.get("cache_read_tokens"),
            cache_creation_tokens=data.get("cache_creation_tokens"),
            total_tokens=data.get("total_tokens"),
            provenance=data.get("provenance", MetricProvenance.UNKNOWN),
        )


def normalize_provider_tokens(
    provider: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    provenance: str = MetricProvenance.MEASURED,
) -> TokenMetrics:
    """Normalize token metrics adhering to vendor accounting contracts to prevent double-counting.

    Vendor Contracts:
    - OpenAI / Azure: `usage.prompt_tokens` includes `cached_tokens`.
      `total_tokens = prompt_tokens + completion_tokens`.
      (cache_read_tokens is already accounted for within prompt_tokens).
    - Anthropic: `usage.input_tokens` is disjoint from cache tokens.
      `total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens`.
    - Google Gemini: `usageMetadata.promptTokenCount` includes `cachedContentTokenCount`.
      `total_tokens = promptTokenCount + candidatesTokenCount`.
    - Generic: If total_tokens is reported, use it; otherwise compute sum of non-null components.
    """
    prov = str(provider or "").strip().lower()
    in_t = None if input_tokens is None else max(0, int(input_tokens))
    out_t = None if output_tokens is None else max(0, int(output_tokens))
    read_t = None if cache_read_tokens is None else max(0, int(cache_read_tokens))
    creat_t = None if cache_creation_tokens is None else max(0, int(cache_creation_tokens))

    computed_total = total_tokens
    if computed_total is None and in_t is not None and out_t is not None:
        if prov in ("openai", "azure"):
            # OpenAI prompt_tokens already includes cached_tokens
            computed_total = in_t + out_t
        elif prov in ("anthropic", "claude"):
            # Anthropic input_tokens is strictly uncached input
            computed_total = in_t + out_t + (read_t or 0) + (creat_t or 0)
        elif prov in ("google", "gemini"):
            # Gemini promptTokenCount already includes cachedContentTokenCount
            computed_total = in_t + out_t
        else:
            computed_total = in_t + out_t + (read_t or 0) + (creat_t or 0)

    return TokenMetrics(
        input_tokens=in_t,
        output_tokens=out_t,
        cache_read_tokens=read_t,
        cache_creation_tokens=creat_t,
        total_tokens=computed_total,
        provenance=provenance,
    )


@dataclass(frozen=True)
class LatencyMetrics:
    """Immutable latency metrics in milliseconds with separate provenance for measured vs derived durations."""
    total_duration_ms: float = 0.0
    ttft_ms: Optional[float] = None
    generation_duration_ms: Optional[float] = None
    tool_duration_ms: Optional[float] = None
    supervisor_overhead_ms: Optional[float] = None
    latency_provenance: str = MetricProvenance.MEASURED
    generation_duration_provenance: str = MetricProvenance.ESTIMATED
    provenance: Optional[str] = None

    def __post_init__(self) -> None:
        lat_prov = self.provenance if self.provenance is not None else self.latency_provenance
        MetricProvenance.validate(lat_prov)
        MetricProvenance.validate(self.generation_duration_provenance)
        tot = max(0.0, float(self.total_duration_ms))
        ttft = None if self.ttft_ms is None else max(0.0, float(self.ttft_ms))
        gen = None if self.generation_duration_ms is None else max(0.0, float(self.generation_duration_ms))
        tool = None if self.tool_duration_ms is None else max(0.0, float(self.tool_duration_ms))
        superv = None if self.supervisor_overhead_ms is None else max(0.0, float(self.supervisor_overhead_ms))

        object.__setattr__(self, "total_duration_ms", tot)
        object.__setattr__(self, "ttft_ms", ttft)
        object.__setattr__(self, "generation_duration_ms", gen)
        object.__setattr__(self, "tool_duration_ms", tool)
        object.__setattr__(self, "supervisor_overhead_ms", superv)
        object.__setattr__(self, "latency_provenance", lat_prov)
        object.__setattr__(self, "generation_duration_provenance", self.generation_duration_provenance)
        object.__setattr__(self, "provenance", lat_prov)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LatencyMetrics:
        if not data:
            return cls()
        return cls(
            total_duration_ms=data.get("total_duration_ms", 0.0),
            ttft_ms=data.get("ttft_ms"),
            generation_duration_ms=data.get("generation_duration_ms"),
            tool_duration_ms=data.get("tool_duration_ms"),
            supervisor_overhead_ms=data.get("supervisor_overhead_ms"),
            latency_provenance=data.get("latency_provenance", MetricProvenance.MEASURED),
            generation_duration_provenance=data.get("generation_duration_provenance", MetricProvenance.ESTIMATED),
        )


@dataclass(frozen=True)
class CostMetrics:
    """Immutable monetary cost accounting metrics in USD with explicit pricing versioning."""
    input_cost_usd: Optional[float] = None
    output_cost_usd: Optional[float] = None
    cache_read_cost_usd: Optional[float] = None
    cache_creation_cost_usd: Optional[float] = None
    total_cost_usd: Optional[float] = None
    provenance: str = MetricProvenance.UNKNOWN
    pricing_version: Optional[str] = None

    def __post_init__(self) -> None:
        MetricProvenance.validate(self.provenance)
        in_c = None if self.input_cost_usd is None else max(0.0, float(self.input_cost_usd))
        out_c = None if self.output_cost_usd is None else max(0.0, float(self.output_cost_usd))
        read_c = None if self.cache_read_cost_usd is None else max(0.0, float(self.cache_read_cost_usd))
        creat_c = None if self.cache_creation_cost_usd is None else max(0.0, float(self.cache_creation_cost_usd))

        tot_c = self.total_cost_usd
        if tot_c is not None:
            tot_c = max(0.0, float(tot_c))
        else:
            components = [c for c in (in_c, out_c, read_c, creat_c) if c is not None]
            tot_c = sum(components) if components else None

        object.__setattr__(self, "input_cost_usd", in_c)
        object.__setattr__(self, "output_cost_usd", out_c)
        object.__setattr__(self, "cache_read_cost_usd", read_c)
        object.__setattr__(self, "cache_creation_cost_usd", creat_c)
        object.__setattr__(self, "total_cost_usd", tot_c)
        object.__setattr__(self, "provenance", self.provenance)
        object.__setattr__(self, "pricing_version", self.pricing_version)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CostMetrics:
        if not data:
            return cls()
        return cls(
            input_cost_usd=data.get("input_cost_usd"),
            output_cost_usd=data.get("output_cost_usd"),
            cache_read_cost_usd=data.get("cache_read_cost_usd"),
            cache_creation_cost_usd=data.get("cache_creation_cost_usd"),
            total_cost_usd=data.get("total_cost_usd"),
            provenance=data.get("provenance", MetricProvenance.UNKNOWN),
            pricing_version=data.get("pricing_version"),
        )


@dataclass(frozen=True)
class CognitiveTelemetryRecord:
    """Immutable observational record of a single cognitive execution invocation."""
    task_id: str
    id: Optional[int] = None
    run_id: Optional[str] = None
    worker_identity: Optional[str] = None
    worker_pid: Optional[int] = None
    plan_revision: Optional[int] = None
    provider: str = "unknown"
    model_name: str = "unknown"
    tokens: TokenMetrics = field(default_factory=TokenMetrics)
    latencies: LatencyMetrics = field(default_factory=LatencyMetrics)
    cost: CostMetrics = field(default_factory=CostMetrics)
    tool_calls_count: int = 0
    retries_count: int = 0
    stream_chunks_count: int = 0
    success: bool = True
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError("task_id obrigatorio para registro de telemetria")
        tc = max(0, int(self.tool_calls_count))
        rc = max(0, int(self.retries_count))
        sc = max(0, int(self.stream_chunks_count))
        prov = str(self.provider or "unknown").strip().lower()
        model = str(self.model_name or "unknown").strip()
        err_msg = scrub_sensitive_text(self.error_message) if self.error_message else None
        meta = scrub_sensitive_dict(self.metadata) if self.metadata else {}

        object.__setattr__(self, "tool_calls_count", tc)
        object.__setattr__(self, "retries_count", rc)
        object.__setattr__(self, "stream_chunks_count", sc)
        object.__setattr__(self, "provider", prov)
        object.__setattr__(self, "model_name", model)
        object.__setattr__(self, "error_message", err_msg)
        object.__setattr__(self, "metadata", meta)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "worker_identity": self.worker_identity,
            "worker_pid": self.worker_pid,
            "plan_revision": self.plan_revision,
            "provider": self.provider,
            "model_name": self.model_name,
            "tokens": self.tokens.to_dict(),
            "latencies": self.latencies.to_dict(),
            "cost": self.cost.to_dict(),
            "tool_calls_count": self.tool_calls_count,
            "retries_count": self.retries_count,
            "stream_chunks_count": self.stream_chunks_count,
            "success": self.success,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CognitiveTelemetryRecord:
        tokens_data = data.get("tokens", {})
        latencies_data = data.get("latencies", {})
        cost_data = data.get("cost", {})
        return cls(
            id=data.get("id"),
            task_id=data["task_id"],
            run_id=data.get("run_id"),
            worker_identity=data.get("worker_identity"),
            worker_pid=data.get("worker_pid"),
            plan_revision=data.get("plan_revision"),
            provider=data.get("provider", "unknown"),
            model_name=data.get("model_name", "unknown"),
            tokens=TokenMetrics.from_dict(tokens_data) if isinstance(tokens_data, dict) else TokenMetrics(),
            latencies=LatencyMetrics.from_dict(latencies_data) if isinstance(latencies_data, dict) else LatencyMetrics(),
            cost=CostMetrics.from_dict(cost_data) if isinstance(cost_data, dict) else CostMetrics(),
            tool_calls_count=data.get("tool_calls_count", 0),
            retries_count=data.get("retries_count", 0),
            stream_chunks_count=data.get("stream_chunks_count", 0),
            success=bool(data.get("success", True)),
            error_category=data.get("error_category"),
            error_message=data.get("error_message"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at") or _now(),
        )

    @classmethod
    def from_db_row(cls, row: Union[sqlite3.Row, Dict[str, Any]]) -> CognitiveTelemetryRecord:
        r = dict(row)
        tokens = TokenMetrics(
            input_tokens=r.get("input_tokens"),
            output_tokens=r.get("output_tokens"),
            cache_read_tokens=r.get("cache_read_tokens"),
            cache_creation_tokens=r.get("cache_creation_tokens"),
            total_tokens=r.get("total_tokens"),
            provenance=r.get("tokens_provenance") or MetricProvenance.UNKNOWN,
        )
        latencies = LatencyMetrics(
            total_duration_ms=r.get("total_duration_ms") or 0.0,
            ttft_ms=r.get("ttft_ms"),
            generation_duration_ms=r.get("generation_duration_ms"),
            tool_duration_ms=r.get("tool_duration_ms"),
            supervisor_overhead_ms=r.get("supervisor_overhead_ms"),
            latency_provenance=r.get("latency_provenance") or MetricProvenance.MEASURED,
            generation_duration_provenance=r.get("generation_duration_provenance") or MetricProvenance.ESTIMATED,
        )
        cost = CostMetrics(
            total_cost_usd=r.get("total_cost_usd"),
            provenance=r.get("cost_provenance") or MetricProvenance.UNKNOWN,
            pricing_version=r.get("pricing_version"),
        )
        meta_raw = r.get("metadata")
        try:
            meta = json.loads(meta_raw) if meta_raw and isinstance(meta_raw, str) else (meta_raw or {})
        except Exception:
            meta = {}

        return cls(
            id=r.get("id"),
            task_id=r["task_id"],
            run_id=r.get("run_id"),
            worker_identity=r.get("worker_identity"),
            worker_pid=r.get("worker_pid"),
            plan_revision=r.get("plan_revision"),
            provider=r.get("provider", "unknown"),
            model_name=r.get("model_name", "unknown"),
            tokens=tokens,
            latencies=latencies,
            cost=cost,
            tool_calls_count=r.get("tool_calls_count") or 0,
            retries_count=r.get("retries_count") or 0,
            stream_chunks_count=r.get("stream_chunks_count") or 0,
            success=bool(r.get("success", 1)),
            error_category=r.get("error_category"),
            error_message=r.get("error_message"),
            metadata=meta,
            created_at=r.get("created_at") or _now(),
        )


class PricingRegistry:
    """Transparent pricing registry in USD per 1,000,000 tokens for baseline cost estimation.

    Estimations are strictly marked as MetricProvenance.ESTIMATED and stamped with REGISTRY_VERSION.
    Unrecognized models return MetricProvenance.UNKNOWN with total_cost_usd=None to prevent fabricated numbers.
    """
    REGISTRY_VERSION = "2026.11.01"

    # Rate format: { "input_per_m": float, "output_per_m": float, "cache_read_per_m": float, "cache_creation_per_m": float }
    _REGISTRY: Dict[str, Dict[str, float]] = {
        # Anthropic reference pricing
        "claude-3-5-sonnet": {"input_per_m": 3.0, "output_per_m": 15.0, "cache_read_per_m": 0.30, "cache_creation_per_m": 3.75},
        "claude-3-5-sonnet-20241022": {"input_per_m": 3.0, "output_per_m": 15.0, "cache_read_per_m": 0.30, "cache_creation_per_m": 3.75},
        "claude-3-opus": {"input_per_m": 15.0, "output_per_m": 75.0, "cache_read_per_m": 1.50, "cache_creation_per_m": 18.75},
        "claude-3-5-haiku": {"input_per_m": 1.0, "output_per_m": 5.0, "cache_read_per_m": 0.10, "cache_creation_per_m": 1.25},
        "claude-3-haiku": {"input_per_m": 0.25, "output_per_m": 1.25, "cache_read_per_m": 0.025, "cache_creation_per_m": 0.30},

        # OpenAI reference pricing
        "gpt-4o": {"input_per_m": 2.50, "output_per_m": 10.0, "cache_read_per_m": 1.25, "cache_creation_per_m": 0.0},
        "gpt-4o-2024-08-06": {"input_per_m": 2.50, "output_per_m": 10.0, "cache_read_per_m": 1.25, "cache_creation_per_m": 0.0},
        "gpt-4o-mini": {"input_per_m": 0.15, "output_per_m": 0.60, "cache_read_per_m": 0.075, "cache_creation_per_m": 0.0},
        "o1": {"input_per_m": 15.0, "output_per_m": 60.0, "cache_read_per_m": 7.50, "cache_creation_per_m": 0.0},
        "o1-mini": {"input_per_m": 3.0, "output_per_m": 12.0, "cache_read_per_m": 1.50, "cache_creation_per_m": 0.0},

        # Google Gemini reference pricing
        "gemini-1.5-pro": {"input_per_m": 3.50, "output_per_m": 10.50, "cache_read_per_m": 0.875, "cache_creation_per_m": 0.0},
        "gemini-1.5-flash": {"input_per_m": 0.075, "output_per_m": 0.30, "cache_read_per_m": 0.01875, "cache_creation_per_m": 0.0},
        "gemini-2.0-flash": {"input_per_m": 0.10, "output_per_m": 0.40, "cache_read_per_m": 0.025, "cache_creation_per_m": 0.0},

        # Synthetic test reference
        "test-model": {"input_per_m": 1.0, "output_per_m": 2.0, "cache_read_per_m": 0.25, "cache_creation_per_m": 0.50},
    }

    @classmethod
    def register_pricing(
        cls,
        model_name: str,
        input_per_m: float,
        output_per_m: float,
        cache_read_per_m: float = 0.0,
        cache_creation_per_m: float = 0.0,
    ) -> None:
        """Register or override a model's token rate in USD per 1M tokens."""
        key = cls._normalize_name(model_name)
        cls._REGISTRY[key] = {
            "input_per_m": max(0.0, float(input_per_m)),
            "output_per_m": max(0.0, float(output_per_m)),
            "cache_read_per_m": max(0.0, float(cache_read_per_m)),
            "cache_creation_per_m": max(0.0, float(cache_creation_per_m)),
        }

    @classmethod
    def get_pricing(cls, model_name: str) -> Optional[Dict[str, float]]:
        """Look up reference rates for a model name."""
        key = cls._normalize_name(model_name)
        if key in cls._REGISTRY:
            return cls._REGISTRY[key]
        for registered_key, rates in cls._REGISTRY.items():
            if key.startswith(registered_key) or registered_key.startswith(key):
                return rates
        return None

    @classmethod
    def estimate_cost(
        cls,
        model_name: str,
        tokens: TokenMetrics,
        provider: Optional[str] = None,
    ) -> CostMetrics:
        """Calculate estimated cost for token metrics based on reference pricing and vendor contracts.

        Returns CostMetrics with provenance=ESTIMATED and pricing_version stamped if model is known
        and essential token metrics are present. If the model is unrecognized or either input_tokens
        or output_tokens is None (unknown), returns provenance=UNKNOWN with total_cost_usd=None to
        prevent fabricated financial numbers.

        Accounting Contract:
        - OpenAI / Azure / Google Gemini: Reported prompt/input tokens already include cached tokens.
          To prevent double charging, uncached input = max(0, input_tokens - cache_read_tokens).
        - Anthropic: Reported input_tokens is disjoint from cached tokens, so uncached input = input_tokens.
        """
        rates = cls.get_pricing(model_name)
        if not rates:
            return CostMetrics(provenance=MetricProvenance.UNKNOWN)

        if tokens.input_tokens is None or tokens.output_tokens is None:
            return CostMetrics(provenance=MetricProvenance.UNKNOWN)

        prov = str(provider or "").strip().lower()
        if not prov or prov == "unknown":
            norm_name = cls._normalize_name(model_name)
            if any(k in norm_name for k in ("gpt-", "o1", "o3", "chatgpt")):
                prov = "openai"
            elif "claude" in norm_name:
                prov = "anthropic"
            elif "gemini" in norm_name:
                prov = "google"

        in_tok = tokens.input_tokens
        out_tok = tokens.output_tokens
        read_tok = tokens.cache_read_tokens or 0
        creat_tok = tokens.cache_creation_tokens or 0

        # OpenAI, Azure, and Google Gemini report prompt/input tokens inclusive of cached tokens.
        # Deduct cache_read_tokens to compute the actual uncached input billed at full rate.
        if prov in ("openai", "azure", "google", "gemini"):
            uncached_in = max(0, in_tok - read_tok)
        else:
            uncached_in = in_tok

        in_cost = (uncached_in / 1_000_000.0) * rates["input_per_m"]
        out_cost = (out_tok / 1_000_000.0) * rates["output_per_m"]
        read_cost = (read_tok / 1_000_000.0) * rates.get("cache_read_per_m", 0.0)
        creat_cost = (creat_tok / 1_000_000.0) * rates.get("cache_creation_per_m", 0.0)
        total = in_cost + out_cost + read_cost + creat_cost

        return CostMetrics(
            input_cost_usd=round(in_cost, 8),
            output_cost_usd=round(out_cost, 8),
            cache_read_cost_usd=round(read_cost, 8),
            cache_creation_cost_usd=round(creat_cost, 8),
            total_cost_usd=round(total, 8),
            provenance=MetricProvenance.ESTIMATED,
            pricing_version=cls.REGISTRY_VERSION,
        )

    @staticmethod
    def _normalize_name(model_name: str) -> str:
        name = str(model_name or "").strip().lower()
        if "/" in name:
            name = name.split("/")[-1]
        return name


class TelemetryCollector:
    """Context manager and high-precision timer for collecting cognitive metrics.

    Ensures fine-grained decomposition of latency and enforces vendor-specific token contracts.
    """

    def __init__(
        self,
        task_id: str,
        run_id: Optional[str] = None,
        worker_identity: Optional[str] = None,
        worker_pid: Optional[int] = None,
        plan_revision: Optional[int] = None,
        provider: str = "unknown",
        model_name: str = "unknown",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.worker_identity = worker_identity
        self.worker_pid = worker_pid
        self.plan_revision = plan_revision
        self.provider = provider
        self.model_name = model_name
        self.metadata = scrub_sensitive_dict(metadata or {})

        self._start_perf: Optional[float] = None
        self._end_perf: Optional[float] = None
        self._first_token_perf: Optional[float] = None

        self._tokens = TokenMetrics(provenance=MetricProvenance.UNKNOWN)
        self._cost = CostMetrics(provenance=MetricProvenance.UNKNOWN)
        self._tool_duration_ms: float = 0.0
        self._supervisor_overhead_ms: float = 0.0
        self._tool_calls_count: int = 0
        self._retries_count: int = 0
        self._stream_chunks_count: int = 0

    def start(self) -> TelemetryCollector:
        """Start the collector timer."""
        if self._start_perf is None:
            self._start_perf = time.perf_counter()
        return self

    def __enter__(self) -> TelemetryCollector:
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._end_perf is None:
            self._end_perf = time.perf_counter()

    def mark_first_token(self) -> None:
        """Record time-to-first-token (TTFT) upon arrival of initial chunk."""
        if self._first_token_perf is None:
            self._first_token_perf = time.perf_counter()

    def record_stream_chunk(self) -> None:
        """Record the arrival of a streaming token chunk."""
        self._stream_chunks_count += 1
        if self._first_token_perf is None:
            self.mark_first_token()

    def record_retry(self) -> None:
        """Increment retries counter."""
        self._retries_count += 1

    @contextlib.contextmanager
    def measure_tool(self):
        """Context manager to measure tool invocation duration and increment tool call count."""
        self._tool_calls_count += 1
        t0 = time.perf_counter()
        try:
            yield
        finally:
            t1 = time.perf_counter()
            self._tool_duration_ms += (t1 - t0) * 1000.0

    @contextlib.contextmanager
    def measure_supervisor(self):
        """Context manager to measure supervisor coordination and validation overhead."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            t1 = time.perf_counter()
            self._supervisor_overhead_ms += (t1 - t0) * 1000.0

    def add_tool_time(self, duration_ms: float) -> None:
        """Add externally measured tool duration in milliseconds."""
        self._tool_duration_ms += max(0.0, float(duration_ms))

    def add_supervisor_time(self, duration_ms: float) -> None:
        """Add externally measured supervisor overhead in milliseconds."""
        self._supervisor_overhead_ms += max(0.0, float(duration_ms))

    def set_tokens(
        self,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        cache_read_tokens: Optional[int] = None,
        cache_creation_tokens: Optional[int] = None,
        total_tokens: Optional[int] = None,
        provenance: str = MetricProvenance.MEASURED,
    ) -> None:
        """Record token accounting applying vendor-specific cache contracts."""
        self._tokens = normalize_provider_tokens(
            provider=self.provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            total_tokens=total_tokens,
            provenance=provenance,
        )

    def set_cost(
        self,
        total_cost_usd: Optional[float],
        input_cost_usd: Optional[float] = None,
        output_cost_usd: Optional[float] = None,
        cache_read_cost_usd: Optional[float] = None,
        cache_creation_cost_usd: Optional[float] = None,
        provenance: str = MetricProvenance.MEASURED,
        pricing_version: Optional[str] = None,
    ) -> None:
        """Record explicit cost accounting."""
        self._cost = CostMetrics(
            input_cost_usd=input_cost_usd,
            output_cost_usd=output_cost_usd,
            cache_read_cost_usd=cache_read_cost_usd,
            cache_creation_cost_usd=cache_creation_cost_usd,
            total_cost_usd=total_cost_usd,
            provenance=provenance,
            pricing_version=pricing_version,
        )

    def finalize(
        self,
        success: bool = True,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> CognitiveTelemetryRecord:
        """Finalize and construct an immutable CognitiveTelemetryRecord."""
        if self._start_perf is None:
            self._start_perf = time.perf_counter()
        if self._end_perf is None:
            self._end_perf = time.perf_counter()

        total_duration_ms = max(0.0, (self._end_perf - self._start_perf) * 1000.0)

        ttft_ms: Optional[float] = None
        if self._first_token_perf is not None:
            ttft_ms = max(0.0, (self._first_token_perf - self._start_perf) * 1000.0)

        # Generation duration is derived by subtraction from total duration
        overhead_sum = self._tool_duration_ms + self._supervisor_overhead_ms
        gen_duration_ms: Optional[float] = max(0.0, total_duration_ms - overhead_sum)

        latencies = LatencyMetrics(
            total_duration_ms=total_duration_ms,
            ttft_ms=ttft_ms,
            generation_duration_ms=gen_duration_ms,
            tool_duration_ms=self._tool_duration_ms,
            supervisor_overhead_ms=self._supervisor_overhead_ms,
            latency_provenance=MetricProvenance.MEASURED,
            generation_duration_provenance=MetricProvenance.ESTIMATED,
        )

        cost = self._cost
        if cost.provenance == MetricProvenance.UNKNOWN and self._tokens.provenance != MetricProvenance.UNKNOWN:
            cost = PricingRegistry.estimate_cost(self.model_name, self._tokens, provider=self.provider)

        merged_meta = dict(self.metadata)
        if metadata_update:
            merged_meta.update(scrub_sensitive_dict(metadata_update))

        return CognitiveTelemetryRecord(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_identity=self.worker_identity,
            worker_pid=self.worker_pid,
            plan_revision=self.plan_revision,
            provider=self.provider,
            model_name=self.model_name,
            tokens=self._tokens,
            latencies=latencies,
            cost=cost,
            tool_calls_count=self._tool_calls_count,
            retries_count=self._retries_count,
            stream_chunks_count=self._stream_chunks_count,
            success=success,
            error_category=error_category,
            error_message=error_message,
            metadata=merged_meta,
            created_at=_now(),
        )


@dataclass(frozen=True)
class TelemetryAggregate:
    """Immutable statistical aggregate summary of cognitive telemetry records.

    Differentiates known zero tokens/costs from unknown ones, and explicitly signals truncation.
    """
    total_matched_records: int = 0
    sample_size: int = 0
    is_truncated: bool = False
    truncation_note: Optional[str] = None

    total_input_tokens: Optional[int] = None
    total_output_tokens: Optional[int] = None
    total_cache_read_tokens: Optional[int] = None
    total_cache_creation_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    records_with_measured_tokens: int = 0
    records_with_estimated_tokens: int = 0
    records_with_unknown_tokens: int = 0
    tokens_by_provenance: Dict[str, int] = field(default_factory=lambda: {
        MetricProvenance.MEASURED: 0,
        MetricProvenance.ESTIMATED: 0,
        MetricProvenance.UNKNOWN: 0,
    })

    total_cost_usd: Optional[float] = None
    records_with_measured_cost: int = 0
    records_with_estimated_cost: int = 0
    records_with_unknown_cost: int = 0
    cost_by_provenance: Dict[str, float] = field(default_factory=lambda: {
        MetricProvenance.MEASURED: 0.0,
        MetricProvenance.ESTIMATED: 0.0,
        MetricProvenance.UNKNOWN: 0.0,
    })

    total_duration_ms: float = 0.0
    avg_duration_ms: float = 0.0
    p50_duration_ms: float = 0.0
    p90_duration_ms: float = 0.0
    p95_duration_ms: float = 0.0
    p99_duration_ms: float = 0.0

    avg_ttft_ms: Optional[float] = None
    avg_generation_duration_ms: Optional[float] = None
    avg_tool_duration_ms: Optional[float] = None
    avg_supervisor_overhead_ms: Optional[float] = None

    tool_calls_count: int = 0
    retries_count: int = 0
    stream_chunks_count: int = 0

    success_count: int = 0
    failure_count: int = 0
    success_rate: float = 0.0
    error_breakdown: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _percentile(sorted_vals: List[float], p: float) -> float:
    """Calculate the p-th percentile (0.0 to 1.0) of a sorted non-empty list of values."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = p * (len(sorted_vals) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    weight = rank - lower
    return round(sorted_vals[lower] * (1.0 - weight) + sorted_vals[upper] * weight, 3)


def compute_telemetry_aggregate(
    records: List[CognitiveTelemetryRecord],
    total_matched: Optional[int] = None,
    is_truncated: bool = False,
    truncation_note: Optional[str] = None,
) -> TelemetryAggregate:
    """Compute comprehensive statistical metrics over a set of telemetry records."""
    if not records:
        return TelemetryAggregate(
            total_matched_records=total_matched or 0,
            sample_size=0,
            is_truncated=is_truncated,
            truncation_note=truncation_note,
        )

    count = len(records)
    matched = total_matched if total_matched is not None else count

    in_tokens_sum = 0
    out_tokens_sum = 0
    cache_read_sum = 0
    cache_creation_sum = 0
    total_tokens_sum = 0

    has_any_known_tokens = False
    tokens_prov: Dict[str, int] = {
        MetricProvenance.MEASURED: 0,
        MetricProvenance.ESTIMATED: 0,
        MetricProvenance.UNKNOWN: 0,
    }
    rec_with_meas_tokens = 0
    rec_with_est_tokens = 0
    rec_with_unk_tokens = 0

    cost_sum = 0.0
    has_any_known_cost = False
    cost_prov: Dict[str, float] = {
        MetricProvenance.MEASURED: 0.0,
        MetricProvenance.ESTIMATED: 0.0,
        MetricProvenance.UNKNOWN: 0.0,
    }
    rec_with_meas_cost = 0
    rec_with_est_cost = 0
    rec_with_unk_cost = 0

    durations: List[float] = []
    ttfts: List[float] = []
    gen_durations: List[float] = []
    tool_durations: List[float] = []
    super_overheads: List[float] = []

    tool_calls = 0
    retries = 0
    stream_chunks = 0
    successes = 0
    failures = 0
    error_counts: Dict[str, int] = {}

    for r in records:
        # Token metrics
        if r.tokens.provenance == MetricProvenance.UNKNOWN or r.tokens.total_tokens is None:
            rec_with_unk_tokens += 1
            tokens_prov[MetricProvenance.UNKNOWN] += (r.tokens.total_tokens or 0)
        else:
            has_any_known_tokens = True
            tok_val = r.tokens.total_tokens or 0
            total_tokens_sum += tok_val
            in_tokens_sum += (r.tokens.input_tokens or 0)
            out_tokens_sum += (r.tokens.output_tokens or 0)
            cache_read_sum += (r.tokens.cache_read_tokens or 0)
            cache_creation_sum += (r.tokens.cache_creation_tokens or 0)

            if r.tokens.provenance == MetricProvenance.MEASURED:
                rec_with_meas_tokens += 1
                tokens_prov[MetricProvenance.MEASURED] += tok_val
            else:
                rec_with_est_tokens += 1
                tokens_prov[MetricProvenance.ESTIMATED] += tok_val

        # Cost metrics
        if r.cost.provenance == MetricProvenance.UNKNOWN or r.cost.total_cost_usd is None:
            rec_with_unk_cost += 1
        else:
            has_any_known_cost = True
            c_val = r.cost.total_cost_usd
            cost_sum += c_val
            if r.cost.provenance == MetricProvenance.MEASURED:
                rec_with_meas_cost += 1
                cost_prov[MetricProvenance.MEASURED] = round(cost_prov[MetricProvenance.MEASURED] + c_val, 8)
            else:
                rec_with_est_cost += 1
                cost_prov[MetricProvenance.ESTIMATED] = round(cost_prov[MetricProvenance.ESTIMATED] + c_val, 8)

        # Durations
        durations.append(r.latencies.total_duration_ms)
        if r.latencies.ttft_ms is not None:
            ttfts.append(r.latencies.ttft_ms)
        if r.latencies.generation_duration_ms is not None:
            gen_durations.append(r.latencies.generation_duration_ms)
        if r.latencies.tool_duration_ms is not None:
            tool_durations.append(r.latencies.tool_duration_ms)
        if r.latencies.supervisor_overhead_ms is not None:
            super_overheads.append(r.latencies.supervisor_overhead_ms)

        # Counters
        tool_calls += r.tool_calls_count
        retries += r.retries_count
        stream_chunks += r.stream_chunks_count

        if r.success:
            successes += 1
        else:
            failures += 1
            cat = r.error_category or "unknown"
            error_counts[cat] = error_counts.get(cat, 0) + 1

    durations.sort()
    total_dur = sum(durations)
    avg_dur = round(total_dur / count, 3)

    return TelemetryAggregate(
        total_matched_records=matched,
        sample_size=count,
        is_truncated=is_truncated,
        truncation_note=truncation_note,
        total_input_tokens=in_tokens_sum if has_any_known_tokens else None,
        total_output_tokens=out_tokens_sum if has_any_known_tokens else None,
        total_cache_read_tokens=cache_read_sum if has_any_known_tokens else None,
        total_cache_creation_tokens=cache_creation_sum if has_any_known_tokens else None,
        total_tokens=total_tokens_sum if has_any_known_tokens else None,
        records_with_measured_tokens=rec_with_meas_tokens,
        records_with_estimated_tokens=rec_with_est_tokens,
        records_with_unknown_tokens=rec_with_unk_tokens,
        tokens_by_provenance=tokens_prov,
        total_cost_usd=round(cost_sum, 8) if has_any_known_cost else None,
        records_with_measured_cost=rec_with_meas_cost,
        records_with_estimated_cost=rec_with_est_cost,
        records_with_unknown_cost=rec_with_unk_cost,
        cost_by_provenance=cost_prov,
        total_duration_ms=round(total_dur, 3),
        avg_duration_ms=avg_dur,
        p50_duration_ms=_percentile(durations, 0.50),
        p90_duration_ms=_percentile(durations, 0.90),
        p95_duration_ms=_percentile(durations, 0.95),
        p99_duration_ms=_percentile(durations, 0.99),
        avg_ttft_ms=round(sum(ttfts) / len(ttfts), 3) if ttfts else None,
        avg_generation_duration_ms=round(sum(gen_durations) / len(gen_durations), 3) if gen_durations else None,
        avg_tool_duration_ms=round(sum(tool_durations) / len(tool_durations), 3) if tool_durations else None,
        avg_supervisor_overhead_ms=round(sum(super_overheads) / len(super_overheads), 3) if super_overheads else None,
        tool_calls_count=tool_calls,
        retries_count=retries,
        stream_chunks_count=stream_chunks,
        success_count=successes,
        failure_count=failures,
        success_rate=round(successes / count, 4) if count > 0 else 0.0,
        error_breakdown=error_counts,
    )


def safe_record_telemetry(store: Any, record: Union[CognitiveTelemetryRecord, Dict[str, Any]]) -> Optional[CognitiveTelemetryRecord]:
    """Fault-tolerant, non-interfering recorder for cognitive telemetry."""
    try:
        if hasattr(store, "record_cognitive_telemetry"):
            return store.record_cognitive_telemetry(record)
        logger.warning("Store não possui método record_cognitive_telemetry; ignorando registro de telemetria")
        return None
    except Exception as exc:
        logger.warning(f"Falha não-bloqueante ao registrar telemetria cognitiva: {exc}", exc_info=True)
        return None
