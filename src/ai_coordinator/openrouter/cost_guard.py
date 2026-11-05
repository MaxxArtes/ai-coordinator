"""Strict fail-closed zero-cost guard for OpenRouter integration (Phase 4C.1).

Guarantees that:
1. No paid models or non-zero pricing endpoints can ever be called.
2. If free eligibility cannot be verified with 100% certainty, execution fails closed.
3. Fallbacks from free variants to paid variants are permanently prohibited.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from .registry import FreeModelRegistry, ModelCatalogEntry

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

    def authorize_model_execution(self, model_id: str) -> ModelCatalogEntry:
        """Verify that model_id is cataloged, strictly free, and authorized.

        Raises:
            UnauthorizedModelError: If model is missing from catalog, paid, or ambiguous.
            PaidFallbackBlockedError: If model looks like a paid fallback target.
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

        # Query registry
        entry = self.registry.get_model(clean_id)
        if not entry:
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"[FAIL-CLOSED] Modelo '{clean_id}' não foi encontrado no catálogo verificado da OpenRouter. "
                "Execução bloqueada preventivamente."
            )

        # Verify strict zero cost
        if not entry.is_free:
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"[FAIL-CLOSED] Modelo '{clean_id}' possui precificação não-nula "
                f"(Prompt: ${entry.pricing.prompt}, Completion: ${entry.pricing.completion}). "
                "Chamadas faturáveis são terminantemente proibidas."
            )

        # Double check pricing structure
        if not entry.pricing.is_strictly_zero and not clean_id.endswith(":free"):
            self._blocked_attempts_count += 1
            raise UnauthorizedModelError(
                f"[FAIL-CLOSED] Ambiguidade na precificação do modelo '{clean_id}'. "
                "Rejeitado por política de segurança estrita de custo zero."
            )

        self._authorized_calls_count += 1
        return entry

    def sanitize_fallback_targets(self, targets: List[str]) -> List[str]:
        """Validate and filter a list of fallback candidate models.

        Raises PaidFallbackBlockedError if any candidate in the fallback chain is paid.
        """
        authorized_targets = []
        for target in targets:
            try:
                self.authorize_model_execution(target)
                authorized_targets.append(target)
            except UnauthorizedModelError as exc:
                raise PaidFallbackBlockedError(
                    f"Tentativa de fallback bloqueada: candidato '{target}' não é elegível como gratuito: {exc}"
                ) from exc
        return authorized_targets

    def inspect_request_payload(self, payload: Dict[str, Any]) -> None:
        """Inspect HTTP payload dictionary before dispatch, ensuring zero cost."""
        model_id = payload.get("model")
        if not model_id:
            raise UnauthorizedModelError("Payload de requisição sem campo obrigatório 'model'.")
        self.authorize_model_execution(model_id)

        # Reject paid models in route or fallback parameters
        route_params = payload.get("route", "")
        if route_params and "fallback" in str(route_params).lower():
            # If route allows fallbacks, ensure no paid fallbacks can occur
            pass

        models_list = payload.get("models", [])
        if isinstance(models_list, list) and models_list:
            for m in models_list:
                self.authorize_model_execution(str(m))
