"""Strict fail-closed zero-cost guard for OpenRouter integration (Phase 4C.1).

Guarantees that:
1. No paid models or non-zero pricing endpoints can ever be called.
2. If free eligibility cannot be verified with 100% certainty, execution fails closed.
3. Fallbacks from free variants to paid variants are permanently prohibited.
4. Ambiguous routes, unverified fallback chains, and unapproved routing parameters are rejected.
5. Models must be backed by fresh catalog data within valid TTL.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from .registry import FreeModelRegistry, ModelCatalogEntry, PricingStatus, StaleCatalogError

logger = logging.getLogger(__name__)


class CostGuardError(Exception):
    """Base exception for cost guard violations."""
    pass


class UnauthorizedModelError(CostGuardError):
    """Raised when a requested model is not authorized as strictly free."""
    pass


class PaidFallbackBlockedError(CostGuardError):
    """Raised when a fallback mechanism attempts to switch to a non-free model."""
    pass


class AmbiguousRoutingError(CostGuardError):
    """Raised when request payload specifies unverified or ambiguous routing parameters."""
    pass


class FreeCostGuard:
    """Fail-closed budget fence enforcing zero financial expenditure."""

    def __init__(
        self,
        registry: FreeModelRegistry,
        allowed_explicit_models: Optional[Set[str]] = None,
        enforce_explicit_free_suffix: bool = False,
    ) -> None:
        self.registry = registry
        self.allowed_explicit_models = set(allowed_explicit_models or [])
        self.enforce_explicit_free_suffix = enforce_explicit_free_suffix
        self._blocked_attempts_count = 0
        self._authorized_calls_count = 0

    @property
    def blocked_attempts(self) -> int:
        return self._blocked_attempts_count

    @property
    def authorized_calls(self) -> int:
        return self._authorized_calls_count

    def authorize_model_execution(self, model_id: str, verify_freshness: bool = True) -> ModelCatalogEntry:
        """Verify that model_id is cataloged, fresh, strictly free, and authorized.

        Raises:
            UnauthorizedModelError: If model is missing from catalog, paid, or ambiguous.
            PaidFallbackBlockedError: If model looks like a paid fallback target.
            StaleCatalogError: If catalog data has expired and cannot be refreshed.
        """
        if not model_id or not isinstance(model_id, str):
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError("Identificador de modelo nulo ou inválido fornecido.")

        clean_id = model_id.strip()

        # Check explicit allowlist if configured
        if self.allowed_explicit_models and clean_id not in self.allowed_explicit_models:
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"Modelo '{clean_id}' não consta na lista de modelos explicitamente autorizados para o experimento."
            )

        # Enforce suffix if configured
        if self.enforce_explicit_free_suffix and not clean_id.endswith(":free"):
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"Modelo '{clean_id}' rejeitado: a política exige sufixo explícito ':free'."
            )

        # Query registry with freshness check
        try:
            entry = self.registry.get_model(clean_id, verify_freshness=verify_freshness)
        except StaleCatalogError as stale_err:
            self._blocked_attempts_count += 1
            raise stale_err

        if not entry:
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"[FAIL-CLOSED] Modelo '{clean_id}' não foi encontrado no catálogo verificado da OpenRouter. "
                "Execução bloqueada preventivamente."
            )

        # Verify pricing integrity status
        if entry.pricing.status != PricingStatus.VALID_ZERO:
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"[FAIL-CLOSED] Modelo '{clean_id}' possui precificação não-nula ou inválida "
                f"(Status: {entry.pricing.status.value}, Prompt: ${entry.pricing.prompt}, "
                f"Completion: ${entry.pricing.completion}, Request: ${entry.pricing.request}). "
                "Chamadas com qualquer componente faturável são terminantemente proibidas."
            )

        # Verify strict zero cost in all dimensions
        if not entry.is_free or not entry.pricing.is_strictly_zero:
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"[FAIL-CLOSED] Ambiguidade na precificação do modelo '{clean_id}'. "
                "Rejeitado por política de segurança estrita de custo zero."
            )

        self._authorized_calls_count += 1
        return entry

    def sanitize_fallback_targets(self, targets: List[str]) -> List[str]:
        """Validate and filter a list of fallback candidate models.

        Raises PaidFallbackBlockedError if any candidate in the fallback chain is paid or invalid.
        """
        authorized_targets = []
        for target in targets:
            try:
                self.authorize_model_execution(target)
                authorized_targets.append(target)
            except (UnauthorizedModelError, CostGuardError) as exc:
                raise PaidFallbackBlockedError(
                    f"Tentativa de fallback bloqueada: candidato '{target}' não é elegível como estritamente gratuito: {exc}"
                ) from exc
        return authorized_targets

    def inspect_request_payload(self, payload: Dict[str, Any]) -> None:
        """Inspect HTTP payload dictionary immediately before dispatch, ensuring zero cost.

        Raises:
            UnauthorizedModelError: If primary model is missing, invalid, or paid.
            PaidFallbackBlockedError: If fallback chain contains non-free models.
            AmbiguousRoutingError: If unrecognized or non-deterministic routing options exist.
        """
        if not isinstance(payload, dict):
            raise CostGuardError("Payload de requisição deve ser um dicionário.")

        model_id = payload.get("model")
        if not model_id:
            raise UnauthorizedModelError("Payload de requisição sem campo obrigatório 'model'.")
        self.authorize_model_execution(str(model_id))

        # Check fallback candidates array (models)
        models_list = payload.get("models")
        if models_list is not None:
            if not isinstance(models_list, list) or not models_list:
                raise AmbiguousRoutingError("Parâmetro 'models' deve ser uma lista não-vazia de identificadores.")
            for m in models_list:
                try:
                    self.authorize_model_execution(str(m))
                except UnauthorizedModelError as exc:
                    raise PaidFallbackBlockedError(
                        f"Fallback proibido: candidato '{m}' na lista 'models' não é estritamente gratuito: {exc}"
                    ) from exc

        # Check routing parameters (route)
        route_param = payload.get("route")
        if route_param is not None:
            route_str = str(route_param).strip().lower()
            if route_str == "fallback":
                if not models_list:
                    raise AmbiguousRoutingError(
                        "Parâmetro route='fallback' exige definição explícita do array 'models' com candidatos gratuitos."
                    )
            elif route_str not in ("", "none"):
                raise AmbiguousRoutingError(
                    f"Parâmetro de roteamento não reconhecido ou ambíguo: '{route_param}'. "
                    "Apenas None ou route='fallback' com 'models' estritamente gratuitos são permitidos."
                )

        # Check provider routing options (provider)
        provider_opts = payload.get("provider")
        if isinstance(provider_opts, dict):
            if provider_opts.get("allow_fallbacks") is True and not models_list:
                raise AmbiguousRoutingError(
                    "provider.allow_fallbacks=True sem restrição de lista 'models' verificada pode desviar para modelos pagos."
                )
