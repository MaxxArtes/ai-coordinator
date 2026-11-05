"""HTTP Transport abstraction for OpenRouter API with centralized Egress Point Gate (Phase 4C.1).

Guarantees:
1. Strict Egress Gate: Every dispatch MUST pass through FreeCostGuard validation
   (model, price, modality compatibility, and payload inspection) before any HTTP request.
2. Direct calls to dispatch() cannot bypass CostGuard.
3. Strict opt-in control: Live network is permanently disabled by default (live_network_enabled=False).
4. Endpoints strictly separated by modality without fallback to /chat/completions.
5. Captures Retry-After headers, status codes, and latency.
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

from .cost_guard import CostGuardError, FreeCostGuard, UnauthorizedModelError

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
    """Standardized response from HTTP transport."""
    status_code: int
    data: Dict[str, Any]
    latency_ms: float
    retry_after_sec: Optional[float] = None
    headers: Dict[str, str] = dataclasses.field(default_factory=dict)
    raw_error: Optional[str] = None


class OpenRouterHttpTransport:
    """Manages outbound HTTP requests to OpenRouter with centralized egress validation."""

    def __init__(
        self,
        cost_guard: Optional[FreeCostGuard] = None,
        api_key: Optional[str] = None,
        live_network_enabled: bool = False,
        timeout_sec: float = 30.0,
    ) -> None:
        self.cost_guard = cost_guard
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.live_network_enabled = live_network_enabled
        self.timeout_sec = timeout_sec

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
    ) -> HttpResponse:
        """Dispatch HTTP POST request to OpenRouter endpoint.

        Mandatory Gates before dispatch:
        1. Modality endpoint validation (rejects unknown modalities fail-closed).
        2. Centralized CostGuard validation (inspects payload, authorizes model and zero-cost guarantee).
        3. Modality compatibility verification against catalog entry.
        4. Live network opt-in gate (live_network_enabled must be True).
        """
        # Gate 1: Endpoint validation
        url = self.get_endpoint_for_modality(modality_str)

        # Gate 2: Centralized Egress CostGuard enforcement
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
        entry = self.cost_guard.authorize_model_execution(str(model_id))

        # Gate 3: Verify modality compatibility
        norm_mod = modality_str.strip().lower()
        entry_modality_values = [m.value for m in entry.modalities]
        if norm_mod not in entry_modality_values:
            raise IncompatibleModalityEndpointError(
                f"[FAIL-CLOSED] Modelo '{model_id}' não suporta a modalidade '{modality_str}'. "
                f"Modalidades declaradas pelo catálogo: {entry_modality_values}."
            )

        # Gate 4: Opt-in network check
        if not self.live_network_enabled:
            raise LiveNetworkDisabledError(
                "[FAIL-CLOSED] Transporte de rede HTTP real desativado por padrão. "
                "Para executar chamadas de rede reais, habilite explicitamente live_network_enabled=True."
            )

        if not self.api_key:
            raise TransportError(
                "Chave de API da OpenRouter não configurada. Defina a variável de ambiente OPENROUTER_API_KEY."
            )

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
                body = response.read().decode("utf-8")
                parsed_json = json.loads(body) if body else {}

                return HttpResponse(
                    status_code=status_code,
                    data=parsed_json,
                    latency_ms=elapsed_ms,
                    headers=resp_headers,
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
            return HttpResponse(
                status_code=500,
                data={},
                latency_ms=elapsed_ms,
                raw_error=str(exc),
            )
