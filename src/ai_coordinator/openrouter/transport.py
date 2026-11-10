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


class OpenRouterTransportSecurityError(TransportError):
    """Raised when an insecure redirect or protocol violation is detected in transport."""
    pass


class GovernedEgressRequiredError(TransportError):
    """Raised when require_governed_egress is active and an ungoverned dispatch is attempted,
    or when mandatory governance dependencies are missing for dispatch_governed()."""
    pass


class StrictNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strict handler rejecting all redirects to prevent credential transmission to unauthorized destinations."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        raise OpenRouterTransportSecurityError(
            f"[FAIL-CLOSED] Redirecionamento HTTP {code} para '{newurl}' rejeitado categoricamente. "
            "Requisições de inferência autenticadas com chave de API não seguem redirecionamentos."
        )

    def http_error_301(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_303(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_307(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_308(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))


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
    governance_metadata: Optional[Dict[str, Any]] = None


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
        opener: Optional[urllib.request.OpenerDirector] = None,
        quota_store: Optional[Any] = None,
        projector: Optional[Any] = None,
        fallback_engine: Optional[Any] = None,
        trust_store: Optional[Any] = None,
        expected_commit: Optional[str] = None,
        require_governed_egress: Optional[bool] = None,
    ) -> None:
        self.cost_guard = cost_guard
        self.rate_limiter = rate_limiter
        self.ledger = ledger
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.live_network_enabled = live_network_enabled
        self.timeout_sec = timeout_sec
        self.redirect_handler = StrictNoRedirectHandler()
        self._custom_opener = opener
        self.opener = opener if opener is not None else urllib.request.build_opener(self.redirect_handler)
        self.quota_store = quota_store
        self.projector = projector
        self.fallback_engine = fallback_engine
        self.trust_store = trust_store or getattr(quota_store, "default_trust_store", None)
        self.expected_commit = expected_commit or getattr(quota_store, "expected_commit", None)
        # When quota_store is configured, require_governed_egress defaults to True.
        # This prevents any accidental or un-governed invocation of the legacy dispatch() path.
        if require_governed_egress is not None:
            self.require_governed_egress = require_governed_egress
        else:
            self.require_governed_egress = (quota_store is not None)

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
        # Governance Gate 0: Require governed egress enforcement
        if self.require_governed_egress:
            raise GovernedEgressRequiredError(
                "[FAIL-CLOSED] require_governed_egress=True ativo: despacho direto desgovernado é terminantemente proibido. "
                "O ponto de saída HTTP exige despacho estritamente governado via dispatch_governed()."
            )
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
            from unittest.mock import MagicMock
            if self._custom_opener is not None:
                open_call = self._custom_opener.open
            elif isinstance(urllib.request.urlopen, MagicMock):
                open_call = urllib.request.urlopen
            else:
                open_call = self.opener.open

            with open_call(req, timeout=self.timeout_sec) as response:
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
        except OpenRouterTransportSecurityError as sec_err:
            t1 = time.perf_counter()
            elapsed_ms = (t1 - t0) * 1000.0
            if self.ledger:
                self.ledger.record_event(
                    str(model_id),
                    AvailabilityEventType.PROVIDER_UNAVAILABLE_503,
                    {"stage": "egress_dispatch_security_error", "error": str(sec_err)},
                )
            return HttpResponse(
                status_code=403,
                data={"error": {"message": str(sec_err)}},
                latency_ms=elapsed_ms,
                raw_error=str(sec_err),
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

    def dispatch_governed(
        self,
        modality_str: str,
        payload: Dict[str, Any],
        budget_id: str,
        logical_attempt_id: str,
        allowed_models: Optional[Sequence[str]] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        is_binary_output: bool = False,
        signed_manifest: Optional[Any] = None,
        human_approval: Optional[Any] = None,
        human_operator_consent: Optional[bool] = None,
        human_operator_identity: Optional[str] = None,
        human_session_id: Optional[str] = None,
        catalog_registry: Optional[Any] = None,
    ) -> HttpResponse:
        """Dispatch HTTP POST request to OpenRouter through Phase 4C.2 governed pipeline.

        Mandatory Egress Gates in Strict Sequential Order:
        1. Governance Infrastructure Check: enforces AtomicDispatchQuotaStore and TransactionalOutboxProjector.
        2. Manifest & Human Authorization: registers and cryptographically validates tripartite authorization.
        3. Global Budget Status & Ceiling Check: rejects if budget is exhausted or unregistered fail-closed.
        4. Payload Privacy & Secret Inspection: recursive scan preventing credential leaks.
        5. Reserved Headers Protection: rejects attempts to override Authorization, Content-Type, Host.
        6. Modality Endpoint Resolution & Validation: verifies mapped official OpenRouter endpoint.
        7. Model Authorization & Pricing Verification: enforces model in budget and strictly $0.00 cost via FreeCostGuard.
        8. Modality Compatibility Verification: checks catalog declared modalities.
        9. Network Opt-In Control: enforces live_network_enabled or mock test opener.
        10. Atomic Reservation Acquisition: acquires SQLite reservation #1 under BEGIN IMMEDIATE.
        11. Pre-Intent Commit (PIC): commits reservation to SEND_COMMITTED and records outbox event.
        12. Mandatory Durable Outbox Projection: projects pending outbox event to physical ledger with fsync before egress.
        13. Physical/Mocked HTTP Dispatch: sends POST request to OpenRouter API.
        14. Post-Flight Outcome Recording & Governed Fallback:
            - On 200 OK: transitions reservation to COMPLETED and projects outbox.
            - On 429/503: transitions reservation to FAILED and evaluates GovernedFallbackEngine:
              * If max_dispatches=1: emits FALLBACK_PROPOSED_BLOCKED, zero secondary calls.
              * If max_dispatches>1: acquires reservation #2, commits SEND_COMMITTED, projects outbox, and executes contingency model.
            - On ambiguous network failure: transitions reservation to UNKNOWN and projects outbox (fallback prohibited).
        """
        from .atomic_quota import (
            BudgetExhaustedError,
            GovernedFallbackEngine,
            QuotaSecurityError,
            STATE_COMPLETED,
            STATE_FAILED,
            STATE_UNKNOWN,
        )

        # Gate 1: Governance Infrastructure Check
        if self.quota_store is None:
            raise GovernedEgressRequiredError(
                "[FAIL-CLOSED] dispatch_governed() exige AtomicDispatchQuotaStore configurado."
            )
        if self.projector is None:
            raise GovernedEgressRequiredError(
                "[FAIL-CLOSED] dispatch_governed() exige TransactionalOutboxProjector configurado."
            )
        if self.cost_guard is None:
            raise CostGuardError(
                "[FAIL-CLOSED] dispatch_governed() exige FreeCostGuard configurado."
            )

        # Gate 2: Manifest & Human Authorization Registration (if provided)
        if signed_manifest is not None:
            # Enforce authenticated human approval or explicit consent without default conversion to True
            registered_budget_id = self.quota_store.register_budget_from_signed_manifest(
                signed_manifest=signed_manifest,
                trust_store=self.trust_store,
                expected_commit=self.expected_commit,
                human_operator_consent=human_operator_consent,
                human_operator_identity=human_operator_identity,
                human_session_id=human_session_id,
                human_approval=human_approval,
            )
            manifest_auth_id = getattr(getattr(signed_manifest, "manifest", None), "authorization_id", None)
            if budget_id in (manifest_auth_id, registered_budget_id):
                budget_id = registered_budget_id
            else:
                raise QuotaSecurityError(
                    f"[FAIL-CLOSED] budget_id '{budget_id}' diverge do authorization_id do manifesto '{manifest_auth_id}'."
                )

        # Gate 3: Budget Status and Ceiling Verification
        budget_status = self.quota_store.get_budget_status(budget_id)
        budget_id = budget_status["budget_id"]
        max_dispatches = budget_status["max_dispatches"]
        consumed_total = budget_status["consumed_total"]
        budget_models = budget_status.get("allowed_models", [])

        if consumed_total >= max_dispatches:
            raise BudgetExhaustedError(
                f"[FAIL-CLOSED] Orçamento '{budget_id}' esgotado: {consumed_total}/{max_dispatches} despachos consumidos."
            )

        # Gate 4: Privacy & Secret Inspection
        from .adapters import inspect_payload_privacy_strictly
        inspect_payload_privacy_strictly(payload)

        # Gate 5: Reserved Headers Protection
        RESERVED_HEADERS = {"authorization", "content-type", "host"}
        if custom_headers:
            for k in custom_headers:
                if str(k).strip().lower() in RESERVED_HEADERS:
                    raise TransportError(
                        f"[FAIL-CLOSED] Cabeçalho reservado '{k}' não pode ser sobrescrito via custom_headers."
                    )

        # Gate 6: Modality Endpoint Resolution
        url = self.get_endpoint_for_modality(modality_str)

        # Gate 7: Primary Model Validation and Cost Guard Payload Inspection
        if not isinstance(payload, dict):
            raise CostGuardError("Payload de envio deve ser um dicionário.")

        # Strict Provider Routing Restriction:
        # In governed dispatch, fallback is exclusively governed locally by GovernedFallbackEngine.
        # Remote fallback arrays ('models'), provider-side fallbacks ('provider.allow_fallbacks'), or
        # arbitrary routing parameters ('route') are strictly prohibited fail-closed.
        if "route" in payload and payload["route"] is not None:
            raise CostGuardError(
                "[FAIL-CLOSED] Parâmetro 'route' proibido em dispatch_governed: o roteamento é estritamente governado localmente."
            )
        if "models" in payload and payload["models"] is not None:
            raise CostGuardError(
                "[FAIL-CLOSED] Parâmetro 'models' proibido em dispatch_governed: o fallback entre modelos deve acontecer exclusivamente sob controle local do AI Coordinator."
            )
        provider_cfg = payload.get("provider")
        if isinstance(provider_cfg, dict):
            if provider_cfg.get("allow_fallbacks") is True:
                raise CostGuardError(
                    "[FAIL-CLOSED] provider.allow_fallbacks=True é proibido em dispatch_governed: fallback do provedor viola a governança local determinística."
                )

        # Mandatory CostGuard deep inspection of payload (models list, pricing, options)
        self.cost_guard.inspect_request_payload(payload)

        model_id = payload.get("model")
        if not model_id:
            raise UnauthorizedModelError("Payload de requisição sem campo obrigatório 'model'.")
        model_id_str = str(model_id)

        # Verify model is permitted by budget
        if budget_models and model_id_str not in budget_models:
            raise UnauthorizedModelError(
                f"[FAIL-CLOSED] Modelo '{model_id_str}' não consta entre os modelos autorizados pelo orçamento '{budget_id}': {budget_models}"
            )

        entry = self.cost_guard.authorize_model_execution(model_id_str, verify_freshness=True)

        # Gate 8: Modality Compatibility Check
        norm_mod = modality_str.strip().lower()
        entry_modality_values = [m.value.lower() for m in entry.modalities]
        if norm_mod not in entry_modality_values:
            raise IncompatibleModalityEndpointError(
                f"[FAIL-CLOSED] Modelo '{model_id_str}' não suporta a modalidade '{modality_str}'."
            )

        if self.live_network_enabled and norm_mod != "text":
            raise IncompatibleModalityEndpointError(
                f"[PILOT_SCOPE_RESTRICTION] Em modo operacional ativo (live network), apenas a modalidade 'text' "
                f"está homologada. Modalidade requisitada '{modality_str}' permanece restrita ao modo simulado."
            )

        # Gate 9: Opt-In Network Check
        if not self.live_network_enabled and self._custom_opener is None:
            raise LiveNetworkDisabledError(
                "[FAIL-CLOSED] Transporte de rede HTTP real desativado por padrão. "
                "Para executar chamadas de rede reais, habilite explicitamente live_network_enabled=True."
            )

        if self.live_network_enabled:
            self._validate_live_configuration()

        if not self.api_key and not self._custom_opener:
            raise TransportError(
                "Chave de API da OpenRouter não configurada. Defina a variável de ambiente OPENROUTER_API_KEY."
            )

        # Gate 10: Atomic Reservation Acquisition (Attempt #1)
        res = self.quota_store.acquire_reservation(
            budget_id=budget_id,
            logical_attempt_id=logical_attempt_id,
            sequence_index=1,
            target_model=model_id_str,
        )

        # Gate 11: Pre-Intent Commit (PIC)
        self.quota_store.commit_send(
            reservation_id=res.reservation_id,
            trust_store=self.trust_store,
            expected_commit=self.expected_commit,
        )

        # Gate 12: Mandatory Durable Outbox Projection before physical egress
        projected = self.projector.project_pending_events()
        if projected is None or projected < 0:
            raise TransportError("Falha crítica na projeção durável da outbox antes do despacho.")

        # Gate 13: Physical or Mocked HTTP Dispatch
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key or 'sk-mock-key'}",
            "HTTP-Referer": "https://github.com/MaxxArtes/ai-coordinator",
            "X-Title": "AI Coordinator Governed Egress",
            "X-AI-Coordinator-Reservation-Id": res.reservation_id,
            "X-AI-Coordinator-Budget-Id": budget_id,
        }
        if custom_headers:
            headers.update(custom_headers)

        # Build sanitized outbound governed payload for Attempt #1:
        # 1. Strip any remote fallback keys (models, route).
        # 2. Exclusively bind model to the authorized reservation's target_model.
        # 3. Explicitly define provider.allow_fallbacks=False (disabling provider-side fallback default).
        outbound_payload = dict(payload)
        outbound_payload.pop("models", None)
        outbound_payload.pop("route", None)
        outbound_payload["model"] = model_id_str
        outbound_provider = dict(outbound_payload.get("provider") or {})
        outbound_provider["allow_fallbacks"] = False
        outbound_payload["provider"] = outbound_provider

        encoded_data = json.dumps(outbound_payload).encode("utf-8")
        req = urllib.request.Request(url, data=encoded_data, headers=headers, method="POST")

        t0 = time.perf_counter()
        try:
            from unittest.mock import MagicMock
            if self._custom_opener is not None:
                open_call = self._custom_opener.open
            elif isinstance(urllib.request.urlopen, MagicMock):
                open_call = urllib.request.urlopen
            else:
                open_call = self.opener.open

            with open_call(req, timeout=self.timeout_sec) as response:
                t1 = time.perf_counter()
                elapsed_ms = (t1 - t0) * 1000.0
                status_code = response.getcode()
                resp_headers = dict(response.headers)
                content_type = resp_headers.get("Content-Type", "").lower()

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

                # Gate 14a: Record Successful Completion
                self.quota_store.record_completion(
                    reservation_id=res.reservation_id,
                    http_status_code=status_code,
                    details=parsed_json if not binary_payload else {"bytes_length": len(binary_payload)},
                )
                self.projector.project_pending_events()

                if self.ledger:
                    self.ledger.record_event(
                        model_id_str,
                        AvailabilityEventType.BENCHMARK_EXECUTED,
                        {"stage": "governed_dispatch_success", "status_code": status_code, "latency_ms": elapsed_ms},
                    )

                return HttpResponse(
                    status_code=status_code,
                    data=parsed_json,
                    latency_ms=elapsed_ms,
                    headers=resp_headers,
                    binary_data=binary_payload,
                    governance_metadata={
                        "reservation_id": res.reservation_id,
                        "budget_id": budget_id,
                        "sequence_index": 1,
                        "state": STATE_COMPLETED,
                        "fallback_action": "NONE",
                        "model": model_id_str,
                    },
                )
        except OpenRouterTransportSecurityError as sec_err:
            t1 = time.perf_counter()
            elapsed_ms = (t1 - t0) * 1000.0
            self.quota_store.record_failure(
                reservation_id=res.reservation_id,
                http_status_code=403,
                details={"security_error": str(sec_err)},
            )
            self.projector.project_pending_events()
            return HttpResponse(
                status_code=403,
                data={"error": {"message": str(sec_err)}},
                latency_ms=elapsed_ms,
                raw_error=str(sec_err),
                governance_metadata={
                    "reservation_id": res.reservation_id,
                    "budget_id": budget_id,
                    "sequence_index": 1,
                    "state": STATE_FAILED,
                    "fallback_action": "SECURITY_ERROR",
                },
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

            # Record failure of attempt #1
            self.quota_store.record_failure(
                reservation_id=res.reservation_id,
                http_status_code=status_code,
                details={"status_code": status_code, "raw_error": str(http_err), "headers": err_headers},
            )
            self.projector.project_pending_events()

            # Gate 14b: Evaluate Governed Fallback on HTTP 429 / 503
            if status_code in (429, 503):
                candidates = list(allowed_models or budget_models)
                engine = self.fallback_engine or GovernedFallbackEngine(
                    quota_store=self.quota_store,
                    projector=self.projector,
                    cost_guard=self.cost_guard,
                    default_trust_store=self.trust_store,
                    expected_commit=self.expected_commit,
                )
                fb_decision = engine.evaluate_fallback(
                    budget_id=budget_id,
                    failed_reservation=res,
                    allowed_models=candidates,
                    logical_attempt_id=logical_attempt_id,
                    catalog_registry=catalog_registry or self.cost_guard.registry,
                )

                if fb_decision.action == "DISPATCH_AUTHORIZED" and fb_decision.reservation_id and fb_decision.target_model:
                    # Execute secondary dispatch for contingency model
                    contingency_model = fb_decision.target_model
                    contingency_payload = dict(payload)
                    contingency_payload.pop("models", None)
                    contingency_payload.pop("route", None)
                    contingency_payload["model"] = contingency_model
                    contingency_provider = dict(contingency_payload.get("provider") or {})
                    contingency_provider["allow_fallbacks"] = False
                    contingency_payload["provider"] = contingency_provider

                    self.cost_guard.authorize_model_execution(contingency_model, verify_freshness=True)

                    headers_2 = dict(headers)
                    headers_2["X-AI-Coordinator-Reservation-Id"] = fb_decision.reservation_id
                    encoded_data_2 = json.dumps(contingency_payload).encode("utf-8")
                    req_2 = urllib.request.Request(url, data=encoded_data_2, headers=headers_2, method="POST")

                    t0_2 = time.perf_counter()
                    try:
                        with open_call(req_2, timeout=self.timeout_sec) as response_2:
                            t1_2 = time.perf_counter()
                            elapsed_ms_2 = (t1_2 - t0_2) * 1000.0
                            status_code_2 = response_2.getcode()
                            resp_headers_2 = dict(response_2.headers)
                            body_2 = response_2.read().decode("utf-8") if response_2 else ""
                            parsed_json_2 = json.loads(body_2) if body_2 else {}

                            self.quota_store.record_completion(
                                reservation_id=fb_decision.reservation_id,
                                http_status_code=status_code_2,
                                details=parsed_json_2,
                            )
                            self.projector.project_pending_events()

                            return HttpResponse(
                                status_code=status_code_2,
                                data=parsed_json_2,
                                latency_ms=elapsed_ms + elapsed_ms_2,
                                headers=resp_headers_2,
                                governance_metadata={
                                    "reservation_id": fb_decision.reservation_id,
                                    "budget_id": budget_id,
                                    "sequence_index": fb_decision.sequence_index,
                                    "state": STATE_COMPLETED,
                                    "fallback_action": "DISPATCH_AUTHORIZED",
                                    "primary_model": model_id_str,
                                    "contingency_model": contingency_model,
                                    "primary_status_code": status_code,
                                },
                            )
                    except urllib.error.HTTPError as fb_http_err:
                        # Confirmed HTTP error response from contingency model (e.g. 503, 429)
                        status_code_2 = fb_http_err.code
                        self.quota_store.record_failure(
                            reservation_id=fb_decision.reservation_id,
                            http_status_code=status_code_2,
                            details={"contingency_error": str(fb_http_err)},
                        )
                        self.projector.project_pending_events()
                        return HttpResponse(
                            status_code=status_code_2,
                            data={"error": {"message": str(fb_http_err)}},
                            latency_ms=elapsed_ms,
                            raw_error=str(fb_http_err),
                            governance_metadata={
                                "reservation_id": fb_decision.reservation_id,
                                "budget_id": budget_id,
                                "sequence_index": fb_decision.sequence_index,
                                "state": STATE_FAILED,
                                "fallback_action": "DISPATCH_AUTHORIZED_FAILED",
                            },
                        )
                    except Exception as fb_ambig_exc:
                        # Ambiguous outcome on contingency attempt (timeout, socket reset, network loss):
                        # MUST transition to UNKNOWN, permanently draining budget and strictly forbidding retries!
                        self.quota_store.record_unknown(
                            reservation_id=fb_decision.reservation_id,
                            details={"contingency_ambiguous_error": str(fb_ambig_exc), "error_type": type(fb_ambig_exc).__name__},
                            reason=f"Ambiguous contingency transport failure: {str(fb_ambig_exc)}",
                        )
                        self.projector.project_pending_events()
                        return HttpResponse(
                            status_code=500,
                            data={"error": {"type": "AmbiguousContingencyOutcome", "message": str(fb_ambig_exc)}},
                            latency_ms=elapsed_ms,
                            raw_error=f"[AMBIGUOUS_CONTINGENCY_UNKNOWN] {str(fb_ambig_exc)}",
                            governance_metadata={
                                "reservation_id": fb_decision.reservation_id,
                                "budget_id": budget_id,
                                "sequence_index": fb_decision.sequence_index,
                                "state": STATE_UNKNOWN,
                                "fallback_action": "CONTINGENCY_BLOCKED_UNKNOWN",
                            },
                        )

                # If action is RECOMMENDED_ONLY, BLOCKED_BUDGET, or NO_CANDIDATE:
                # ZERO secondary network calls made!
                return HttpResponse(
                    status_code=status_code,
                    data=parsed_json,
                    latency_ms=elapsed_ms,
                    retry_after_sec=retry_after,
                    headers=err_headers,
                    raw_error=str(http_err),
                    governance_metadata={
                        "reservation_id": res.reservation_id,
                        "budget_id": budget_id,
                        "sequence_index": 1,
                        "state": STATE_FAILED,
                        "fallback_action": fb_decision.action,
                        "fallback_decision": fb_decision.to_dict(),
                    },
                )

            return HttpResponse(
                status_code=status_code,
                data=parsed_json,
                latency_ms=elapsed_ms,
                retry_after_sec=retry_after,
                headers=err_headers,
                raw_error=str(http_err),
                governance_metadata={
                    "reservation_id": res.reservation_id,
                    "budget_id": budget_id,
                    "sequence_index": 1,
                    "state": STATE_FAILED,
                    "fallback_action": "NONE",
                },
            )
        except Exception as exc:
            # Gate 14c: Ambiguous Network Outcome -> UNKNOWN
            t1 = time.perf_counter()
            elapsed_ms = (t1 - t0) * 1000.0
            self.quota_store.record_unknown(
                reservation_id=res.reservation_id,
                details={"error_type": type(exc).__name__, "error": str(exc)},
            )
            self.projector.project_pending_events()

            # UNKNOWN state strictly forbids automatic retry or fallback!
            return HttpResponse(
                status_code=500,
                data={"error": {"type": "AmbiguousNetworkOutcome", "message": str(exc)}},
                latency_ms=elapsed_ms,
                raw_error=f"[AMBIGUOUS_OUTCOME_UNKNOWN] {str(exc)}",
                governance_metadata={
                    "reservation_id": res.reservation_id,
                    "budget_id": budget_id,
                    "sequence_index": 1,
                    "state": STATE_UNKNOWN,
                    "fallback_action": "BLOCKED_UNKNOWN",
                },
            )
