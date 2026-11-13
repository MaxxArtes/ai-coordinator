"""Desktop Profile Manager for AI Coordinator Desktop (Marco 4D.1.3).

Provides multi-profile isolation (default, work, testing) with independent
data directories, authorized project path fencing, and persistent settings.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PROFILE_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")


class ProfileError(Exception):
    """Base exception for profile operations."""


class ProfileValidationError(ProfileError):
    """Raised when profile name or configuration is invalid."""


class ProfileNotFoundError(ProfileError):
    """Raised when the requested profile does not exist."""


@dataclass
class DesktopProfile:
    """Represents an isolated user profile for AI Coordinator Desktop."""

    name: str
    display_name: str
    data_dir: str
    created_at: str
    authorized_projects: List[str] = field(default_factory=list)
    settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DesktopProfile:
        return cls(
            name=data["name"],
            display_name=data.get("display_name", data["name"]),
            data_dir=data["data_dir"],
            created_at=data.get("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            authorized_projects=data.get("authorized_projects", []),
            settings=data.get("settings", {}),
        )


class DesktopProfileManager:
    """Manages creation, switching, persistence, and isolation of desktop profiles."""

    def __init__(self, root_dir: Optional[Path] = None) -> None:
        if root_dir is None:
            base = Path.home() / ".ai-coordinator" / "desktop"
            self.root_dir = base / "profiles"
        else:
            self.root_dir = Path(root_dir).resolve()

        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.active_file = self.root_dir / "active_profile.txt"

        # Ensure default profile exists
        if not self.list_profiles():
            self.create_profile("default", display_name="Default Profile")
            self.set_active_profile("default")

    def _validate_name(self, name: str) -> str:
        if not name or not isinstance(name, str):
            raise ProfileValidationError("Profile name must be a non-empty string")
        cleaned = name.strip()
        if not PROFILE_NAME_REGEX.match(cleaned):
            raise ProfileValidationError(
                f"Profile name '{name}' is invalid. Allowed: alphanumeric, '.', '-', '_' (1-32 chars)."
            )
        return cleaned

    def _profile_file(self, name: str) -> Path:
        return self.root_dir / name / "profile.json"

    def list_profiles(self) -> List[DesktopProfile]:
        """List all available profiles."""
        profiles = []
        if not self.root_dir.exists():
            return profiles

        for item in self.root_dir.iterdir():
            if item.is_dir():
                p_file = item / "profile.json"
                if p_file.exists():
                    try:
                        data = json.loads(p_file.read_text(encoding="utf-8"))
                        profiles.append(DesktopProfile.from_dict(data))
                    except Exception:
                        continue
        profiles.sort(key=lambda p: p.name)
        return profiles

    def get_profile(self, name: str) -> Optional[DesktopProfile]:
        """Get profile by name."""
        cleaned = self._validate_name(name)
        p_file = self._profile_file(cleaned)
        if not p_file.exists():
            return None
        try:
            data = json.loads(p_file.read_text(encoding="utf-8"))
            return DesktopProfile.from_dict(data)
        except Exception as exc:
            raise ProfileError(f"Error reading profile '{name}': {exc}") from exc

    def create_profile(
        self,
        name: str,
        display_name: Optional[str] = None,
        initial_authorized_projects: Optional[List[str]] = None,
        initial_settings: Optional[Dict[str, Any]] = None,
    ) -> DesktopProfile:
        """Create a new profile with an isolated directory."""
        cleaned = self._validate_name(name)
        p_dir = self.root_dir / cleaned
        if p_dir.exists() and (p_dir / "profile.json").exists():
            raise ProfileValidationError(f"Profile '{cleaned}' already exists")

        p_dir.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        canon_projects = []
        if initial_authorized_projects:
            for p in initial_authorized_projects:
                canon_projects.append(str(Path(p).resolve()))

        profile = DesktopProfile(
            name=cleaned,
            display_name=display_name or cleaned.replace("_", " ").title(),
            data_dir=str(p_dir),
            created_at=now_iso,
            authorized_projects=sorted(list(set(canon_projects))),
            settings=initial_settings or {},
        )
        self._save_profile(profile)
        return profile

    def _save_profile(self, profile: DesktopProfile) -> None:
        p_file = self._profile_file(profile.name)
        p_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = p_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")
        temp_file.replace(p_file)

    def update_settings(self, profile_name: str, settings: Dict[str, Any]) -> DesktopProfile:
        """Replace the persisted (non-secret) settings of a profile. Validation happens in desktop.settings."""
        profile = self.get_profile(profile_name)
        if not profile:
            raise ValueError(f"Profile '{profile_name}' not found")
        profile.settings = dict(settings)
        self._save_profile(profile)
        return profile

    def delete_profile(self, name: str) -> bool:
        """Delete a profile and its contents. Cannot delete active profile or 'default'."""
        cleaned = self._validate_name(name)
        if cleaned == "default":
            raise ProfileValidationError("Cannot delete default profile")

        active = self.get_active_profile()
        if active.name == cleaned:
            raise ProfileValidationError("Cannot delete currently active profile")

        p_dir = self.root_dir / cleaned
        if not p_dir.exists():
            return False

        import shutil

        shutil.rmtree(p_dir)
        return True

    def get_active_profile(self) -> DesktopProfile:
        """Get the currently active profile."""
        if self.active_file.exists():
            active_name = self.active_file.read_text(encoding="utf-8").strip()
            profile = self.get_profile(active_name)
            if profile:
                return profile

        # Fallback to default
        default_prof = self.get_profile("default")
        if default_prof:
            self.set_active_profile("default")
            return default_prof

        # Create default if missing
        prof = self.create_profile("default", display_name="Default Profile")
        self.set_active_profile("default")
        return prof

    def set_active_profile(self, name: str) -> DesktopProfile:
        """Switch active profile."""
        cleaned = self._validate_name(name)
        profile = self.get_profile(cleaned)
        if not profile:
            raise ProfileNotFoundError(f"Profile '{cleaned}' not found")

        self.active_file.write_text(cleaned, encoding="utf-8")
        return profile

    def authorize_project(self, profile_name: str, project_path: Union[str, Path]) -> None:
        """Authorize a workspace/project path for the profile."""
        profile = self.get_profile(profile_name)
        if not profile:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

        canon = str(Path(project_path).resolve())
        if canon not in profile.authorized_projects:
            profile.authorized_projects.append(canon)
            profile.authorized_projects.sort()
            self._save_profile(profile)

    def revoke_project(self, profile_name: str, project_path: Union[str, Path]) -> bool:
        """Revoke authorization for a project path."""
        profile = self.get_profile(profile_name)
        if not profile:
            raise ProfileNotFoundError(f"Profile '{profile_name}' not found")

        canon = str(Path(project_path).resolve())
        if canon in profile.authorized_projects:
            profile.authorized_projects.remove(canon)
            self._save_profile(profile)
            return True
        return False

    def is_project_authorized(self, profile_name: str, project_path: Union[str, Path]) -> bool:
        """Check if project path or its ancestor is authorized."""
        profile = self.get_profile(profile_name)
        if not profile:
            return False

        target = Path(project_path).resolve()
        for auth in profile.authorized_projects:
            auth_path = Path(auth).resolve()
            if target == auth_path or auth_path in target.parents:
                return True
        return False
