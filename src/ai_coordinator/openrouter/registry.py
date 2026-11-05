"""Dynamic discovery and registration of OpenRouter free-tier models (Phase 4C.1).

Parses real-time catalog snapshots from the official OpenRouter API, filters
strictly free-tier models, classifies modalities, and extracts capability metadata.
Enforces strict validation on pricing: missing, NaN, infinite, or negative pricing
is treated as INVALID and blocks free qualification.
"""
from __future__ import annotations

import dataclasses
from enum import Enum
import json
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class ModelModality(str, Enum):
    """Supported model capabilities and modalities."""
    TEXT = "text"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    DECISIONS = "decisions"
    SPEECH = "speech"


class PricingStatus(str, Enum):
    """Integrity status of model pricing data."""
    VALID_ZERO = "VALID_ZERO"
    PAID = "PAID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class InvalidPricingError(ValueError):
    """Raised when a pricing value is missing, negative, NaN, infinite, or malformed."""
    pass


@dataclasses.dataclass(frozen=True)
class ModelPricing:
    """Pricing structure per token or request in USD."""
    prompt: float
    completion: float
    image: float = 0.0
    request: float = 0.0
    status: PricingStatus = PricingStatus.UNKNOWN

    @property
    def is_valid(self) -> bool:
        """True only if pricing was validated without errors."""
        return self.status in (PricingStatus.VALID_ZERO, PricingStatus.PAID)

    @property
    def is_strictly_zero(self) -> bool:
        """True only if all pricing dimensions are verified and strictly $0.00."""
        return (
            self.status == PricingStatus.VALID_ZERO
            and abs(self.prompt) < 1e-9
            and abs(self.completion) < 1e-9
            and abs(self.image) < 1e-9
            and abs(self.request) < 1e-9
        )


@dataclasses.dataclass(frozen=True)
class ModelCatalogEntry:
    """Standardized representation of a discovered model."""
    id: str
    name: str
    context_length: int
    pricing: ModelPricing
    modalities: List[ModelModality]
    supports_tools: bool
    is_free: bool
    provider_name: str
    description: str = ""
    created_at_epoch: Optional[int] = None
    architecture: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "context_length": self.context_length,
            "pricing": {
                "prompt": self.pricing.prompt,
                "completion": self.pricing.completion,
                "image": self.pricing.image,
                "request": self.pricing.request,
                "status": self.pricing.status.value,
            },
            "modalities": [m.value for m in self.modalities],
            "supports_tools": self.supports_tools,
            "is_free": self.is_free,
            "provider_name": self.provider_name,
            "description": self.description,
            "created_at_epoch": self.created_at_epoch,
            "architecture": self.architecture,
        }


def parse_float_cost(val: Any, dimension_name: str = "cost") -> float:
    """Strictly parse a cost value in USD.

    Fails closed: missing, negative, NaN, infinite, or non-numeric values
    raise InvalidPricingError.
    """
    if val is None:
        raise InvalidPricingError(f"Valor de preço ausente (None) para dimensão '{dimension_name}'.")

    try:
        f = float(val)
    except (ValueError, TypeError) as exc:
        raise InvalidPricingError(
            f"Valor de preço não-numérico ou malformado '{val}' para dimensão '{dimension_name}'."
        ) from exc

    if math.isnan(f) or math.isinf(f):
        raise InvalidPricingError(
            f"Valor de preço anômalo (NaN ou Infinito: {val}) para dimensão '{dimension_name}'."
        )

    if f < 0.0:
        raise InvalidPricingError(
            f"Valor de preço negativo ({val}) para dimensão '{dimension_name}'."
        )

    return f


def infer_modalities(model_id: str, arch: Dict[str, Any], description: str) -> List[ModelModality]:
    """Deterministically infer model modalities based on model attributes."""
    modalities: Set[ModelModality] = set()
    model_lower = model_id.lower()
    desc_lower = (description or "").lower()
    modality_str = str(arch.get("modality", "")).lower()

    # Reranking detection (e.g. nvidia/nemotron-rerank, bge-reranker, etc.)
    if "rerank" in model_lower or "rerank" in desc_lower or "rerank" in modality_str:
        modalities.add(ModelModality.RERANK)

    # Embedding detection
    if "embed" in model_lower or "embedding" in desc_lower or "text->embedding" in modality_str:
        modalities.add(ModelModality.EMBEDDING)

    # Speech / Audio detection
    if any(k in model_lower for k in ["audio", "speech", "whisper", "tts", "voice"]) or "audio" in modality_str:
        modalities.add(ModelModality.SPEECH)

    # Decision / Tool calling / Function calling detection
    tools_supported = False
    supported_params = arch.get("supported_parameters", [])
    if isinstance(supported_params, list):
        if any(p in ("tools", "functions", "tool_choice") for p in supported_params):
            tools_supported = True
    if tools_supported or "decision" in model_lower or "function" in desc_lower:
        modalities.add(ModelModality.DECISIONS)

    # Standard text completion / chat
    if not modalities or "chat" in model_lower or "instruct" in model_lower or "text->text" in modality_str:
        modalities.add(ModelModality.TEXT)

    # If it supports tools, it can also operate as decisions
    if tools_supported:
        modalities.add(ModelModality.DECISIONS)

    return sorted(list(modalities), key=lambda m: m.value)


