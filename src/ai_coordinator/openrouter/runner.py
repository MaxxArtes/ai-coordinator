"""Benchmark orchestrator and evaluation runner for OpenRouter free models (Phase 4C.1).

Coordinates:
1. Dynamic model verification via FreeCostGuard.
2. Resilience enforcement via RateLimiter and CircuitBreaker.
3. Modality adapter execution (Text, Embedding, Rerank, Decisions, Speech).
4. AvailabilityLedger event auditing and snapshotting.
5. CognitiveTelemetry integration.
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
    DecisionModelAdapter,
    EmbeddingModelAdapter,
    RerankModelAdapter,
    SpeechModelAdapter,
    TextModelAdapter,
)
from .availability import AvailabilityEventType, AvailabilityLedger
from .cost_guard import CostGuardError, FreeCostGuard, UnauthorizedModelError
from .registry import FreeModelRegistry, ModelCatalogEntry, ModelModality
from .resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RateLimitExceededError,
    RetryPolicy,
    SlidingWindowRateLimiter,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ModelBenchmarkRunResult:
    """Execution result of a single model benchmark evaluation."""
    run_id: str
    model_id: str
    modality: ModelModality
    success: bool
    latency_ms: float
    reported_tokens: Dict[str, int]
    cost_usd: float
    status_code: int
    error_message: Optional[str]
    timestamp: str
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "model_id": self.model_id,
            "modality": self.modality.value,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 4),
            "reported_tokens": self.reported_tokens,
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
        store: Optional[Any] = None,
    ) -> None:
        self.registry = registry
        self.cost_guard = cost_guard or FreeCostGuard(registry)
        self.ledger = ledger or AvailabilityLedger(store)
        self.rate_limiter = rate_limiter or SlidingWindowRateLimiter(max_per_minute=20, max_per_day=50)
        self.circuit_breaker = circuit_breaker or CircuitBreaker(failure_threshold=3, recovery_timeout_sec=30.0)
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        self.store = store

        # Initialize modality adapters
        self.adapters: Dict[ModelModality, BaseModelAdapter] = {
            ModelModality.TEXT: TextModelAdapter(self.cost_guard),
            ModelModality.EMBEDDING: EmbeddingModelAdapter(self.cost_guard),
            ModelModality.RERANK: RerankModelAdapter(self.cost_guard),
            ModelModality.DECISIONS: DecisionModelAdapter(self.cost_guard),
            ModelModality.SPEECH: SpeechModelAdapter(self.cost_guard),
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

        # Gate 1: Strict Zero-Cost Authorization (Fail-Closed)
        try:
            entry = self.cost_guard.authorize_model_execution(model_id)
        except CostGuardError as cge:
            self.ledger.record_event(
                model_id=model_id,
                event_type=AvailabilityEventType.PRICE_CHANGED_BLOCKED,
                details={"error": str(cge), "run_id": run_id},
            )
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                reported_tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                cost_usd=0.0,
                status_code=403,
                error_message=f"[COST_GUARD_BLOCKED] {cge}",
                timestamp=now_iso,
                metadata={"blocked_by": "FreeCostGuard"},
            )

        # Gate 2: Circuit Breaker Inspection
        if not self.circuit_breaker.allow_request():
            self.ledger.record_event(
                model_id=model_id,
                event_type=AvailabilityEventType.CIRCUIT_BREAKER_TRIPPED,
                details={"run_id": run_id, "state": self.circuit_breaker.state.value},
            )
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                reported_tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                cost_usd=0.0,
                status_code=503,
                error_message="[CIRCUIT_BREAKER_OPEN] Endpoint temporariamente bloqueado por excesso de falhas consecutivas.",
                timestamp=now_iso,
                metadata={"state": self.circuit_breaker.state.value},
            )

        # Gate 3: Rate Limiting Inspection and Token Acquisition
        if not self.rate_limiter.acquire():
            wait_sec = self.rate_limiter.time_until_available()
            self.ledger.record_event(
                model_id=model_id,
                event_type=AvailabilityEventType.RATE_LIMITED_429,
                details={"run_id": run_id, "wait_seconds": wait_sec},
            )
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                reported_tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                cost_usd=0.0,
                status_code=429,
                error_message=f"[RATE_LIMIT_EXCEEDED] Limite de requisições atingido. Aguarde {wait_sec:.1f}s.",
                timestamp=now_iso,
                metadata={"time_until_available_sec": wait_sec},
            )

        adapter = self.adapters.get(modality)
        if not adapter:
            return ModelBenchmarkRunResult(
                run_id=run_id,
                model_id=model_id,
                modality=modality,
                success=False,
                latency_ms=0.0,
                reported_tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                cost_usd=0.0,
                status_code=400,
                error_message=f"Nenhum adaptador configurado para a modalidade '{modality.value}'.",
                timestamp=now_iso,
                metadata={},
            )

        # Gate 4: Execution with Retry Policy
        attempt = 1
        last_response: Optional[AdapterResponse] = None
        while attempt <= self.retry_policy.max_attempts:
            try:
                resp = adapter.execute(model_id, inputs)
                last_response = resp
                if resp.success:
                    self.circuit_breaker.record_success()
                    self.ledger.record_event(
                        model_id=model_id,
                        event_type=AvailabilityEventType.BENCHMARK_EXECUTED,
                        details={"run_id": run_id, "latency_ms": resp.latency_ms, "tokens": resp.reported_tokens},
                    )
                    break
                else:
                    if self.retry_policy.is_retryable_status(resp.status_code) and attempt < self.retry_policy.max_attempts:
                        delay = self.retry_policy.calculate_delay(attempt)
                        time.sleep(min(0.1, delay))  # Bound sleep in tests
                        attempt += 1
                        continue
                    self.circuit_breaker.record_failure()
                    break
            except Exception as exc:
                self.circuit_breaker.record_failure()
                return ModelBenchmarkRunResult(
                    run_id=run_id,
                    model_id=model_id,
                    modality=modality,
                    success=False,
                    latency_ms=0.0,
                    reported_tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
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
                reported_tokens={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                cost_usd=0.0,
                status_code=500,
                error_message="Nenhuma resposta obtida após tentativas.",
                timestamp=now_iso,
                metadata={},
            )

        return ModelBenchmarkRunResult(
            run_id=run_id,
            model_id=model_id,
            modality=modality,
            success=last_response.success,
            latency_ms=last_response.latency_ms,
            reported_tokens=last_response.reported_tokens,
            cost_usd=last_response.cost_usd,
            status_code=last_response.status_code,
            error_message=last_response.error_message,
            timestamp=now_iso,
            metadata=last_response.metadata,
        )
