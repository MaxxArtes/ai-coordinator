"""HTTP Transport abstraction for OpenRouter API (Phase 4C.1).

Guarantees:
1. Strict opt-in control: Live network is permanently disabled by default (live_network_enabled=False).
2. Endpoints separated by modality.
3. Pure standard library implementation (urllib.request) with zero external dependencies.
4. Captures Retry-After headers, status codes, and latency.
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

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MODALITY_ENDPOINTS = {
    "text": f"{OPENROUTER_BASE_URL}/chat/completions",
    "decisions": f"{OPENROUTER_BASE_URL}/chat/completions",
    "embedding": f"{OPENROUTER_BASE_URL}/embeddings",
    "rerank": f"{OPENROUTER_BASE_URL}/rerank",
    "speech": f"{OPENROUTER_BASE_URL}/audio/transcriptions",
}


class TransportError(Exception):
    """Base exception for transport layer failures."""
    pass


class LiveNetworkDisabledError(TransportError):
    """Raised when an attempt to use live network occurs while live_network_enabled=False."""
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
    """Manages outbound HTTP requests to OpenRouter with strict fail-closed safeguards."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        live_network_enabled: bool = False,
        timeout_sec: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.live_network_enabled = live_network_enabled
        self.timeout_sec = timeout_sec

    def get_endpoint_for_modality(self, modality_str: str) -> str:
        """Resolve OpenRouter endpoint URL for a given modality."""
        return MODALITY_ENDPOINTS.get(modality_str.lower(), f"{OPENROUTER_BASE_URL}/chat/completions")

    def dispatch(
        self,
        modality_str: str,
        payload: Dict[str, Any],
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> HttpResponse:
        """Dispatch HTTP POST request to OpenRouter endpoint.

        Raises LiveNetworkDisabledError if live_network_enabled is False.
        """
        if not self.live_network_enabled:
            raise LiveNetworkDisabledError(
                "[FAIL-CLOSED] Transporte de rede HTTP real desativado por padrão. "
                "Para executar chamadas de rede reais, habilite explicitamente live_network_enabled=True."
            )

        if not self.api_key:
            raise TransportError(
                "Chave de API da OpenRouter não configurada. Defina a variável de ambiente OPENROUTER_API_KEY."
            )

        url = self.get_endpoint_for_modality(modality_str)
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

            # Check Retry-After header
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
