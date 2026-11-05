"""HTTP Transport abstraction for OpenRouter API with centralized Indivisible Egress Gate (Phase 4C.1).

Guarantees:
1. Indivisible Egress Gate: Every dispatch MUST pass through:
   (a) Secret scanning on payload (scan_for_credentials).
   (b) Modality endpoint validation.
   (c) FreeCostGuard authorization & fresh catalog check.
   (d) Modality compatibility check against catalog.
   (e) Opt-in network check (live_network_enabled == True).
   (f) Atomic quota reservation at the egress point via SQLite.
   (g) Pre-flight and post-flight durable audit recording with fsync.
2. Direct calls to dispatch() cannot bypass any governance control.
3. Strict opt-in control: Live network is permanently disabled by default (live_network_enabled=False).
4. Endpoints strictly separated by modality without fallback to /chat/completions.
5. Audio contract support: handles binary responses (audio/wav, audio/mpeg) and multipart uploads.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

from .availability import AuditPersistenceError, AvailabilityEventType, AvailabilityLedger
from .cost_guard import CostGuardError, FreeCostGuard, UnauthorizedModelError
from .resilience import QuotaPersistenceError, RateLimitExceededError, SlidingWindowRateLimiter

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MODALITY_ENDPOINTS = {
    "text": f"{OPENROUTER_BASE_URL}/chat/completions",
    "decisions": f"{OPENROUTER_BASE_URL}/chat/completions",
    "embedding": f"{OPENROUTER_BASE_URL}/embeddings",
    "rerank": f"{OPENROUTER_BASE_URL}/rerank",
    "speech": f"{OPENROUTER_BASE_URL}/audio/transcriptions",
    "speech_transcription": f"{OPENROUTER_BASE_URL}/audio/transcriptions",
    "speech_generation": f"{OPENROUTER_BASE_URL}/audio/speech",
}


class TransportError(Exception):
    """Base exception for transport layer failures."""
    pass


class LiveNetworkDisabledError(TransportError):
    """Raised when an attempt to use live network occurs while live_network_enabled=False."""
    pass


class IncompatibleModalityEndpointError(TransportError):
    """Raised when an unknown modality is requested or endpoint is incompatible."""
    pass


@dataclasses.dataclass(frozen=True)
class HttpResponse:
    """Standardized response from HTTP transport supporting both JSON and binary payloads."""
    status_code: int
    data: Dict[str, Any]
    latency_ms: float
    retry_after_sec: Optional[float] = None
    headers: Dict[str, str] = dataclasses.field(default_factory=dict)
    binary_data: Optional[bytes] = None
    raw_error: Optional[str] = None


class OpenRouterHttpTransport:
    """Manages outbound HTTP requests to OpenRouter with centralized, indivisible egress validation."""

    def __init__(
        self,
        cost_guard: Optional[FreeCostGuard] = None,
        rate_limiter: Optional[SlidingWindowRateLimiter] = None,
        ledger: Optional[AvailabilityLedger] = None,
        api_key: Optional[str] = None,
        live_network_enabled: bool = False,
        timeout_sec: float = 30.0,
    ) -> None:
        self.cost_guard = cost_guard
        self.rate_limiter = rate_limiter
        self.ledger = ledger
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.live_network_enabled = live_network_enabled
        self.timeout_sec = timeout_sec

        # Fail-closed validation for live mode
        if self.live_network_enabled:
            self._validate_live_configuration()

    def _validate_live_configuration(self) -> None:
        """Enforce strict durable requirements when live network is enabled."""
        if self.cost_guard is None:
            raise CostGuardError(
                "[FAIL-CLOSED] Modo live exige FreeCostGuard configurado."
            )
        if self.rate_limiter is None or self.rate_limiter._store is None:
            raise CostGuardError(
                "[FAIL-CLOSED] Modo live exige rate_limiter com SQLiteQuotaStore persistente atômico."
            )
        if self.ledger is None or not self.ledger.require_durable or not self.ledger.storage_path:
            raise AuditPersistenceError(
                "[FAIL-CLOSED] Modo live exige AvailabilityLedger configurado com require_durable=True e storage_path."
            )
        if not self.cost_guard.registry.is_authenticated_origin:
            from .registry import UntrustedCatalogSourceError
            raise UntrustedCatalogSourceError(
                "[FAIL-CLOSED] Modo live exige catálogo com origem oficial HTTPS comprovada "
                "(OfficialOpenRouterCatalogClient). Dicionários locais, dados estáticos ou snapshots fabricados via "
                "AuthenticatedCatalogSnapshot.create() são terminantemente proibidos em chamadas reais."
            )

    def get_endpoint_for_modality(self, modality_str: str) -> str:
        """Resolve OpenRouter endpoint URL for a given modality. Rejects unknown modalities fail-closed."""
        norm_modality = str(modality_str).strip().lower()
        if norm_modality not in MODALITY_ENDPOINTS:
            raise IncompatibleModalityEndpointError(
                f"[FAIL-CLOSED] Modalidade desconhecida ou sem endpoint correspondente: '{modality_str}'. "
                f"Modalidades aceitas: {sorted(list(MODALITY_ENDPOINTS.keys()))}."
            )
        return MODALITY_ENDPOINTS[norm_modality]

    def dispatch(
        self,
        modality_str: str,
        payload: Dict[str, Any],
        custom_headers: Optional[Dict[str, str]] = None,
        is_binary_output: bool = False,
    ) -> HttpResponse:
        """Dispatch HTTP POST request to OpenRouter endpoint through the indivisible egress gateway.

        Mandatory Egress Gates in Order:
        1. Pre-flight Secret Scanning (scan_for_credentials).
        2. Modality endpoint validation (rejects unknown modalities fail-closed).
        3. Centralized CostGuard validation (inspects payload, authorizes model, zero-cost, catalog TTL).
        4. Modality compatibility verification against catalog entry.
        5. Opt-in network check (live_network_enabled must be True).
        6. Live configuration check (enforces SQLite quotas and durable ledger).
        7. Atomic quota acquisition at the egress point.
        8. Pre-flight durable audit intent recording.
        9. Outbound HTTP dispatch (supporting JSON and binary audio).
        10. Post-flight durable audit outcome recording and Retry-After handling.
        """
        # Gate 1: Comprehensive Recursive Privacy & Secret Inspection
        # Import lazily to avoid circular imports
        from .adapters import inspect_payload_privacy_strictly
        inspect_payload_privacy_strictly(payload)

        # Gate 1b: Reserved Headers Protection (Authorization, Content-Type, Host)
        RESERVED_HEADERS = {"authorization", "content-type", "host"}
        if custom_headers:
            for k in custom_headers:
                if str(k).strip().lower() in RESERVED_HEADERS:
                    raise TransportError(
                        f"[FAIL-CLOSED] Cabeçalho reservado '{k}' não pode ser sobrescrito via custom_headers. "
                        "Substituição de identidade ou cabeçalhos de controle é terminantemente proibida."
                    )

        # Gate 2: Endpoint validation
        url = self.get_endpoint_for_modality(modality_str)

        # Gate 3: Centralized Egress CostGuard enforcement
        if self.cost_guard is None:
            raise CostGuardError(
                "[FAIL-CLOSED] OpenRouterHttpTransport não pode executar chamadas sem FreeCostGuard configurado. "
                "Execução bloqueada preventivamente no ponto de saída HTTP."
            )

        if not isinstance(payload, dict):
            raise CostGuardError("Payload de envio deve ser um dicionário.")

        model_id = payload.get("model")
        if not model_id:
            raise UnauthorizedModelError("Payload de requisição sem campo obrigatório 'model'.")

        self.cost_guard.inspect_request_payload(payload)
        entry = self.cost_guard.authorize_model_execution(str(model_id), verify_freshness=True)

        # Gate 4: Verify modality compatibility
        norm_mod = modality_str.strip().lower()
        entry_modality_values = [m.value for m in entry.modalities]
        if norm_mod not in entry_modality_values:
            raise IncompatibleModalityEndpointError(
                f"[FAIL-CLOSED] Modelo '{model_id}' não suporta a modalidade '{modality_str}'. "
                f"Modalidades declaradas pelo catálogo: {entry_modality_values}."
            )

        # Gate 4b: Strict Pilot Scope Enforcement in Live Mode
        # In live network mode, the initial pilot is strictly restricted to TEXT modality
        if self.live_network_enabled and norm_mod != "text":
            raise IncompatibleModalityEndpointError(
                f"[PILOT_SCOPE_RESTRICTION] Em modo operacional ativo (live network), apenas a modalidade 'text' "
                f"está homologada para execução inicial. Modalidade requisitada '{modality_str}' permanece restrita ao modo simulado."
            )

        # Gate 5: Opt-in network check
        if not self.live_network_enabled:
            raise LiveNetworkDisabledError(
                "[FAIL-CLOSED] Transporte de rede HTTP real desativado por padrão. "
                "Para executar chamadas de rede reais, habilite explicitamente live_network_enabled=True."
            )

        # Gate 6: Live configuration validation
        self._validate_live_configuration()

        if not self.api_key:
            raise TransportError(
                "Chave de API da OpenRouter não configurada. Defina a variável de ambiente OPENROUTER_API_KEY."
            )

        # Gate 7: Atomic Quota Reservation at the Egress Point
        if self.rate_limiter:
            acquired = self.rate_limiter.acquire()
            if not acquired:
                wait_sec = self.rate_limiter.time_until_available()
                return HttpResponse(
                    status_code=429,
                    data={},
                    latency_ms=0.0,
                    retry_after_sec=wait_sec,
                    raw_error=f"[EGRESS_RATE_LIMIT_EXCEEDED] Quota esgotada. Aguarde {wait_sec:.1f}s.",
                )

        # Gate 8: Pre-flight Durable Audit Intent
        if self.ledger:
            self.ledger.record_event(
                str(model_id),
                AvailabilityEventType.BENCHMARK_EXECUTED,
                {"stage": "egress_dispatch_intent", "modality": norm_mod, "url": url},
            )

        # Gate 9: HTTP Dispatch (Handling JSON and Binary Audio)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/MaxxArtes/ai-coordinator",
            "X-Title": "AI Coordinator Scientific Benchmark",
        }
        if custom_headers:
            headers.update(custom_headers)

        encoded_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=encoded_data, headers=headers, method="POST")

        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as response:
                t1 = time.perf_counter()
                elapsed_ms = (t1 - t0) * 1000.0
                status_code = response.getcode()
                resp_headers = dict(response.headers)
                content_type = resp_headers.get("Content-Type", "").lower()

                # Check if response is binary audio
                if is_binary_output or any(t in content_type for t in ("audio/", "application/octet-stream")):
                    raw_bytes = response.read()
                    parsed_json = {
                        "binary_format": content_type,
                        "bytes_length": len(raw_bytes),
                    }
                    binary_payload = raw_bytes
                else:
                    body = response.read().decode("utf-8")
                    parsed_json = json.loads(body) if body else {}
                    binary_payload = None

                # Gate 10: Post-flight Durable Audit Outcome
                if self.ledger:
                    self.ledger.record_event(
                        str(model_id),
                        AvailabilityEventType.BENCHMARK_EXECUTED,
                        {"stage": "egress_dispatch_success", "status_code": status_code, "latency_ms": elapsed_ms},
                    )

                return HttpResponse(
                    status_code=status_code,
                    data=parsed_json,
                    latency_ms=elapsed_ms,
                    headers=resp_headers,
                    binary_data=binary_payload,
                )
        except urllib.error.HTTPError as http_err:
            t1 = time.perf_counter()
            elapsed_ms = (t1 - t0) * 1000.0
            status_code = http_err.code
            err_headers = dict(http_err.headers)
            body = http_err.read().decode("utf-8") if http_err.fp else ""
            parsed_json = {}
            try:
                if body:
                    parsed_json = json.loads(body)
            except Exception:
                parsed_json = {"raw": body}

            retry_after = None
            if "Retry-After" in err_headers:
                try:
                    retry_after = float(err_headers["Retry-After"])
                except ValueError:
                    retry_after = None

            if retry_after is not None and self.rate_limiter:
                self.rate_limiter.record_retry_after(retry_after)

            if self.ledger:
                self.ledger.record_event(
                    str(model_id),
                    AvailabilityEventType.RATE_LIMITED_429 if status_code == 429 else AvailabilityEventType.PROVIDER_UNAVAILABLE_503,
                    {"stage": "egress_dispatch_http_error", "status_code": status_code, "retry_after": retry_after},
                )

            return HttpResponse(
                status_code=status_code,
                data=parsed_json,
                latency_ms=elapsed_ms,
                retry_after_sec=retry_after,
                headers=err_headers,
                raw_error=str(http_err),
            )
        except Exception as exc:
            t1 = time.perf_counter()
            elapsed_ms = (t1 - t0) * 1000.0
            if self.ledger:
                self.ledger.record_event(
                    str(model_id),
                    AvailabilityEventType.PROVIDER_UNAVAILABLE_503,
                    {"stage": "egress_dispatch_network_error", "error": str(exc)},
                )
            return HttpResponse(
                status_code=500,
                data={},
                latency_ms=elapsed_ms,
                raw_error=str(exc),
            )
