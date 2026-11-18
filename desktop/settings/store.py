"""Versioned desktop settings with backend validation (Marco 4D.3, Etapa 3; decisions D3 and D4).

- Schema v1 lives in the desktop layer; the Core has no JSON Schema. Where the Core has a validator it is reused
  (FlightBudget.from_dict for mission budgets) and the desktop adds ranges. The Core itself is not modified.
- Unknown, invalid or incompatible input is rejected with SettingsValidationError(field, reason); nothing is saved.
- Stored data never makes reading fail (E2-6): future or malformed envelopes are read-only, and invalid stored
  fields are reported and kept until the operator writes them.
- Every category reports its state: saved fields, validated, applied, and how it is applied:
  "interface" (stored and validated; the running interface applies supported preferences) or "unsupported" (stored and
  validated, but nothing in the desktop applies it yet, e.g. mission budgets before the Missões milestone).
- Secrets never go here: credentials use the profile vault (vault.* IPC), never the plain-text profile settings.
"""

from __future__ import annotations

import copy

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
    "perfil": "interface",
    "geral": "interface",
    "aparencia": "interface",
    "terminal": "interface",
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


STORED_ISSUE_MESSAGES = {
    "incompatible_version": "As configurações foram gravadas por outra versão e estão protegidas. Os padrões estão em uso "
                            "e nada será alterado.",
    "invalid_envelope": "As configurações salvas não estão no formato esperado e foram protegidas. Os padrões estão em "
                        "uso e nada será alterado.",
    "invalid_fields": "Alguns valores salvos são inválidos. O padrão é usado para eles, e o valor salvo é preservado até "
                      "você gravar o campo.",
}
READ_ONLY_MESSAGE = "As configurações foram gravadas por outra versão e estão protegidas. Nada foi alterado."
PRE_V1_BACKUP_SUFFIX = "pre-v1.bak"


@dataclass
class _StoredState:
    """What the stored envelope allows: the migrated envelope to write (None when read-only) and values to show."""

    envelope: Optional[Dict[str, Any]]
    values: Dict[str, Any]
    version: Optional[int]
    migration_required: bool
    read_only_reason: Optional[str]  # None | "incompatible_version" | "invalid_stored_value"


