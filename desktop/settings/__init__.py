"""Desktop settings: versioned schema, backend validation and persistence (Marco 4D.3, Etapa 3)."""

from .store import SCHEMA_VERSION, SettingsStore, SettingsValidationError

__all__ = ["SCHEMA_VERSION", "SettingsStore", "SettingsValidationError"]