class FreeModelRegistry:
    """Dynamic catalog registry and discovery engine for OpenRouter free models."""

    def __init__(self) -> None:
        self._entries_by_id: Dict[str, ModelCatalogEntry] = {}
        self._raw_snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_timestamp: Optional[str] = None

    @property
    def total_models(self) -> int:
        return len(self._entries_by_id)

    def load_catalog_data(self, raw_data: Dict[str, Any], timestamp: Optional[str] = None) -> List[ModelCatalogEntry]:
        """Parse raw OpenRouter /api/v1/models response and index models."""
        self._raw_snapshot = raw_data
        self._snapshot_timestamp = timestamp
        self._entries_by_id.clear()

        data_list = raw_data.get("data", [])
        if not isinstance(data_list, list):
            return []

        for item in data_list:
            if not isinstance(item, dict):
                continue
            m_id = item.get("id", "")
            if not m_id or not isinstance(m_id, str):
                continue

            pricing_raw = item.get("pricing")
            pricing_status = PricingStatus.UNKNOWN
            p_cost = 0.0
            c_cost = 0.0
            img_cost = 0.0
            req_cost = 0.0

            if not isinstance(pricing_raw, dict):
                pricing_status = PricingStatus.INVALID
            else:
                try:
                    p_cost = parse_float_cost(pricing_raw.get("prompt"), "prompt")
                    c_cost = parse_float_cost(pricing_raw.get("completion"), "completion")
                    # image and request might default to 0.0 if key not in dict, but if present must be valid
                    img_raw = pricing_raw.get("image", 0.0)
                    req_raw = pricing_raw.get("request", 0.0)
                    img_cost = parse_float_cost(img_raw, "image")
                    req_cost = parse_float_cost(req_raw, "request")

                    if p_cost == 0.0 and c_cost == 0.0 and img_cost == 0.0 and req_cost == 0.0:
                        pricing_status = PricingStatus.VALID_ZERO
                    else:
                        pricing_status = PricingStatus.PAID
                except InvalidPricingError as err:
                    logger.debug("Preço inválido para modelo '%s': %s", m_id, err)
                    pricing_status = PricingStatus.INVALID

            pricing = ModelPricing(
                prompt=p_cost,
                completion=c_cost,
                image=img_cost,
                request=req_cost,
                status=pricing_status,
            )

            # Strict free-tier qualification:
            # Requires SIMULTANEOUSLY:
            # 1. Pricing status is strictly VALID_ZERO (prompt, completion, image, request all 0.00).
            # 2. Suffix :free is an additional authorization check if configured, but cannot override non-zero cost.
            # 3. If pricing is PAID or INVALID, is_free is permanently FALSE.
            is_free = (pricing_status == PricingStatus.VALID_ZERO and pricing.is_strictly_zero)

            arch = item.get("architecture", {})
            if not isinstance(arch, dict):
                arch = {}

            desc = item.get("description", "")
            modalities = infer_modalities(m_id, arch, desc)

            # Determine tool support
            supported_params = arch.get("supported_parameters", [])
            supports_tools = isinstance(supported_params, list) and any(
                p in ("tools", "functions", "tool_choice") for p in supported_params
            )

            provider = m_id.split("/")[0] if "/" in m_id else "unknown"

            entry = ModelCatalogEntry(
                id=m_id,
                name=item.get("name", m_id),
                context_length=int(item.get("context_length", 4096)),
                pricing=pricing,
                modalities=modalities,
                supports_tools=supports_tools,
                is_free=is_free,
                provider_name=provider,
                description=desc,
                created_at_epoch=item.get("created"),
                architecture=arch,
            )
            self._entries_by_id[m_id] = entry

        return list(self._entries_by_id.values())

    def get_free_models(self) -> List[ModelCatalogEntry]:
        """Return all catalog entries that qualify as 100% free."""
        return [m for m in self._entries_by_id.values() if m.is_free]

    def get_by_modality(self, modality: ModelModality, free_only: bool = True) -> List[ModelCatalogEntry]:
        """Return models supporting a specific modality (text, embedding, rerank, etc.)."""
        results = []
        for m in self._entries_by_id.values():
            if free_only and not m.is_free:
                continue
            if modality in m.modalities:
                results.append(m)
        return results

    def get_model(self, model_id: str) -> Optional[ModelCatalogEntry]:
        """Get catalog entry by exact model ID."""
        return self._entries_by_id.get(model_id)

    def is_model_free(self, model_id: str) -> bool:
        """Check if a given model ID is cataloged and confirmed as strictly free."""
        entry = self.get_model(model_id)
        if not entry:
            return False
        return entry.is_free and entry.pricing.is_strictly_zero

    def export_snapshot(self) -> Dict[str, Any]:
        """Export serialized snapshot of currently indexed models."""
        return {
            "snapshot_timestamp": self._snapshot_timestamp,
            "total_models": len(self._entries_by_id),
            "free_models_count": len(self.get_free_models()),
            "models": [m.to_dict() for m in self._entries_by_id.values()],
        }
