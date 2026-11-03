"""Cognitive Telemetry Module for tracking model tokens, latency, cost, and tool usage.

Provides observational, passive instrumentation for AI Coordinator missions and workers,
with strict data provenance differentiation (measured, estimated, unknown) and invariant
non-interference guarantees.
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


# Sensitive credential patterns for scrubbing
_SENSITIVE_PATTERNS = [
    re.compile(r"(bearer\s+)[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE),
    re.compile(r"(password|secret|token|api[_-]?key|credential)\s*[:=]\s*['\"]?[^'\"\s]+['\"]?", re.IGNORECASE),
    re.compile(r"(sk-[A-Za-z0-9_\-]{20,})", re.IGNORECASE),
    re.compile(r"(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,})", re.IGNORECASE),
]


def scrub_sensitive_text(text: Optional[str]) -> Optional[str]:
    """Scrub sensitive credentials, passwords, and tokens from telemetry text."""
    if not text:
        return text
    scrubbed = str(text)
    for pattern in _SENSITIVE_PATTERNS:
        scrubbed = pattern.sub(r"\1[REDACTED_SECRET]", scrubbed)
    return scrubbed


def scrub_sensitive_dict(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Recursively scrub sensitive keys and token values from telemetry metadata."""
    if not data:
        return {}
    scrubbed: Dict[str, Any] = {}
    for k, v in data.items():
        k_str = str(k)
        if any(term in k_str.lower() for term in ("secret", "token", "password", "key", "auth", "credential")):
            scrubbed[k_str] = "[REDACTED_SECRET]"
        elif isinstance(v, str):
            scrubbed[k_str] = scrub_sensitive_text(v)
        elif isinstance(v, dict):
            scrubbed[k_str] = scrub_sensitive_dict(v)
        elif isinstance(v, list):
            scrubbed[k_str] = [
                scrub_sensitive_dict(item) if isinstance(item, dict)
                else (scrub_sensitive_text(item) if isinstance(item, str) else item)
                for item in v
            ]
        else:
            scrubbed[k_str] = v
    return scrubbed


