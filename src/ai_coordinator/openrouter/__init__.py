"""OpenRouter Free Model Integration and Resilience Subsystem (Phase 4C.1)."""
from .registry import (
    ModelModality,
    ModelPricing,
    ModelCatalogEntry,
    FreeModelRegistry,
)
from .cost_guard import (
    FreeCostGuard,
    CostGuardError,
    UnauthorizedModelError,
    PaidFallbackBlockedError,
)
from .availability import (
    AvailabilityEventType,
    AvailabilityEvent,
    AvailabilityLedger,
)
from .resilience import (
    SlidingWindowRateLimiter,
    CircuitState,
    CircuitBreaker,
    CircuitOpenError,
    RateLimitExceededError,
    RetryPolicy,
)
from .adapters import (
    BaseModelAdapter,
    TextModelAdapter,
    EmbeddingModelAdapter,
    RerankModelAdapter,
    DecisionModelAdapter,
    SpeechModelAdapter,
    AdapterResponse,
)
from .runner import (
    ModelBenchmarkRunner,
    ModelBenchmarkRunResult,
)

__all__ = [
    "ModelModality",
    "ModelPricing",
    "ModelCatalogEntry",
    "FreeModelRegistry",
    "FreeCostGuard",
    "CostGuardError",
    "UnauthorizedModelError",
    "PaidFallbackBlockedError",
    "AvailabilityEventType",
    "AvailabilityEvent",
    "AvailabilityLedger",
    "SlidingWindowRateLimiter",
    "CircuitState",
    "CircuitBreaker",
    "CircuitOpenError",
    "RateLimitExceededError",
    "RetryPolicy",
    "BaseModelAdapter",
    "TextModelAdapter",
    "EmbeddingModelAdapter",
    "RerankModelAdapter",
    "DecisionModelAdapter",
    "SpeechModelAdapter",
    "AdapterResponse",
    "ModelBenchmarkRunner",
    "ModelBenchmarkRunResult",
]