class SettingsStore:
    """Version 1 with lossless legacy migration and read-only protection for future or malformed envelopes.

    Reading never fails because of stored data: a future schema or a malformed envelope is reported read-only, and an
    invalid stored field is reported in `invalid_fields` with its default shown, while its stored value is kept until
    the operator writes that field.
    """

    def __init__(self, profile_manager: Any) -> None:
        self.profile_manager = profile_manager

    @staticmethod
    def _inspect(raw: Any) -> _StoredState:
        if not isinstance(raw, dict):
            return _StoredState(None, {}, None, False, "invalid_stored_value")
        version = raw.get("schema_version")
        if version is not None and (type(version) is not int or version not in (0, SCHEMA_VERSION)):
            shown = raw.get("values") if isinstance(raw.get("values"), dict) else {}
            return _StoredState(None, copy.deepcopy(shown), version if type(version) is int else None, False,
                                "incompatible_version")
        envelope = copy.deepcopy(raw)
        if "values" in raw:
            if not isinstance(raw["values"], dict):
                return _StoredState(None, {}, version, False, "invalid_stored_value")
            stored = copy.deepcopy(raw["values"])
        elif version == SCHEMA_VERSION:
            # Version 1 without its values object: written by something else; protect it instead of guessing.
            return _StoredState(None, {}, version, False, "invalid_stored_value")
        else:
            # Legacy flat known categories are moved into the envelope; unrelated extensions stay at the root.
            stored = {name: envelope.pop(name) for name in FIELDS if name in envelope}
        if any(name in stored and not isinstance(stored[name], dict) for name in FIELDS):
            return _StoredState(None, stored, version, False, "invalid_stored_value")
        envelope["schema_version"] = SCHEMA_VERSION
        envelope["values"] = stored
        return _StoredState(envelope, stored, version, bool(raw) and version != SCHEMA_VERSION, None)

    @staticmethod
    def _category_view(category: str, saved: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        values, invalid = {}, []
        for name, spec in FIELDS[category].items():
            default = _default_budget() if spec.kind == "flight_budget" else spec.default
            if name not in saved:
                values[name] = default
                continue
            if spec.kind == "text" and saved[name] is None and spec.default is None:
                values[name] = None
                continue
            stored = saved[name]
            if spec.kind == "flight_budget" and isinstance(stored, dict):
                # Future budget extensions survive storage; only recognized fields reach this UI/Core contract.
                stored = {key: value for key, value in stored.items() if key in _BUDGET_LIMITS}
            try:
                values[name] = _validate_field(f"{category}.{name}", spec, stored)
            except SettingsValidationError:
                values[name] = default
                invalid.append(name)
        return values, invalid

    def snapshot(self) -> Dict[str, Any]:
        profile = self.profile_manager.get_active_profile()
        state = self._inspect(profile.settings)
        read_only = state.read_only_reason is not None
        categories = {}
        any_invalid = False
        for category, fields in FIELDS.items():
            saved = state.values.get(category, {})
            if not isinstance(saved, dict):
                saved = {}
            values, invalid = self._category_view(category, saved)
            any_invalid = any_invalid or bool(invalid)
            categories[category] = {
                "values": values,
                "saved_fields": sorted(name for name in saved if name in fields),
                "invalid_fields": invalid,
                "validated": not invalid and not read_only,
                "applied": False,  # Only the interface can confirm application to mounted components.
                "application": APPLICATION[category],
            }
        if state.read_only_reason == "incompatible_version":
            issue = {"reason": "incompatible_version", "message": STORED_ISSUE_MESSAGES["incompatible_version"]}
        elif read_only:
            issue = {"reason": "invalid_stored_value", "message": STORED_ISSUE_MESSAGES["invalid_envelope"]}
        elif any_invalid:
            issue = {"reason": "invalid_stored_value", "message": STORED_ISSUE_MESSAGES["invalid_fields"]}
        else:
            issue = None
        return {
            "schema_version": SCHEMA_VERSION,
            "stored_schema_version": state.version,
            "read_only": read_only,
            "stored_issue": issue,
            "migration": {"required": state.migration_required, "from_version": state.version, "to_version": SCHEMA_VERSION},
            "categories": categories,
            "unsupported_categories": dict(UNSUPPORTED_CATEGORIES),
        }

    def update(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            _fail("params", "Parâmetros de configuração inválidos.")
        version = payload.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            _fail("schema_version", f"Versão de esquema incompatível: esperado {SCHEMA_VERSION}.", "incompatible_version")
        category = payload.get("category")
        if not isinstance(category, str):
            _fail("category", "Categoria inválida.", "unknown_category")
        if category in UNSUPPORTED_CATEGORIES:
            _fail("category", UNSUPPORTED_CATEGORIES[category], "unsupported_category")
        if category not in FIELDS:
            _fail("category", "Categoria desconhecida.", "unknown_category")
        values = payload.get("values")
        if not isinstance(values, dict) or not values:
            _fail("values", "'values' deve ser um objeto com ao menos um campo.")
        fields = FIELDS[category]
        validated = {}
        for name, value in values.items():
            if name not in fields:
                _fail(f"{category}.{name}", "Campo desconhecido.", "unknown_field")
            validated[name] = _validate_field(f"{category}.{name}", fields[name], value)

        profile_name = self.profile_manager.get_active_profile().name
        def merge(current):
            state = self._inspect(current)
            if state.envelope is None:
                _fail("settings", READ_ONLY_MESSAGE, "read_only_stored_schema")
            if state.migration_required:
                # Before the first write that migrates legacy (v0 or flat) settings, keep the old file once.
                self.profile_manager.backup_profile_file(profile_name, PRE_V1_BACKUP_SUFFIX)
            envelope = state.envelope
            saved = envelope["values"].setdefault(category, {})
            # Only the fields being written are replaced; other stored fields, valid or not, stay exactly as they are.
            for name, value in validated.items():
                if fields[name].kind == "flight_budget" and isinstance(saved.get(name), dict):
                    saved[name] = {**saved[name], **value}
                else:
                    saved[name] = value
            return envelope

        self.profile_manager.mutate_settings(profile_name, merge)
        return self.snapshot()