@dataclass
class TokenMetrics:
    """Token accounting metrics with explicit data provenance."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    total_tokens: int = 0
    provenance: str = MetricProvenance.UNKNOWN

    def __post_init__(self) -> None:
        MetricProvenance.validate(self.provenance)
        self.input_tokens = max(0, int(self.input_tokens))
        self.output_tokens = max(0, int(self.output_tokens))
        self.cache_read_tokens = max(0, int(self.cache_read_tokens))
        self.cache_creation_tokens = max(0, int(self.cache_creation_tokens))
        if self.total_tokens == 0 and (self.input_tokens > 0 or self.output_tokens > 0):
            self.total_tokens = self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens
        else:
            self.total_tokens = max(0, int(self.total_tokens))

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TokenMetrics:
        if not data:
            return cls()
        return cls(
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            cache_read_tokens=data.get("cache_read_tokens", 0),
            cache_creation_tokens=data.get("cache_creation_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            provenance=data.get("provenance", MetricProvenance.UNKNOWN),
        )


@dataclass
class LatencyMetrics:
    """Detailed latency and execution duration metrics in milliseconds."""
    ttft_ms: Optional[float] = None
    generation_duration_ms: Optional[float] = None
    tool_duration_ms: Optional[float] = None
    supervisor_overhead_ms: Optional[float] = None
    total_duration_ms: float = 0.0
    provenance: str = MetricProvenance.UNKNOWN

    def __post_init__(self) -> None:
        MetricProvenance.validate(self.provenance)
        if self.ttft_ms is not None:
            self.ttft_ms = max(0.0, float(self.ttft_ms))
        if self.generation_duration_ms is not None:
            self.generation_duration_ms = max(0.0, float(self.generation_duration_ms))
        if self.tool_duration_ms is not None:
            self.tool_duration_ms = max(0.0, float(self.tool_duration_ms))
        if self.supervisor_overhead_ms is not None:
            self.supervisor_overhead_ms = max(0.0, float(self.supervisor_overhead_ms))
        self.total_duration_ms = max(0.0, float(self.total_duration_ms))

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LatencyMetrics:
        if not data:
            return cls()
        return cls(
            ttft_ms=data.get("ttft_ms"),
            generation_duration_ms=data.get("generation_duration_ms"),
            tool_duration_ms=data.get("tool_duration_ms"),
            supervisor_overhead_ms=data.get("supervisor_overhead_ms"),
            total_duration_ms=data.get("total_duration_ms", 0.0),
            provenance=data.get("provenance", MetricProvenance.UNKNOWN),
        )


@dataclass
class CostMetrics:
    """Monetary cost accounting metrics in USD."""
    input_cost_usd: Optional[float] = None
    output_cost_usd: Optional[float] = None
    cache_read_cost_usd: Optional[float] = None
    cache_creation_cost_usd: Optional[float] = None
    total_cost_usd: Optional[float] = None
    provenance: str = MetricProvenance.UNKNOWN

    def __post_init__(self) -> None:
        MetricProvenance.validate(self.provenance)
        if self.input_cost_usd is not None:
            self.input_cost_usd = max(0.0, float(self.input_cost_usd))
        if self.output_cost_usd is not None:
            self.output_cost_usd = max(0.0, float(self.output_cost_usd))
        if self.cache_read_cost_usd is not None:
            self.cache_read_cost_usd = max(0.0, float(self.cache_read_cost_usd))
        if self.cache_creation_cost_usd is not None:
            self.cache_creation_cost_usd = max(0.0, float(self.cache_creation_cost_usd))

        if self.total_cost_usd is not None:
            self.total_cost_usd = max(0.0, float(self.total_cost_usd))
        else:
            components = [
                c for c in (self.input_cost_usd, self.output_cost_usd, self.cache_read_cost_usd, self.cache_creation_cost_usd)
                if c is not None
            ]
            if components:
                self.total_cost_usd = sum(components)

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
        )


@dataclass
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
        self.tool_calls_count = max(0, int(self.tool_calls_count))
        self.retries_count = max(0, int(self.retries_count))
        self.stream_chunks_count = max(0, int(self.stream_chunks_count))
        self.provider = str(self.provider or "unknown").strip().lower()
        self.model_name = str(self.model_name or "unknown").strip()
        if self.error_message:
            self.error_message = scrub_sensitive_text(self.error_message)
        if self.metadata:
            self.metadata = scrub_sensitive_dict(self.metadata)

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
            input_tokens=r.get("input_tokens") or 0,
            output_tokens=r.get("output_tokens") or 0,
            cache_read_tokens=r.get("cache_read_tokens") or 0,
            cache_creation_tokens=r.get("cache_creation_tokens") or 0,
            total_tokens=r.get("total_tokens") or 0,
            provenance=r.get("tokens_provenance") or MetricProvenance.UNKNOWN,
        )
        latencies = LatencyMetrics(
            ttft_ms=r.get("ttft_ms"),
            generation_duration_ms=r.get("generation_duration_ms"),
            tool_duration_ms=r.get("tool_duration_ms"),
            supervisor_overhead_ms=r.get("supervisor_overhead_ms"),
            total_duration_ms=r.get("total_duration_ms") or 0.0,
            provenance=r.get("latency_provenance") or MetricProvenance.UNKNOWN,
        )
        cost = CostMetrics(
            total_cost_usd=r.get("total_cost_usd"),
            provenance=r.get("cost_provenance") or MetricProvenance.UNKNOWN,
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

    Estimations are strictly marked as MetricProvenance.ESTIMATED. Unrecognized models
    are returned as MetricProvenance.UNKNOWN with total_cost_usd=None to avoid fabricated numbers.
    """
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
        # Match without date suffixes or common prefixes
        for registered_key, rates in cls._REGISTRY.items():
            if key.startswith(registered_key) or registered_key.startswith(key):
                return rates
        return None

    @classmethod
    def estimate_cost(cls, model_name: str, tokens: TokenMetrics) -> CostMetrics:
        """Calculate estimated cost for token metrics based on reference pricing.

        Returns CostMetrics with provenance=ESTIMATED if the model is registered,
        or provenance=UNKNOWN if the model is unrecognized.
        """
        rates = cls.get_pricing(model_name)
        if not rates:
            return CostMetrics(provenance=MetricProvenance.UNKNOWN)

        in_cost = (tokens.input_tokens / 1_000_000.0) * rates["input_per_m"]
        out_cost = (tokens.output_tokens / 1_000_000.0) * rates["output_per_m"]
        read_cost = (tokens.cache_read_tokens / 1_000_000.0) * rates.get("cache_read_per_m", 0.0)
        creat_cost = (tokens.cache_creation_tokens / 1_000_000.0) * rates.get("cache_creation_per_m", 0.0)
        total = in_cost + out_cost + read_cost + creat_cost

        return CostMetrics(
            input_cost_usd=round(in_cost, 8),
            output_cost_usd=round(out_cost, 8),
            cache_read_cost_usd=round(read_cost, 8),
            cache_creation_cost_usd=round(creat_cost, 8),
            total_cost_usd=round(total, 8),
            provenance=MetricProvenance.ESTIMATED,
        )

    @staticmethod
    def _normalize_name(model_name: str) -> str:
        name = str(model_name or "").strip().lower()
        # Strip provider prefixes like "openai/", "anthropic/", "google/"
        if "/" in name:
            name = name.split("/")[-1]
        return name


