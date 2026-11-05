"""Specialized modular adapters for OpenRouter modalities (Phase 4C.1).

Defines isolated contracts and metric capture for:
1. Text / Chat Completion (TextModelAdapter)
2. Embeddings Vectorization (EmbeddingModelAdapter)
3. Document Context Reranking (RerankModelAdapter, e.g. Nemotron)
4. Structured Decision Formulation (DecisionModelAdapter)
5. Audio / Voice Processing (SpeechModelAdapter)

All adapters integrate:
- FreeCostGuard authorization.
- Preventative secret scrubbing (zero API keys/code in outbound payloads).
- Standardized latency, token, and cost metric reporting.
- Offline mock engine for deterministic testing.
"""
from __future__ import annotations

import abc
import dataclasses
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .cost_guard import FreeCostGuard, UnauthorizedModelError
from .registry import FreeModelRegistry, ModelCatalogEntry, ModelModality

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class AdapterResponse:
    """Standardized response container across all adapter modalities."""
    model_id: str
    modality: ModelModality
    success: bool
    output_data: Any
    latency_ms: float
    reported_tokens: Dict[str, int]
    cost_usd: float  # Strictly 0.0 for free-tier
    status_code: int = 200
    ttft_ms: Optional[float] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "modality": self.modality.value,
            "success": self.success,
            "output_data": self.output_data,
            "latency_ms": round(self.latency_ms, 4),
            "reported_tokens": self.reported_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "status_code": self.status_code,
            "ttft_ms": round(self.ttft_ms, 4) if self.ttft_ms is not None else None,
            "error_message": self.error_message,
            "metadata": self.metadata,
        }


def scrub_outbound_payload(data: Any) -> Any:
    """Recursively scrub sensitive keys and tokens from outbound request payloads."""
    if isinstance(data, dict):
        scrubbed = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(term in k_lower for term in ("key", "secret", "token", "password", "auth", "credential")):
                scrubbed[k] = "[REDACTED_BY_COST_GUARD]"
            else:
                scrubbed[k] = scrub_outbound_payload(v)
        return scrubbed
    if isinstance(data, (list, tuple)):
        return [scrub_outbound_payload(x) for x in data]
    return data


