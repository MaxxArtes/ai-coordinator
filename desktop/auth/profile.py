"""Desktop profiles: atomic updates, authorization fencing and lossless settings preservation."""
from __future__ import annotations

import copy
import datetime
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .file_lock import interprocess_lock, retry_sharing_violation

PROFILE_NAME_REGEX = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")

RECOVERY_MESSAGE = (
    "O perfil ativo estava ilegível. Uma cópia exata foi guardada na pasta do perfil e um perfil novo, com o mesmo "
    "nome, foi criado sem projetos autorizados e sem configurações. Autorize os projetos novamente."
)


class ProfileError(Exception):
    """A profile could not be read or written. Its previous bytes are preserved."""


class ProfileValidationError(ProfileError):
    """Invalid profile name or configuration."""


class ProfileNotFoundError(ProfileError):
    """The requested profile does not exist."""


class ProfileCorruptError(ProfileError):
    """The profile bytes were read but are not a valid profile (as opposed to a file that could not be read)."""


def _same_path_text(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _resolved_text(path: Union[str, Path]) -> Optional[str]:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, ValueError, RuntimeError):
        return None


def _is_within(child: str, parent: str) -> bool:
    """Case and separator insensitive where the platform is (os.path.normcase), without touching the disk."""
    c = os.path.normcase(os.path.normpath(child))
    p = os.path.normcase(os.path.normpath(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def path_forms(path: Union[str, Path]) -> List[str]:
    """Literal and resolved forms of a path, normalized as the authorization compares them."""
    forms = [os.path.normcase(os.path.normpath(str(path)))]
    resolved = _resolved_text(path)
    if resolved is not None and resolved not in forms:
        forms.append(resolved)
    return forms


def path_matches(entry: Union[str, Path], target: Union[str, Path], *, include_children: bool = False) -> bool:
    """True when `entry` names `target` (or, with include_children, lies inside it) literally or once resolved."""
    for entry_form in path_forms(entry):
        for target_form in path_forms(target):
            if entry_form == target_form or (include_children and _is_within(entry_form, target_form)):
                return True
    return False


@dataclass
class DesktopProfile:
    name: str
    display_name: str
    data_dir: str
    created_at: str
    authorized_projects: List[str] = field(default_factory=list)
    # Normally an object; any other stored JSON value is kept as is and reported read-only by the settings store.
    settings: Any = field(default_factory=dict)
    last_project: Optional[str] = None
    extra_fields: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        values = asdict(self)
        extra = values.pop("extra_fields")
        # Unknown profile extensions are retained, but cannot override the canonical fields.
        return {**extra, **values}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DesktopProfile:
        if not isinstance(data, dict):
            raise ProfileValidationError("Formato do perfil inválido. Os dados foram preservados.")
        name = data.get("name")
        if not isinstance(name, str) or not PROFILE_NAME_REGEX.fullmatch(name) or name in (".", ".."):
            raise ProfileValidationError("Nome do perfil inválido. Os dados foram preservados.")
        for key in ("data_dir", "display_name", "created_at"):
            if key in data and not isinstance(data[key], str):
                raise ProfileValidationError("Metadados do perfil inválidos. Os dados foram preservados.")
        if not data.get("data_dir"):
            raise ProfileValidationError("Diretório do perfil inválido. Os dados foram preservados.")
        authorized = data.get("authorized_projects", [])
        if not isinstance(authorized, list) or any(not isinstance(p, str) or not p for p in authorized):
            raise ProfileValidationError("Lista de diretórios do perfil inválida. Os dados foram preservados.")
        # Settings are not validated here: a malformed envelope must not make the whole profile unreadable.
        settings = data.get("settings", {})
        last_project = data.get("last_project")
        if last_project is not None and (not isinstance(last_project, str) or not last_project):
            raise ProfileValidationError("Último projeto inválido. Os dados foram preservados.")
        known = {"name", "display_name", "data_dir", "created_at", "authorized_projects", "settings", "last_project"}
        return cls(
            name=name,
            display_name=data.get("display_name", name),
            data_dir=data["data_dir"],
            created_at=data.get("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat()),
            authorized_projects=authorized,
            settings=settings,
            last_project=last_project,
            extra_fields={key: value for key, value in data.items() if key not in known},
        )


class DesktopProfileManager:
    """Profiles use a stable sidecar lock; atomic replacement never changes the lock inode.

    Lock order: profiles.lock before a profile's profile.lock.
    """

    def __init__(self, root_dir: Optional[Path] = None) -> None:
        self.root_dir = (Path(root_dir) if root_dir is not None else Path.home() / ".ai-coordinator" / "desktop" / "profiles").resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.active_file = self.root_dir / "active_profile.txt"
        # Set when this manager quarantined and recreated the active profile (E2-6); reported by profile.get_active.
        self.recovery: Optional[Dict[str, str]] = None
        # The default profile is created only in an empty installation: an unreadable profile.json still counts.
        with interprocess_lock(self.root_dir / "profiles.lock"):
            if not any(item.is_dir() and (item / "profile.json").exists() for item in self.root_dir.iterdir()):
                self.create_profile("default", display_name="Default Profile")
                self.set_active_profile("default")

    def _validate_name(self, name: str) -> str:
        if not isinstance(name, str) or not name:
            raise ProfileValidationError("Profile name must be a non-empty string")
        cleaned = name.strip()
        if not PROFILE_NAME_REGEX.fullmatch(cleaned) or cleaned in (".", ".."):
            raise ProfileValidationError("Profile name must use letters, numbers, '.', '-', '_' (1-32 chars).")
        return cleaned

    def _profile_file(self, name: str) -> Path:
        return self.root_dir / self._validate_name(name) / "profile.json"

    def _profile_lock(self, name: str) -> Path:
        return self._profile_file(name).with_name("profile.lock")

    def list_profiles(self) -> List[DesktopProfile]:
        """Readable profiles only. An unreadable one is skipped here: never quarantined, rewritten or deleted."""
        profiles = []
        for item in self.root_dir.iterdir():
            if item.is_dir() and (item / "profile.json").exists():
                try:
                    profile = self.get_profile(item.name)
                except ProfileError:
                    continue
                if profile is not None:
                    profiles.append(profile)
        return sorted(profiles, key=lambda p: p.name)

    def get_profile(self, name: str) -> Optional[DesktopProfile]:
        cleaned = self._validate_name(name)
        target = self._profile_file(cleaned)
        try:
            raw = retry_sharing_violation(target.read_bytes)
        except FileNotFoundError:
            return None
        except OSError:
            raise ProfileError("Não foi possível ler o perfil salvo. Os dados foram preservados.") from None
        try:
            def reject_constant(_value):
                raise ValueError("Invalid JSON constant")
            # A BOM written by a text editor is tolerated; every other decoding problem makes the profile corrupt.
            data = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant)
            if isinstance(data, dict):
                # The data directory is where the file really is: a moved or copied profile folder keeps working,
                # and a stored path can never point the vault somewhere else.
                data = {**data, "data_dir": str(target.parent)}
            profile = DesktopProfile.from_dict(data)
            if profile.name != cleaned:
                raise ValueError("Profile identity mismatch")
            return profile
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, ProfileError):
            # Do not interpolate parse errors: they can echo private values from the profile.
            raise ProfileCorruptError("O perfil salvo está inválido ou ilegível. Os dados foram preservados.") from None

    def create_profile(self, name: str, display_name: Optional[str] = None,
                       initial_authorized_projects: Optional[List[str]] = None,
                       initial_settings: Optional[Dict[str, Any]] = None) -> DesktopProfile:
        cleaned = self._validate_name(name)
        directory = self.root_dir / cleaned
        with interprocess_lock(self._profile_lock(cleaned)):
            if (directory / "profile.json").exists():
                raise ProfileValidationError("Profile already exists")
            directory.mkdir(parents=True, exist_ok=True)
            profile = DesktopProfile(
                name=cleaned, display_name=display_name or cleaned.replace("_", " ").title(),
                data_dir=str(directory), created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                authorized_projects=sorted({str(Path(p).resolve()) for p in initial_authorized_projects or []}),
                settings=copy.deepcopy(initial_settings) if initial_settings is not None else {},
            )
            self._save_profile(profile)
            return profile

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix="profile_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            retry_sharing_violation(lambda: os.replace(temporary, target))
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise ProfileError("Não foi possível gravar o perfil. Os dados anteriores foram preservados.") from None

    def _save_profile(self, profile: DesktopProfile) -> None:
        with interprocess_lock(self._profile_lock(profile.name)):
            try:
                content = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise ProfileValidationError("Configuração de perfil inválida. Nada foi gravado.") from None
            self._atomic_write(self._profile_file(profile.name), content)

    @staticmethod
    def _exclusive_copy(target: Path, raw: bytes) -> None:
        """Write `raw` to a new file; an existing file is never replaced. The copy is read back and compared."""
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if retry_sharing_violation(target.read_bytes) != raw:
                raise OSError("backup verification failed")
        except BaseException:
            try:
                os.unlink(target)
            except OSError:
                pass
            raise

    def backup_profile_file(self, name: str, suffix: str) -> bool:
        """Copy profile.json byte for byte to profile.json.<suffix> once; False when that copy already exists."""
        source = self._profile_file(name)
        backup = source.with_name(f"profile.json.{suffix}")
        with interprocess_lock(self._profile_lock(name)):
            if backup.exists():
                return False
            try:
                self._exclusive_copy(backup, retry_sharing_violation(source.read_bytes))
            except FileExistsError:
                return False
            except OSError:
                raise ProfileError("Não foi possível guardar a cópia de segurança do perfil. Nada foi alterado.") from None
            return True

    def _quarantine(self, target: Path) -> Path:
        try:
            raw = retry_sharing_violation(target.read_bytes)
        except OSError:
            raise ProfileError("Não foi possível ler o perfil salvo. Os dados foram preservados.") from None
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for attempt in range(1, 100):
            candidate = target.with_name(f"profile.json.corrupt-{stamp}" + ("" if attempt == 1 else f"-{attempt}"))
            try:
                self._exclusive_copy(candidate, raw)
            except FileExistsError:
                continue  # never overwrite an earlier quarantine
            except OSError:
                break
            return candidate
        raise ProfileError("Não foi possível guardar a cópia do perfil ilegível. Os dados foram preservados.")

    def _recover_active(self, name: str) -> DesktopProfile:
        """Quarantine an unreadable active profile byte for byte, then recreate it empty with the same name."""
        with interprocess_lock(self.root_dir / "profiles.lock"):
            with interprocess_lock(self._profile_lock(name)):
                try:
                    profile = self.get_profile(name)  # another window may already have recovered it
                except ProfileCorruptError:
                    profile = None
                else:
                    if profile is None:
                        raise ProfileNotFoundError("O perfil ativo não foi encontrado. Nenhum perfil foi recriado.")
                    return profile
                target = self._profile_file(name)
                backup = self._quarantine(target)
                fresh = DesktopProfile(
                    name=name, display_name=name.replace("_", " ").title(), data_dir=str(target.parent),
                    created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                self._save_profile(fresh)
                self.recovery = {"reason": "profile_unreadable", "backup_file": backup.name, "message": RECOVERY_MESSAGE}
                return fresh

    def _mutate(self, name: str, change: Callable[[DesktopProfile], Any]) -> tuple[DesktopProfile, Any]:
        with interprocess_lock(self._profile_lock(name)):
            profile = self.get_profile(name)
            if profile is None:
                raise ProfileNotFoundError("Profile not found")
            result = change(profile)
            self._save_profile(profile)
            return profile, result

    def mutate_settings(self, profile_name: str, transform: Callable[[Any], Any]) -> DesktopProfile:
        """Read, validate/merge and write preferences under the same interprocess profile lock."""
        def change(profile):
            profile.settings = transform(copy.deepcopy(profile.settings))
        return self._mutate(profile_name, change)[0]

    def update_settings(self, profile_name: str, settings: Dict[str, Any]) -> DesktopProfile:
        return self.mutate_settings(profile_name, lambda _current: copy.deepcopy(settings))

    def delete_profile(self, name: str) -> bool:
        cleaned = self._validate_name(name)
        if cleaned == "default":
            raise ProfileValidationError("Cannot delete default profile")
        with interprocess_lock(self.root_dir / "profiles.lock"):
            if self.get_active_profile().name == cleaned:
                raise ProfileValidationError("Cannot delete currently active profile")
            directory = self.root_dir / cleaned
            if not directory.exists():
                return False
            # The validated single-component name confines this existing API to its profile directory.
            if directory.resolve().parent != self.root_dir:
                raise ProfileValidationError("Invalid profile directory")
            import shutil
            shutil.rmtree(directory)
            return True

    def get_active_profile(self) -> DesktopProfile:
        if self.active_file.exists():
            try:
                active_name = retry_sharing_violation(lambda: self.active_file.read_text(encoding="utf-8")).strip()
            except (OSError, UnicodeError):
                raise ProfileError("Não foi possível ler a seleção de perfil. Os dados foram preservados.") from None
            missing = "O perfil ativo não foi encontrado. Nenhum perfil foi recriado."
        else:
            active_name = "default"
            missing = "Nenhum perfil ativo foi definido. Selecione um perfil existente."
        try:
            active_name = self._validate_name(active_name)
        except ProfileValidationError:
            raise ProfileError("A seleção de perfil está inválida. Os dados foram preservados.") from None
        try:
            profile = self.get_profile(active_name)
        except ProfileCorruptError:
            # E2-6: an unreadable active profile must not prevent opening the application.
            return self._recover_active(active_name)
        if profile is None:
            raise ProfileNotFoundError(missing)
        return profile

    def set_active_profile(self, name: str) -> DesktopProfile:
        cleaned = self._validate_name(name)
        with interprocess_lock(self.root_dir / "profiles.lock"):
            profile = self.get_profile(cleaned)
            if profile is None:
                raise ProfileNotFoundError("Profile not found")
            self._atomic_write(self.active_file, cleaned)
            return profile

    def authorize_project(self, profile_name: str, project_path: Union[str, Path]) -> None:
        canon = str(Path(project_path).resolve())
        def change(profile):
            # One entry per directory: an equivalent entry (other letter case, older spelling) is replaced.
            kept = [p for p in profile.authorized_projects if not path_matches(p, canon)]
            profile.authorized_projects = sorted(kept + [canon])
        self._mutate(profile_name, change)

    def revoke_project(self, profile_name: str, project_path: Union[str, Path],
                       guard: Optional[Callable[[List[str]], None]] = None) -> bool:
        """Remove every entry naming the directory (literal or resolved, os.path.normcase) and forget it as the last
        project when it is that directory or lies inside it. `guard` sees the matching entries before anything is
        written and may raise to refuse."""
        def change(profile):
            matching = [p for p in profile.authorized_projects if path_matches(p, project_path)]
            if guard is not None:
                guard(matching)
            if not matching:
                return False
            profile.authorized_projects = [p for p in profile.authorized_projects if p not in matching]
            if profile.last_project and any(
                    path_matches(profile.last_project, target, include_children=True)
                    for target in [str(project_path), *matching]):
                profile.last_project = None
            return True
        return self._mutate(profile_name, change)[1]

    @staticmethod
    def _authorized(profile: DesktopProfile, target: Path) -> bool:
        resolved = _resolved_text(target)
        if resolved is None:
            return False
        for entry in profile.authorized_projects:
            entry_resolved = _resolved_text(entry)
            if entry_resolved is not None and _is_within(resolved, entry_resolved):
                return True
        return False

    def remember_project(self, profile_name: str, project_path: Union[str, Path]) -> None:
        target = Path(project_path).resolve()
        def change(profile):
            if not self._authorized(profile, target):
                raise ProfileValidationError("O último projeto deve estar autorizado no perfil.")
            profile.last_project = str(target)
        self._mutate(profile_name, change)

    def is_project_authorized(self, profile_name: str, project_path: Union[str, Path]) -> bool:
        profile = self.get_profile(profile_name)
        return profile is not None and self._authorized(profile, Path(project_path).resolve())
