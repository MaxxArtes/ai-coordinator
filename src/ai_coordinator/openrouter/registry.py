"""Dynamic discovery and registration of OpenRouter free-tier models (Phase 4C.1).

Parses real-time catalog snapshots from the official OpenRouter API, filters
strictly free-tier models, classifies modalities, and extracts capability metadata.
Enforces:
1. Strict validation on pricing: missing, NaN, infinite, or negative pricing
   is treated as INVALID and blocks free qualification.
2. TTL and freshness verification: expired catalogs block execution fail-closed.
3. Authenticated provenance: OfficialOpenRouterCatalogClient fetches over verified HTTPS;
   in production/live mode, untrusted external dictionaries are rejected.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from enum import Enum
import hashlib
import json
import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional, Set
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

OFFICIAL_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class ModelModality(str, Enum):
    """Supported model capabilities and modalities."""
    TEXT = "text"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    DECISIONS = "decisions"
    SPEECH = "speech"
    SPEECH_TRANSCRIPTION = "speech_transcription"
    SPEECH_GENERATION = "speech_generation"


class PricingStatus(str, Enum):
    """Integrity status of model pricing data."""
    VALID_ZERO = "VALID_ZERO"
    PAID = "PAID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class InvalidPricingError(ValueError):
    """Raised when a pricing value is missing, negative, NaN, infinite, or malformed."""
    pass


class StaleCatalogError(Exception):
    """Raised when the model catalog snapshot is expired or cannot be freshly verified."""
    pass


class UntrustedCatalogSourceError(Exception):
    """Raised when an unauthenticated or untrusted catalog source is provided in live mode."""
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

    if "rerank" in model_lower or "rerank" in desc_lower or "rerank" in modality_str:
        modalities.add(ModelModality.RERANK)

    if "embed" in model_lower or "embedding" in desc_lower or "text->embedding" in modality_str:
        modalities.add(ModelModality.EMBEDDING)

    is_speech_transcription = (
        "audio->text" in modality_str
        or "whisper" in model_lower
        or "transcription" in desc_lower
        or "stt" in model_lower
    )
    is_speech_generation = (
        "text->audio" in modality_str
        or "tts" in model_lower
        or "voice" in model_lower
        or "speech generation" in desc_lower
        or "fish audio" in desc_lower
        or "heygen" in desc_lower
    )

    if is_speech_transcription:
        modalities.add(ModelModality.SPEECH)
        modalities.add(ModelModality.SPEECH_TRANSCRIPTION)

    if is_speech_generation:
        modalities.add(ModelModality.SPEECH)
        modalities.add(ModelModality.SPEECH_GENERATION)

    tools_supported = False
    supported_params = arch.get("supported_parameters", [])
    if isinstance(supported_params, list):
        if any(p in ("tools", "functions", "tool_choice") for p in supported_params):
            tools_supported = True
    if tools_supported or "decision" in model_lower or "function" in desc_lower:
        modalities.add(ModelModality.DECISIONS)

    if not modalities or "chat" in model_lower or "instruct" in model_lower or "text->text" in modality_str:
        modalities.add(ModelModality.TEXT)

    if tools_supported:
        modalities.add(ModelModality.DECISIONS)

    return sorted(list(modalities), key=lambda m: m.value)


class OfficialOpenRouterCatalogClient:
    """Official HTTPS catalog client directly querying OpenRouter's /api/v1/models endpoint."""

    def __init__(self, endpoint_url: str = OFFICIAL_OPENROUTER_MODELS_URL, timeout_sec: float = 30.0) -> None:
        self.endpoint_url = endpoint_url
        self.timeout_sec = timeout_sec

    def fetch_catalog(self) -> Dict[str, Any]:
        """Fetch and structure-check official catalog over HTTPS.

        Validates status 200, Content-Type application/json, and required data schema.
        """
        req = urllib.request.Request(
            self.endpoint_url,
            headers={
                "User-Agent": "AI-Coordinator/0.5.1 (Scientific Benchmark Suite)",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                if resp.getcode() != 200:
                    raise StaleCatalogError(f"HTTP {resp.getcode()} ao consultar catálogo oficial da OpenRouter.")
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(data, dict) or "data" not in data or not isinstance(data["data"], list):
                    raise StaleCatalogError("Estrutura inválida retornada pelo endpoint oficial da OpenRouter.")

                data["_source"] = self.endpoint_url
                data["_trusted"] = True
                data["_fetched_at_epoch"] = time.time()
                return data
        except Exception as exc:
            logger.error("Falha ao consultar endpoint oficial da OpenRouter: %s", exc)
            raise StaleCatalogError(f"Falha na consulta oficial HTTPS do catálogo: {exc}") from exc


class FreeModelRegistry:
    """Dynamic catalog registry and discovery engine with TTL validation and provenance checking."""

    def __init__(
        self,
        max_age_seconds: float = 3600.0,
        fetcher: Optional[Callable[[], Dict[str, Any]]] = None,
        require_trusted_source: bool = False,
    ) -> None:
        self.max_age_seconds = max_age_seconds
        self.fetcher = fetcher
        self.require_trusted_source = require_trusted_source

        self._entries_by_id: Dict[str, ModelCatalogEntry] = {}
        self._raw_snapshot: Optional[Dict[str, Any]] = None
        self._snapshot_timestamp: Optional[str] = None
        self._snapshot_epoch: Optional[float] = None
        self._provenance_hash: Optional[str] = None
        self._source_url: str = "uninitialized"

    @property
    def total_models(self) -> int:
        return len(self._entries_by_id)

    @property
    def snapshot_epoch(self) -> Optional[float]:
        return self._snapshot_epoch

    @property
    def provenance_hash(self) -> Optional[str]:
        return self._provenance_hash

    @property
    def source_url(self) -> str:
        return self._source_url

    def is_stale(self, now: Optional[float] = None) -> bool:
        """Check if the current catalog snapshot has exceeded its TTL."""
        if self._snapshot_epoch is None:
            return True
        t = now if now is not None else time.time()
        return (t - self._snapshot_epoch) > self.max_age_seconds

    def ensure_fresh_catalog(self, force: bool = False, now: Optional[float] = None) -> None:
        """Ensure catalog snapshot is fresh and trusted. Fetches updates or raises StaleCatalogError."""
        t = now if now is not None else time.time()
        if not force and not self.is_stale(now=t):
            return

        if self.fetcher is not None:
            try:
                raw_data = self.fetcher()
                is_trusted = bool(raw_data.get("_trusted", False))
                self.load_catalog_data(
                    raw_data,
                    epoch=t,
                    source=raw_data.get("_source", OFFICIAL_OPENROUTER_MODELS_URL),
                    is_trusted=is_trusted,
                )
                return
            except Exception as exc:
                logger.error("Falha ao atualizar catálogo dinamicamente: %s", exc)
                raise StaleCatalogError(
                    f"[FAIL-CLOSED] Falha ao consultar endpoint oficial de catálogo da OpenRouter: {exc}"
                ) from exc

        raise StaleCatalogError(
            f"[FAIL-CLOSED] Catálogo de modelos expirado ({t - (self._snapshot_epoch or 0.0):.1f}s > "
            f"{self.max_age_seconds:.1f}s) e nenhum mecanismo de atualização está disponível. "
            "Execução bloqueada preventivamente."
        )

    def load_catalog_data(
        self,
        raw_data: Dict[str, Any],
        timestamp: Optional[str] = None,
        epoch: Optional[float] = None,
        source: str = "untrusted_input",
        is_trusted: bool = False,
    ) -> List[ModelCatalogEntry]:
        """Parse raw OpenRouter /api/v1/models response and index models."""
        if self.require_trusted_source and not is_trusted:
            raise UntrustedCatalogSourceError(
                "[FAIL-CLOSED] Catálogo rejeitado: em modo de produção/live, apenas snapshots originados "
                "diretamente do endpoint oficial HTTPS da OpenRouter são autorizados."
            )

        self._raw_snapshot = raw_data
        self._snapshot_epoch = epoch if epoch is not None else time.time()
        self._snapshot_timestamp = timestamp or dt.datetime.now(dt.timezone.utc).isoformat()
        self._source_url = source
        canonical_bytes = json.dumps(raw_data, sort_keys=True).encode("utf-8")
        self._provenance_hash = hashlib.sha256(canonical_bytes).hexdigest()

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

            is_free = (pricing_status == PricingStatus.VALID_ZERO and pricing.is_strictly_zero)

            arch = item.get("architecture", {})
            if not isinstance(arch, dict):
                arch = {}

            desc = item.get("description", "")
            modalities = infer_modalities(m_id, arch, desc)

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

    def get_model(self, model_id: str, verify_freshness: bool = True) -> Optional[ModelCatalogEntry]:
        """Get catalog entry by exact model ID, verifying freshness if requested."""
        if verify_freshness:
            self.ensure_fresh_catalog()
        return self._entries_by_id.get(model_id)

    def is_model_free(self, model_id: str, verify_freshness: bool = True) -> bool:
        """Check if a given model ID is cataloged, fresh, and confirmed as strictly free."""
        entry = self.get_model(model_id, verify_freshness=verify_freshness)
        if not entry:
            return False
        return entry.is_free and entry.pricing.is_strictly_zero

    def export_snapshot(self) -> Dict[str, Any]:
        """Export serialized snapshot of currently indexed models."""
        return {
            "snapshot_timestamp": self._snapshot_timestamp,
            "snapshot_epoch": self._snapshot_epoch,
            "provenance_hash": self._provenance_hash,
            "source_url": self._source_url,
            "total_models": len(self._entries_by_id),
            "free_models_count": len(self.get_free_models()),
            "models": [m.to_dict() for m in self._entries_by_id.values()],
        }
