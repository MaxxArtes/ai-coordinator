"""Benchmark orchestrator and evaluation runner for OpenRouter free models (Phase 4C.1).

Coordinates:
1. Dynamic model verification via FreeCostGuard with fresh catalog enforcement.
2. Resilience enforcement: RateLimiter (every attempt consumes quota), CircuitBreaker, and RetryPolicy.
3. Modality adapter execution (Text, Embedding, Rerank, Decisions, Speech).
4. AvailabilityLedger event auditing and persistent snapshotting.
5. Strict classification of metric provenance (MEASURED, ESTIMATED, SYNTHETIC, UNKNOWN).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .adapters import (
    AdapterResponse,
    BaseModelAdapter,
    ConfidentialDataDetectedError,
    DecisionModelAdapter,
    EmbeddingModelAdapter,
    IncompatibleModalityError,
    MetricProvenance,
    RerankModelAdapter,
    SpeechModelAdapter,
    TextModelAdapter,
)
from .availability import AuditPersistenceError, AvailabilityEventType, AvailabilityLedger
from .cost_guard import CostGuardError, FreeCostGuard, UnauthorizedModelError
from .registry import FreeModelRegistry, ModelCatalogEntry, ModelModality, StaleCatalogError
from .resilience import (
    CircuitBreaker,
    CircuitOpenError,
    QuotaPersistenceError,
    RateLimitExceededError,
    RetryPolicy,
    SlidingWindowRateLimiter,
)
from .transport import IncompatibleModalityEndpointError, OpenRouterHttpTransport

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ModelBenchmarkRunResult:
    """Execution result of a single model benchmark evaluation."""
    run_id: str
    model_id: str
    modality: ModelModality
    success: bool
    latency_ms: float
    tokens: Dict[str, int]
    token_provenance: MetricProvenance
    latency_provenance: MetricProvenance
    cost_usd: float
    status_code: int
    error_message: Optional[str]
    timestamp: str
    metadata: Dict[str, Any]

    @property
    def reported_tokens(self) -> Dict[str, int]:
        """Backward-compatible alias for tokens."""
        return self.tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model_id": self.model_id,
            "modality": self.modality.value,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 4),
            "tokens": self.tokens,
            "reported_tokens": self.tokens,
            "token_provenance": self.token_provenance.value,
            "latency_provenance": self.latency_provenance.value,
            "cost_usd": round(self.cost_usd, 6),
            "status_code": self.status_code,
            "error_message": self.error_message,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


class ModelBenchmarkRunner:
    """Orchestrates resilient benchmarks of OpenRouter free models with strict zero-cost enforcement."""

    def __init__(
        self,
        registry: FreeModelRegistry,
        cost_guard: Optional[FreeCostGuard] = None,
        ledger: Optional[AvailabilityLedger] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        retry_policy: Optional[RetryPolicy] = None,
        transport: Optional[OpenRouterHttpTransport] = None,
        store: Optional[Any] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.registry = registry
        self.cost_guard = cost_guard or FreeCostGuard(registry)
        self.ledger = ledger or AvailabilityLedger(store)
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter(max_per_minute=20, max_per_day=50)
        self.circuit_breaker = circuit_breaker or CircuitBreaker(failure_threshold=3, recovery_timeout_sec=30.0)
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        self.transport = transport or OpenRouterHttpTransport(
            cost_guard=self.cost_guard,
            rate_limiter=self.rate_limiter,
            ledger=self.ledger,
            live_network_enabled=False,
        )
        if self.transport.cost_guard is None:
            self.transport.cost_guard = self.cost_guard
        if self.transport.rate_limiter is None:
            self.transport.rate_limiter = self.rate_limiter
        if self.transport.ledger is None:
            self.transport.ledger = self.ledger
        self.store = store
        self.sleeper = sleeper or time.sleep

        # Initialize modality adapters with bound CostGuard and transport
        self.adapters: Dict[ModelModality, BaseModelAdapter] = {
            ModelModality.TEXT: TextModelAdapter(self.cost_guard, transport=self.transport),
            ModelModality.EMBEDDING: EmbeddingModelAdapter(self.cost_guard, transport=self.transport),
            ModelModality.RERANK: RerankModelAdapter(self.cost_guard, transport=self.transport),
            ModelModality.DECISIONS: DecisionModelAdapter(self.cost_guard, transport=self.transport),
            ModelModality.SPEECH: SpeechModelAdapter(self.cost_guard, transport=self.transport),
            ModelModality.SPEECH_TRANSCRIPTION: SpeechModelAdapter(self.cost_guard, transport=self.transport),
            ModelModality.SPEECH_GENERATION: SpeechModelAdapter(self.cost_guard, transport=self.transport),
        }

    def register_custom_adapter(self, modality: ModelModality, adapter: BaseModelAdapter) -> None:
        """Register a custom or mocked adapter for a given modality."""
        self.adapters[modality] = adapter

    def execute_task(
        self,
        model_id: str,
        modality: ModelModality,
        inputs: Dict[str, Any],
        run_id_prefix: str = "openrouter_bench",
    ) -> ModelBenchmarkRunResult:
        """Execute a benchmark evaluation on a free model with full resilience gates."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        run_id = f"{run_id_prefix}_{int(time.time() * 1000)}"

        # Gate 1: Strict Zero-Cost Authorization with Freshness Check (Fail-Closed)
        try:
            entry = self.cost_guard.authorize_model_execution(model_id, verify_freshness=True)
        except StaleCatalogError as sce:
            try:
                self.ledger.record_event(
                    model_id=model_id,
                    event_type=AvailabilityEventType.PRICE_CHANGED_BLOCKED,
                    details={"error": str(sce), "run_id": run_id, "reason": "stale_catalog"},
                )
            except AuditPersistenceError:
                pass
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                token_provenance=MetricProvenance.UNKNOWN,
                latency_provenance=MetricProvenance.UNKNOWN,
                cost_usd=0.0,
                status_code=403,
                error_message=f"[STALE_CATALOG_BLOCKED] {sce}",
                timestamp=now_iso,
                metadata={"blocked_by": "StaleCatalogCheck"},
            )
        except CostGuardError as cge:
            try:
                self.ledger.record_event(
                    model_id=model_id,
                    event_type=AvailabilityEventType.PRICE_CHANGED_BLOCKED,
                    details={"error": str(cge), "run_id": run_id},
                )
            except AuditPersistenceError:
                pass
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                token_provenance=MetricProvenance.UNKNOWN,
                latency_provenance=MetricProvenance.UNKNOWN,
                cost_usd=0.0,
                status_code=403,
                error_message=f"[COST_GUARD_BLOCKED] {cge}",
                timestamp=now_iso,
                metadata={"blocked_by": "FreeCostGuard"},
            )

        # Gate 2: Circuit Breaker Inspection
        if not self.circuit_breaker.allow_request():
            try:
                self.ledger.record_event(
                    model_id=model_id,
                    event_type=AvailabilityEventType.CIRCUIT_BREAKER_TRIPPED,
                    details={"run_id": run_id, "state": self.circuit_breaker.state.value},
                )
            except AuditPersistenceError:
                pass
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                token_provenance=MetricProvenance.UNKNOWN,
                latency_provenance=MetricProvenance.UNKNOWN,
                cost_usd=0.0,
                status_code=503,
                error_message="[CIRCUIT_BREAKER_OPEN] Endpoint temporariamente bloqueado por excesso de falhas consecutivas.",
                timestamp=now_iso,
                metadata={"state": self.circuit_breaker.state.value},
            )

        adapter = self.adapters.get(modality)
        if not adapter:
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                token_provenance=MetricProvenance.UNKNOWN,
                latency_provenance=MetricProvenance.UNKNOWN,
                cost_usd=0.0,
                status_code=400,
                error_message=f"Nenhum adaptador configurado para a modalidade '{modality.value}'.",
                timestamp=now_iso,
                metadata={},
            )

        # Gate 3 & 4: Execution with Retry Policy where EVERY ATTEMPT consumes quota atomically
        attempt = 1
        last_response: Optional[AdapterResponse] = None

        while attempt <= self.retry_policy.max_attempts:
            # Single Quota Reservation Point:
            # When live network is enabled on transport and rate_limiter is configured on it,
            # quota is acquired atomically inside transport.dispatch() at Gate 7 to avoid double counting.
            # In offline or mock mode, runner reserves quota directly here.
            quota_handled_by_transport = (
                self.transport is not None
                and self.transport.live_network_enabled
                and self.transport.rate_limiter is not None
            )

            if not quota_handled_by_transport:
                try:
                    granted = self.rate_limiter.acquire()
                except QuotaPersistenceError as qpe:
                    return ModelBenchmarkRunResult(
                        run_id=run_id,
                        model_id=model_id,
                        modality=modality,
                        success=False,
                        latency_ms=0.0,
                        tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        token_provenance=MetricProvenance.UNKNOWN,
                        latency_provenance=MetricProvenance.UNKNOWN,
                        cost_usd=0.0,
                        status_code=500,
                        error_message=f"[QUOTA_PERSISTENCE_FAILED] {qpe}",
                        timestamp=now_iso,
                        metadata={"attempt": attempt},
                    )

                if not granted:
                    wait_sec = self.rate_limiter.time_until_available()
                    try:
                        self.ledger.record_event(
                            model_id=model_id,
                            event_type=AvailabilityEventType.RATE_LIMITED_429,
                            details={"run_id": run_id, "attempt": attempt, "wait_seconds": wait_sec},
                        )
                    except AuditPersistenceError:
                        pass
                    return ModelBenchmarkRunResult(
                        run_id=run_id,
                        model_id=model_id,
                        modality=modality,
                        success=False,
                        latency_ms=0.0,
                        tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        token_provenance=MetricProvenance.UNKNOWN,
                        latency_provenance=MetricProvenance.UNKNOWN,
                        cost_usd=0.0,
                        status_code=429,
                        error_message=f"[RATE_LIMIT_EXCEEDED] Limite de requisições atingido na tentativa {attempt}. Aguarde {wait_sec:.1f}s.",
                        timestamp=now_iso,
                        metadata={"time_until_available_sec": wait_sec, "attempt": attempt},
                    )

            try:
                resp = adapter.execute(model_id, inputs)
                last_response = resp

                if resp.retry_after_sec is not None and resp.retry_after_sec > 0:
                    self.rate_limiter.record_retry_after(resp.retry_after_sec)

                if resp.success:
                    self.circuit_breaker.record_success()
                    self.ledger.record_event(
                        model_id=model_id,
                        event_type=AvailabilityEventType.BENCHMARK_EXECUTED,
                        details={"run_id": run_id, "latency_ms": resp.latency_ms, "tokens": resp.tokens},
                    )
                    break
                else:
                    if self.retry_policy.is_retryable_status(resp.status_code) and attempt < self.retry_policy.max_attempts:
                        delay = self.retry_policy.calculate_delay(attempt, retry_after_sec=resp.retry_after_sec)
                        self.sleeper(delay)
                        attempt += 1
                        continue

                    self.circuit_breaker.record_failure()
                    break

            except (IncompatibleModalityError, IncompatibleModalityEndpointError, ConfidentialDataDetectedError, CostGuardError) as security_err:
                self.circuit_breaker.record_failure()
                return ModelBenchmarkRunResult(
                    run_id=run_id,
                    model_id=model_id,
                    modality=modality,
                    success=False,
                    latency_ms=0.0,
                    tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    token_provenance=MetricProvenance.UNKNOWN,
                    latency_provenance=MetricProvenance.UNKNOWN,
                    cost_usd=0.0,
                    status_code=400 if isinstance(security_err, (IncompatibleModalityError, IncompatibleModalityEndpointError)) else 403,
                    error_message=f"[{security_err.__class__.__name__}] {security_err}",
                    timestamp=now_iso,
                    metadata={"attempt": attempt, "fatal": True},
                )
            except AuditPersistenceError as ape:
                # Critical audit failure
                self.circuit_breaker.record_failure()
                return ModelBenchmarkRunResult(
                    run_id=run_id,
                    model_id=model_id,
                    modality=modality,
                    success=False,
                    latency_ms=0.0,
                    tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    token_provenance=MetricProvenance.UNKNOWN,
                    latency_provenance=MetricProvenance.UNKNOWN,
                    cost_usd=0.0,
                    status_code=500,
                    error_message=f"[AUDIT_PERSISTENCE_FAILED] {ape}",
                    timestamp=now_iso,
                    metadata={"attempt": attempt, "fatal": True},
                )
            except Exception as exc:
                self.circuit_breaker.record_failure()
                if attempt < self.retry_policy.max_attempts:
                    delay = self.retry_policy.calculate_delay(attempt)
                    self.sleeper(delay)
                    attempt += 1
                    continue

                return ModelBenchmarkRunResult(
                    run_id=run_id,
                    model_id=model_id,
                    modality=modality,
                    success=False,
                    latency_ms=0.0,
                    tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    token_provenance=MetricProvenance.UNKNOWN,
                    latency_provenance=MetricProvenance.UNKNOWN,
                    cost_usd=0.0,
                    status_code=500,
                    error_message=f"Exceção inesperada na execução do adaptador: {exc}",
                    timestamp=now_iso,
                    metadata={"attempt": attempt},
                )

        if not last_response:
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                token_provenance=MetricProvenance.UNKNOWN,
                latency_provenance=MetricProvenance.UNKNOWN,
                cost_usd=0.0,
                status_code=500,
                error_message="Nenhuma resposta obtida após tentativas.",
                timestamp=now_iso,
                metadata={"total_attempts": attempt},
            )

        latency_prov = MetricProvenance.MEASURED if last_response.metadata.get("live_transport") else MetricProvenance.SYNTHETIC

        return ModelBenchmarkRunResult(
            run_id=run_id,
            model_id=model_id,
            modality=modality,
            success=last_response.success,
            latency_ms=last_response.latency_ms,
            tokens=last_response.tokens,
            token_provenance=last_response.token_provenance,
            latency_provenance=latency_prov,
            cost_usd=last_response.cost_usd,
            status_code=last_response.status_code,
            error_message=last_response.error_message,
            timestamp=now_iso,
            metadata={**last_response.metadata, "attempts": attempt},
        )
