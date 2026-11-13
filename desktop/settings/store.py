"""Versioned desktop settings with backend validation (Marco 4D.3, Etapa 3; decisions D3 and D4).

- Schema v1 lives in the desktop layer; the Core has no JSON Schema. Where the Core has a validator it is reused
  (FlightBudget.from_dict for mission budgets) and the desktop adds ranges. The Core itself is not modified.
- Unknown, invalid or incompatible input is rejected with SettingsValidationError(field, reason); nothing is saved.
- Every category reports its state: saved fields, validated, applied, and how it is applied:
  "pending_ui" (stored and validated; the interface applies it in a later stage) or "unsupported" (stored and
  validated, but nothing in the desktop applies it yet, e.g. mission budgets before the Missões milestone).
- Secrets never go here: credentials use the profile vault (vault.* IPC), never the plain-text profile settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

SCHEMA_VERSION = 1


class SettingsValidationError(ValueError):
    def __init__(self, message: str, *, field: str, reason: str) -> None:
        super().__init__(message)
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class FieldSpec:
    kind: str  # "bool" | "int" | "enum" | "text" | "flight_budget"
    default: Any
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    choices: Tuple[str, ...] = ()


_BUDGET_LIMITS = {
    "max_steps": (1, 1000),
    "max_elapsed_seconds": (60, 86400),
    "max_uncheckpointed_steps": (1, 100),
    "max_worker_calls": (1, 200),
}


def _default_budget() -> Dict[str, Any]:
    from ai_coordinator.mission import FlightBudget

    return FlightBudget().to_dict()


FIELDS: Dict[str, Dict[str, FieldSpec]] = {
    "perfil": {"display_name": FieldSpec("text", None, maximum=64)},
    "geral": {"open_last_project_on_start": FieldSpec("bool", True)},
    "aparencia": {
        "stimulation": FieldSpec("enum", "standard", choices=("standard", "low")),
        "density": FieldSpec("enum", "comfortable", choices=("comfortable", "compact")),
    },
    "terminal": {
        "font_size": FieldSpec("int", 13, minimum=10, maximum=24),
        "cursor_blink": FieldSpec("bool", True),
        "scrollback_lines": FieldSpec("int", 1000, minimum=100, maximum=1000),
    },
    "coordinator": {"flight_budget": FieldSpec("flight_budget", None)},
}

APPLICATION = {
    "perfil": "pending_ui",
    "geral": "pending_ui",
    "aparencia": "pending_ui",
    "terminal": "pending_ui",
    "coordinator": "unsupported",
}

UNSUPPORTED_CATEGORIES = {
    "agentes": "A descoberta de agentes ainda não é configurável nesta versão.",
    "provedores": "Credenciais usam o cofre do perfil (vault.*), nunca as configurações em texto.",
    "seguranca": "Permissões e diretórios autorizados usam as operações de perfil, não as configurações.",
    "sobre": "Categoria somente leitura.",
}


def _fail(path: str, message: str, reason: str = "invalid_value") -> None:
    raise SettingsValidationError(message, field=path, reason=reason)


def _validate_budget(path: str, value: Any) -> Dict[str, Any]:
    from ai_coordinator.mission import FlightBudget

    if not isinstance(value, dict):
        _fail(path, f"'{path}' deve ser um objeto com os limites da missão.")
    unknown = sorted(set(value) - set(_BUDGET_LIMITS))
    if unknown:
        _fail(f"{path}.{unknown[0]}", f"Campo desconhecido em '{path}': {unknown[0]}.", "unknown_field")
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            _fail(f"{path}.{key}", f"'{path}.{key}' deve ser numérico.")
    try:
        budget = FlightBudget.from_dict(value)  # Core validator: type coercion of the mission budget
    except (TypeError, ValueError) as exc:
        _fail(path, f"Orçamento de missão inválido: {exc}")
    for key, (low, high) in _BUDGET_LIMITS.items():
        current = getattr(budget, key)
        if not low <= current <= high:
            _fail(f"{path}.{key}", f"'{path}.{key}' deve estar entre {low} e {high}.")
    return budget.to_dict()


def _validate_field(path: str, spec: FieldSpec, value: Any) -> Any:
    if spec.kind == "flight_budget":
        return _validate_budget(path, value)
    if spec.kind == "bool":
        if not isinstance(value, bool):
            _fail(path, f"'{path}' deve ser verdadeiro ou falso.")
        return value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(path, f"'{path}' deve ser um número inteiro.")
        if not spec.minimum <= value <= spec.maximum:
            _fail(path, f"'{path}' deve estar entre {spec.minimum} e {spec.maximum}.")
        return value
    if spec.kind == "enum":
        if value not in spec.choices:
            _fail(path, f"'{path}' deve ser um destes valores: {', '.join(spec.choices)}.")
        return value
    if spec.kind == "text":
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > spec.maximum:
            _fail(path, f"'{path}' deve ser um texto de 1 a {spec.maximum} caracteres.")
        return value.strip()
    raise AssertionError(f"unknown field kind {spec.kind}")


class SettingsStore:
    """Reads and writes the validated settings of the active profile (DesktopProfile.settings)."""

    def __init__(self, profile_manager: Any) -> None:
        self.profile_manager = profile_manager

    def _stored(self) -> Tuple[str, Dict[str, Dict[str, Any]], Optional[int]]:
        profile = self.profile_manager.get_active_profile()
        raw = profile.settings if isinstance(profile.settings, dict) else {}
        version = raw.get("schema_version")
        values = raw.get("values") if version == SCHEMA_VERSION and isinstance(raw.get("values"), dict) else {}
        return profile.name, values, version

    def snapshot(self) -> Dict[str, Any]:
        _, stored, stored_version = self._stored()
        categories: Dict[str, Any] = {}
        for category, fields in FIELDS.items():
            saved = stored.get(category, {}) if isinstance(stored.get(category), dict) else {}
            values = {}
            for name, spec in fields.items():
                default = _default_budget() if spec.kind == "flight_budget" else spec.default
                values[name] = saved.get(name, default)
            categories[category] = {
                "values": values,
                "saved_fields": sorted(k for k in saved if k in fields),
                "validated": True,
                "applied": False,
                "application": APPLICATION[category],
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "stored_schema_version": stored_version,
            "categories": categories,
            "unsupported_categories": dict(UNSUPPORTED_CATEGORIES),
        }

    def update(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            _fail("params", "Parâmetros de configuração inválidos.")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            _fail("schema_version", f"Versão de esquema incompatível: esperado {SCHEMA_VERSION}.", "incompatible_version")
        category = payload.get("category")
        if category in UNSUPPORTED_CATEGORIES:
            _fail("category", UNSUPPORTED_CATEGORIES[category], "unsupported_category")
        if category not in FIELDS:
            _fail("category", f"Categoria desconhecida: {category!r}.", "unknown_category")
        values = payload.get("values")
        if not isinstance(values, dict) or not values:
            _fail("values", "'values' deve ser um objeto com ao menos um campo.")

        fields = FIELDS[category]
        validated: Dict[str, Any] = {}
        for name, value in values.items():
            path = f"{category}.{name}"
            if name not in fields:
                _fail(path, f"Campo desconhecido: {path}.", "unknown_field")
            validated[name] = _validate_field(path, fields[name], value)

        # Persist only after every field validated (all-or-nothing).
        profile_name, stored, _ = self._stored()
        merged = {cat: dict(vals) for cat, vals in stored.items() if cat in FIELDS and isinstance(vals, dict)}
        merged.setdefault(category, {}).update(validated)
        self.profile_manager.update_settings(profile_name, {"schema_version": SCHEMA_VERSION, "values": merged})
        return self.snapshot()
