"""AI Coordinator Desktop — Updater Package."""
from .controller import (
    DesktopUpdaterController,
    PlatformUpdate,
    UpdateCheckResult,
    UpdateManifest,
    compare_versions,
    parse_version,
)

__all__ = [
    "DesktopUpdaterController",
    "PlatformUpdate",
    "UpdateCheckResult",
    "UpdateManifest",
    "compare_versions",
    "parse_version",
]