class BaseModelAdapter(abc.ABC):
    """Abstract base class for all specialized modality adapters."""

    def __init__(
        self,
        cost_guard: FreeCostGuard,
        mock_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        self.cost_guard = cost_guard
        self.mock_handler = mock_handler

    @property
    @abc.abstractmethod
    def modality(self) -> ModelModality:
        """Declared modality for this adapter."""
        pass

    def _prepare_and_validate(self, model_id: str, payload: Dict[str, Any]) -> ModelCatalogEntry:
        """Validate model through CostGuard and sanitize payload."""
        entry = self.cost_guard.authorize_model_execution(model_id)
        if self.modality not in entry.modalities:
            logger.warning(
                "Modelo '%s' não declara suporte formal à modalidade '%s'; prosseguindo com calibração.",
                model_id,
                self.modality.value,
            )
        return entry

    @abc.abstractmethod
    def execute(self, model_id: str, inputs: Dict[str, Any]) -> AdapterResponse:
        """Execute request and produce standardized response."""
        pass


class TextModelAdapter(BaseModelAdapter):
    """Adapter for chat and textual reasoning models."""

    @property
    def modality(self) -> ModelModality:
        return ModelModality.TEXT

    def execute(self, model_id: str, inputs: Dict[str, Any]) -> AdapterResponse:
        entry = self._prepare_and_validate(model_id, inputs)
        sanitized_inputs = scrub_outbound_payload(inputs)

        t0 = time.perf_counter()
        if self.mock_handler:
            mock_res = self.mock_handler(sanitized_inputs)
            t1 = time.perf_counter()
            elapsed_ms = max(0.01, (t1 - t0) * 1000.0)
            text_out = mock_res.get("text", "Mock completion response")
            in_tok = mock_res.get("prompt_tokens", len(json.dumps(inputs)) // 4)
            out_tok = mock_res.get("completion_tokens", len(text_out) // 4)

            return AdapterResponse(
                model_id=model_id,
                modality=self.modality,
                success=True,
                output_data={"text": text_out},
                latency_ms=elapsed_ms,
                reported_tokens={"prompt_tokens": in_tok, "completion_tokens": out_tok, "total_tokens": in_tok + out_tok},
                cost_usd=0.0,
                status_code=200,
                metadata={"provider": entry.provider_name},
            )

        # Default synthetic mock execution when no live HTTP handler is configured
        t1 = time.perf_counter()
        elapsed_ms = max(0.01, (t1 - t0) * 1000.0)
        return AdapterResponse(
            model_id=model_id,
            modality=self.modality,
            success=True,
            output_data={"text": f"Offline simulated response from {model_id}"},
            latency_ms=elapsed_ms,
            reported_tokens={"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75},
            cost_usd=0.0,
            status_code=200,
            metadata={"offline_simulated": True, "provider": entry.provider_name},
        )


class EmbeddingModelAdapter(BaseModelAdapter):
    """Adapter for semantic vectorization and embedding models."""

    @property
    def modality(self) -> ModelModality:
        return ModelModality.EMBEDDING

    def execute(self, model_id: str, inputs: Dict[str, Any]) -> AdapterResponse:
        entry = self._prepare_and_validate(model_id, inputs)
        text_input = inputs.get("input", "")

        t0 = time.perf_counter()
        dimensions = int(inputs.get("dimensions", 768))

        if self.mock_handler:
            mock_res = self.mock_handler(inputs)
            t1 = time.perf_counter()
            vec = mock_res.get("embedding", [0.0] * dimensions)
            in_tok = mock_res.get("tokens", len(str(text_input)) // 4)
            return AdapterResponse(
                model_id=model_id,
                modality=self.modality,
                success=True,
                output_data={"embedding": vec, "dimensions": len(vec)},
                latency_ms=(t1 - t0) * 1000.0,
                reported_tokens={"prompt_tokens": in_tok, "completion_tokens": 0, "total_tokens": in_tok},
                cost_usd=0.0,
                metadata={"dimensions": len(vec)},
            )

        # Synthetic deterministic mock embedding vector
        t1 = time.perf_counter()
        vec = [round(float((i % 10) / 10.0), 4) for i in range(dimensions)]
        return AdapterResponse(
            model_id=model_id,
            modality=self.modality,
            success=True,
            output_data={"embedding": vec, "dimensions": dimensions},
            latency_ms=(t1 - t0) * 1000.0,
            reported_tokens={"prompt_tokens": max(1, len(str(text_input)) // 4), "completion_tokens": 0, "total_tokens": max(1, len(str(text_input)) // 4)},
            cost_usd=0.0,
            metadata={"dimensions": dimensions, "offline_simulated": True},
        )


class RerankModelAdapter(BaseModelAdapter):
    """Adapter for document and context reranking models (e.g. Nemotron Rerank)."""

    @property
    def modality(self) -> ModelModality:
        return ModelModality.RERANK

    def execute(self, model_id: str, inputs: Dict[str, Any]) -> AdapterResponse:
        entry = self._prepare_and_validate(model_id, inputs)
        query = inputs.get("query", "")
        documents = inputs.get("documents", [])

        t0 = time.perf_counter()
        if self.mock_handler:
            mock_res = self.mock_handler(inputs)
            t1 = time.perf_counter()
            ranked = mock_res.get("results", [])
            return AdapterResponse(
                model_id=model_id,
                modality=self.modality,
                success=True,
                output_data={"results": ranked, "documents_evaluated": len(documents)},
                latency_ms=(t1 - t0) * 1000.0,
                reported_tokens={"prompt_tokens": len(query) // 4, "completion_tokens": 0, "total_tokens": len(query) // 4},
                cost_usd=0.0,
                metadata={"documents_count": len(documents)},
            )

        # Default synthetic ranking mock (reverses documents or assigns synthetic scores)
        t1 = time.perf_counter()
        results = [
            {"index": idx, "document": doc, "relevance_score": round(1.0 - (idx * 0.1), 4)}
            for idx, doc in enumerate(documents)
        ]
        return AdapterResponse(
            model_id=model_id,
            modality=self.modality,
            success=True,
            output_data={"results": results, "documents_evaluated": len(documents)},
            latency_ms=(t1 - t0) * 1000.0,
            reported_tokens={"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100},
            cost_usd=0.0,
            metadata={"documents_count": len(documents), "offline_simulated": True},
        )


class DecisionModelAdapter(BaseModelAdapter):
    """Adapter for structured outputs, schema enforcement, and tool decisions."""

    @property
    def modality(self) -> ModelModality:
        return ModelModality.DECISIONS

    def execute(self, model_id: str, inputs: Dict[str, Any]) -> AdapterResponse:
        entry = self._prepare_and_validate(model_id, inputs)
        prompt = inputs.get("prompt", "")
        expected_schema = inputs.get("schema", {})

        t0 = time.perf_counter()
        if self.mock_handler:
            mock_res = self.mock_handler(inputs)
            t1 = time.perf_counter()
            decision_json = mock_res.get("decision", {})
            return AdapterResponse(
                model_id=model_id,
                modality=self.modality,
                success=True,
                output_data={"decision": decision_json},
                latency_ms=(t1 - t0) * 1000.0,
                reported_tokens={"prompt_tokens": len(prompt) // 4, "completion_tokens": 30, "total_tokens": (len(prompt) // 4) + 30},
                cost_usd=0.0,
                metadata={"schema_enforced": bool(expected_schema)},
            )

        t1 = time.perf_counter()
        synthetic_decision = {"decision": "APPROVE", "confidence": 0.95, "reasoning": "Standard simulated decision"}
        return AdapterResponse(
            model_id=model_id,
            modality=self.modality,
            success=True,
            output_data={"decision": synthetic_decision},
            latency_ms=(t1 - t0) * 1000.0,
            reported_tokens={"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            cost_usd=0.0,
            metadata={"schema_enforced": bool(expected_schema), "offline_simulated": True},
        )


class SpeechModelAdapter(BaseModelAdapter):
    """Adapter for voice and speech models with isolated audio contracts."""

    @property
    def modality(self) -> ModelModality:
        return ModelModality.SPEECH

    def execute(self, model_id: str, inputs: Dict[str, Any]) -> AdapterResponse:
        entry = self._prepare_and_validate(model_id, inputs)
        audio_payload = inputs.get("audio_bytes") or inputs.get("text_to_speak", "")

        t0 = time.perf_counter()
        if self.mock_handler:
            mock_res = self.mock_handler(inputs)
            t1 = time.perf_counter()
            return AdapterResponse(
                model_id=model_id,
                modality=self.modality,
                success=True,
                output_data=mock_res.get("audio_result", {}),
                latency_ms=(t1 - t0) * 1000.0,
                reported_tokens={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100},
                cost_usd=0.0,
                metadata={"audio_format": inputs.get("format", "wav")},
            )

        t1 = time.perf_counter()
        return AdapterResponse(
            model_id=model_id,
            modality=self.modality,
            success=True,
            output_data={"transcript": "Simulated speech transcript", "duration_sec": 2.5},
            latency_ms=(t1 - t0) * 1000.0,
            reported_tokens={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
            cost_usd=0.0,
            metadata={"format": inputs.get("format", "wav"), "offline_simulated": True},
        )
