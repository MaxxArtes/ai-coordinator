"""Initialize portable, per-user coordinator data without replacing user files."""
from __future__ import annotations

import os
from pathlib import Path
import shutil


PACKAGE_DIR = Path(__file__).resolve().parent


def default_data_dir() -> Path:
    configured = os.environ.get("AI_COORDINATOR_HOME")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ai-coordinator"


def initialize_data_dir(root: str | os.PathLike[str] | None = None) -> Path:
    """Create the data directory and seed missing defaults exactly once."""
    directory = Path(root).expanduser() if root is not None else default_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for packaged, local in (
        ("routes.example.json", "routes.json"),
        ("COORDINATOR.example.md", "COORDINATOR.md"),
    ):
        destination = directory / local
        if not destination.exists():
            shutil.copyfile(PACKAGE_DIR / packaged, destination)
    return directory