class TelemetryCollector:
    """Context manager and high-precision timer for collecting cognitive metrics.

    Ensures fine-grained decomposition of latency (TTFT, generation, tool, supervisor overhead)
    and strictly validates token provenance.
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

        # High-resolution wall-clock timer
        self._start_perf: Optional[float] = None
        self._end_perf: Optional[float] = None
        self._first_token_perf: Optional[float] = None

        # Metrics accumulators
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
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        total_tokens: Optional[int] = None,
        provenance: str = MetricProvenance.MEASURED,
    ) -> None:
        """Record token accounting with explicit provenance classification."""
        self._tokens = TokenMetrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            total_tokens=total_tokens or (input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens),
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
    ) -> None:
        """Record explicit cost accounting."""
        self._cost = CostMetrics(
            input_cost_usd=input_cost_usd,
            output_cost_usd=output_cost_usd,
            cache_read_cost_usd=cache_read_cost_usd,
            cache_creation_cost_usd=cache_creation_cost_usd,
            total_cost_usd=total_cost_usd,
            provenance=provenance,
        )

    def finalize(
        self,
        success: bool = True,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
        metadata_update: Optional[Dict[str, Any]] = None,
    ) -> CognitiveTelemetryRecord:
        """Finalize and construct an immutable CognitiveTelemetryRecord.

        Computes latency decomposition and derives cost if not explicitly recorded.
        """
        if self._start_perf is None:
            self._start_perf = time.perf_counter()
        if self._end_perf is None:
            self._end_perf = time.perf_counter()

        total_duration_ms = max(0.0, (self._end_perf - self._start_perf) * 1000.0)

        # TTFT calculation
        ttft_ms: Optional[float] = None
        if self._first_token_perf is not None:
            ttft_ms = max(0.0, (self._first_token_perf - self._start_perf) * 1000.0)

        # Generation duration = Total - (Tool + Supervisor)
        # Bounded to non-negative
        overhead_sum = self._tool_duration_ms + self._supervisor_overhead_ms
        gen_duration_ms: Optional[float] = max(0.0, total_duration_ms - overhead_sum)

        latencies = LatencyMetrics(
            ttft_ms=ttft_ms,
            generation_duration_ms=gen_duration_ms,
            tool_duration_ms=self._tool_duration_ms,
            supervisor_overhead_ms=self._supervisor_overhead_ms,
            total_duration_ms=total_duration_ms,
            provenance=MetricProvenance.MEASURED,
        )

        # Cost: if not explicitly recorded with MEASURED or ESTIMATED, attempt reference pricing
        cost = self._cost
        if cost.provenance == MetricProvenance.UNKNOWN and (self._tokens.input_tokens > 0 or self._tokens.output_tokens > 0):
            cost = PricingRegistry.estimate_cost(self.model_name, self._tokens)

        # Merge metadata
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


@dataclass
class TelemetryAggregate:
    """Statistical aggregate summary of cognitive telemetry records across runs or missions."""
    record_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_tokens: int = 0
    tokens_by_provenance: Dict[str, int] = field(default_factory=lambda: {
        MetricProvenance.MEASURED: 0,
        MetricProvenance.ESTIMATED: 0,
        MetricProvenance.UNKNOWN: 0,
    })
    total_cost_usd: float = 0.0
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


def compute_telemetry_aggregate(records: List[CognitiveTelemetryRecord]) -> TelemetryAggregate:
    """Compute comprehensive statistical metrics over a set of telemetry records."""
    if not records:
        return TelemetryAggregate()

    count = len(records)
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_tok = 0
    tokens_prov: Dict[str, int] = {
        MetricProvenance.MEASURED: 0,
        MetricProvenance.ESTIMATED: 0,
        MetricProvenance.UNKNOWN: 0,
    }

    total_cost = 0.0
    cost_prov: Dict[str, float] = {
        MetricProvenance.MEASURED: 0.0,
        MetricProvenance.ESTIMATED: 0.0,
        MetricProvenance.UNKNOWN: 0.0,
    }

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
        # Tokens
        total_in += r.tokens.input_tokens
        total_out += r.tokens.output_tokens
        total_cache_read += r.tokens.cache_read_tokens
        total_cache_creation += r.tokens.cache_creation_tokens
        total_tok += r.tokens.total_tokens

        tok_prov = r.tokens.provenance
        if tok_prov in tokens_prov:
            tokens_prov[tok_prov] += r.tokens.total_tokens
        else:
            tokens_prov[MetricProvenance.UNKNOWN] += r.tokens.total_tokens

        # Cost
        if r.cost.total_cost_usd is not None:
            c_val = r.cost.total_cost_usd
            total_cost += c_val
            c_prov = r.cost.provenance
            if c_prov in cost_prov:
                cost_prov[c_prov] = round(cost_prov[c_prov] + c_val, 8)
            else:
                cost_prov[MetricProvenance.UNKNOWN] = round(cost_prov[MetricProvenance.UNKNOWN] + c_val, 8)

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
        record_count=count,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_cache_read_tokens=total_cache_read,
        total_cache_creation_tokens=total_cache_creation,
        total_tokens=total_tok,
        tokens_by_provenance=tokens_prov,
        total_cost_usd=round(total_cost, 8),
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
    """Fault-tolerant, non-interfering recorder for cognitive telemetry.

    Guarantees that telemetry logging failures will NEVER crash supervisor runs or alter
    operational workflows.
    """
    try:
        if hasattr(store, "record_cognitive_telemetry"):
            return store.record_cognitive_telemetry(record)
        logger.warning("Store não possui método record_cognitive_telemetry; ignorando registro de telemetria")
        return None
    except Exception as exc:
        logger.warning(f"Falha não-bloqueante ao registrar telemetria cognitiva: {exc}", exc_info=True)
        return None
