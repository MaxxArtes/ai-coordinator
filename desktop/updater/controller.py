"""AI Coordinator Desktop — Auto-Updater Controller & Cryptographic Verifier.

Governance:
- Ed25519 / Minisign cryptographic signature verification of all update payloads.
- Strict active session safety fence (never terminate running processes silently).
- Seamless offline tolerance and zero-network resilience.
- User data, configurations, and authorized projects preservation.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519


# Standard Minisign public key for AI Coordinator Desktop Alpha
DEFAULT_PUBKEY_B64 = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDQ5OUZGODkxMkQ2NjVGRTgKUldUb1gyWXRrZmlmU2RFTEZQUnhMNEloODBpU2JNd3U0RWxBa29WYzJFeWdFSTk5NkcrTWhNRjMK"
)

DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/MaxxArtes/ai-coordinator/feature/phase4d-m2-terminals/desktop/updates/alpha.json"
)


def parse_version(v_str: str) -> Tuple[int, int, int, str, int]:
    """Parse version string into comparable tuple: (major, minor, patch, prerelease_tag, prerelease_num).

    Examples:
        '0.6.0' -> (0, 6, 0, 'release', 0)
        '0.6.1-alpha.48' -> (0, 6, 1, 'alpha', 48)
        '0.6.1-alpha.49' -> (0, 6, 1, 'alpha', 49)
        'v0.6.1' -> (0, 6, 1, 'release', 0)
    """
    cleaned = v_str.strip().lstrip("v")
    pattern = r"^(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z]+)(?:\.(\d+))?)?$"
    m = re.match(pattern, cleaned)
    if not m:
        # Fallback numeric extraction
        nums = [int(n) for n in re.findall(r"\d+", cleaned)]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2], "release", 0)

    major = int(m.group(1))
    minor = int(m.group(2))
    patch = int(m.group(3))
    prerelease_tag = m.group(4) or "release"
    prerelease_num = int(m.group(5)) if m.group(5) else 0

    return (major, minor, patch, prerelease_tag, prerelease_num)


def compare_versions(v1: str, v2: str) -> int:
    """Compare two version strings.

    Returns:
        -1 if v1 < v2
         0 if v1 == v2
         1 if v1 > v2
    """
    p1 = parse_version(v1)
    p2 = parse_version(v2)

    # Compare major, minor, patch
    if p1[0:3] < p2[0:3]:
        return -1
    if p1[0:3] > p2[0:3]:
        return 1

    # Same major.minor.patch
    # A release is greater than any prerelease
    is_p1_rel = p1[3] == "release"
    is_p2_rel = p2[3] == "release"
    if is_p1_rel and not is_p2_rel:
        return 1
    if not is_p1_rel and is_p2_rel:
        return -1
    if is_p1_rel and is_p2_rel:
        return 0

    # Both are prereleases (e.g. 'alpha')
    if p1[3] < p2[3]:
        return -1
    if p1[3] > p2[3]:
        return 1

    # Compare prerelease numbers
    if p1[4] < p2[4]:
        return -1
    if p1[4] > p2[4]:
        return 1

    return 0


def extract_ed25519_pubkey(pub_str: str) -> ed25519.Ed25519PublicKey:
    """Extract raw 32-byte Ed25519 public key from Minisign or raw base64 string."""
    clean = pub_str.strip()
    try:
        decoded_text = base64.b64decode(clean).decode("utf-8", errors="ignore")
        if "untrusted comment" in decoded_text:
            lines = [line.strip() for line in decoded_text.splitlines() if line.strip()]
            if len(lines) >= 2:
                raw_pub = base64.b64decode(lines[1])
                # Minisign pubkey layout: 2 bytes 'Ed' + 8 bytes key ID + 32 bytes pubkey
                if len(raw_pub) == 42:
                    return ed25519.Ed25519PublicKey.from_public_bytes(raw_pub[10:])
    except Exception:
        pass

    # Direct raw base64
    raw = base64.b64decode(clean)
    if len(raw) == 32:
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    elif len(raw) == 42:
        return ed25519.Ed25519PublicKey.from_public_bytes(raw[10:])

    raise ValueError(f"Invalid public key length: {len(raw)} (expected 32 or 42 bytes)")


def extract_ed25519_signature(sig_str: str) -> Tuple[bytes, bool]:
    """Extract raw 64-byte Ed25519 signature and prehash flag from Minisign or raw base64 string.

    Returns:
        (sig_bytes, is_prehashed_blake2b)
    """
    clean = sig_str.strip()

    # Case 1: Minisign multiline text wrapped in base64 (Tauri Updater format)
    try:
        decoded_text = base64.b64decode(clean).decode("utf-8", errors="ignore")
        if "untrusted comment" in decoded_text:
            lines = [line.strip() for line in decoded_text.splitlines() if line.strip()]
            if len(lines) >= 2:
                sig_raw = base64.b64decode(lines[1])
                is_prehashed = sig_raw[:2] == b"ED"
                return (sig_raw[10:74], is_prehashed)
    except Exception:
        pass

    # Case 2: Minisign multiline plain text
    if "untrusted comment" in clean:
        lines = [line.strip() for line in clean.splitlines() if line.strip()]
        if len(lines) >= 2:
            sig_raw = base64.b64decode(lines[1])
            is_prehashed = sig_raw[:2] == b"ED"
            return (sig_raw[10:74], is_prehashed)

    # Case 3: Raw base64 of 64 or 74 bytes
    raw = base64.b64decode(clean)
    if len(raw) == 64:
        return (raw, False)
    elif len(raw) == 74:
        is_prehashed = raw[:2] == b"ED"
        return (raw[10:74], is_prehashed)

    raise ValueError(f"Invalid signature length: {len(raw)} (expected 64 or 74 bytes)")


@dataclass
class PlatformUpdate:
    signature: str
    url: str


@dataclass
class UpdateManifest:
    version: str
    notes: str
    pub_date: str
    platforms: Dict[str, PlatformUpdate] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> UpdateManifest:
        version = data.get("version", "")
        notes = data.get("notes", "")
        pub_date = data.get("pub_date", "")
        platforms = {}
        for platform_key, plat_data in data.get("platforms", {}).items():
            if isinstance(plat_data, dict):
                platforms[platform_key] = PlatformUpdate(
                    signature=plat_data.get("signature", ""),
                    url=plat_data.get("url", ""),
                )
        return cls(
            version=version,
            notes=notes,
            pub_date=pub_date,
            platforms=platforms,
        )


@dataclass
class UpdateCheckResult:
    available: bool
    current_version: str
    latest_version: str
    notes: str
    date: str
    active_sessions_count: int
    offline: bool
    error: Optional[str] = None
    download_url: Optional[str] = None
    signature: Optional[str] = None


class DesktopUpdaterController:
    """Coordinates version checks, cryptographic verification, and update safety fences."""

    def __init__(
        self,
        current_version: str = "0.6.0",
        channel: str = "alpha",
        manifest_url: Optional[str] = None,
        pubkey: Optional[str] = None,
    ) -> None:
        self.current_version = current_version
        self.channel = channel
        self.manifest_url = manifest_url or DEFAULT_MANIFEST_URL
        self.pubkey = pubkey or DEFAULT_PUBKEY_B64

    def check_update(
        self,
        active_sessions_count: int = 0,
        custom_manifest_content: Optional[str] = None,
        timeout_sec: float = 5.0,
    ) -> UpdateCheckResult:
        """Check for updates against the manifest URL or provided content.

        Handles network failures gracefully without throwing exceptions (offline mode).
        """
        raw_manifest = ""
        if custom_manifest_content is not None:
            raw_manifest = custom_manifest_content
        else:
            try:
                req = urllib.request.Request(
                    self.manifest_url,
                    headers={"User-Agent": "AI-Coordinator-Desktop-Updater"},
                )
                with urllib.request.urlopen(req, timeout=timeout_sec) as response:
                    raw_manifest = response.read().decode("utf-8")
            except urllib.error.URLError as e:
                return UpdateCheckResult(
                    available=False,
                    current_version=self.current_version,
                    latest_version=self.current_version,
                    notes="",
                    date="",
                    active_sessions_count=active_sessions_count,
                    offline=True,
                    error=f"Falha de rede ao consultar atualizações (modo offline): {e}",
                )
            except Exception as e:
                return UpdateCheckResult(
                    available=False,
                    current_version=self.current_version,
                    latest_version=self.current_version,
                    notes="",
                    date="",
                    active_sessions_count=active_sessions_count,
                    offline=True,
                    error=f"Erro ao verificar atualização: {e}",
                )

        try:
            data = json.loads(raw_manifest)
            manifest = UpdateManifest.from_dict(data)
        except Exception as e:
            return UpdateCheckResult(
                available=False,
                current_version=self.current_version,
                latest_version=self.current_version,
                notes="",
                date="",
                active_sessions_count=active_sessions_count,
                offline=False,
                error=f"Manifesto de atualização corrompido ou inválido: {e}",
            )

        cmp = compare_versions(self.current_version, manifest.version)
        available = cmp < 0

        win_plat = manifest.platforms.get("windows-x86_64")
        dl_url = win_plat.url if win_plat else None
        sig = win_plat.signature if win_plat else None

        return UpdateCheckResult(
            available=available,
            current_version=self.current_version,
            latest_version=manifest.version,
            notes=manifest.notes,
            date=manifest.pub_date,
            active_sessions_count=active_sessions_count,
            offline=False,
            error=None,
            download_url=dl_url,
            signature=sig,
        )

    def verify_signature(
        self,
        data_bytes: bytes,
        signature_str: str,
        custom_pubkey: Optional[str] = None,
    ) -> bool:
        """Cryptographically verify Ed25519 / Minisign signature over binary payload."""
        pubkey_to_use = custom_pubkey or self.pubkey
        try:
            pk = extract_ed25519_pubkey(pubkey_to_use)
            sig_bytes, is_prehashed = extract_ed25519_signature(signature_str)

            if is_prehashed:
                digest = hashlib.blake2b(data_bytes, digest_size=64).digest()
                pk.verify(sig_bytes, digest)
            else:
                pk.verify(sig_bytes, data_bytes)
            return True
        except (InvalidSignature, ValueError, Exception):
            return False

    def can_safely_update(
        self,
        active_sessions_count: int,
        force: bool = False,
    ) -> Tuple[bool, str]:
        """Safety fence: prevents updating when active terminal sessions or workers are running.

        Fail-closed: if the count is negative, non-integer, or indicates an error, block unless forced.
        """
        if not isinstance(active_sessions_count, int) or active_sessions_count < 0:
            if not force:
                return (
                    False,
                    "Não foi possível verificar com segurança o estado dos terminais ativos (fail-closed). "
                    "A atualização requer verificação bem-sucedida ou autorização explícita (force=True).",
                )
            return (True, "Atualização forçada autorizada pelo operador apesar de contagem de sessões indefinida.")

        if active_sessions_count > 0 and not force:
            return (
                False,
                f"Existem {active_sessions_count} terminais ou processos ativos em execução. "
                "Conclua suas tarefas ou autorize a reinicialização forçada.",
            )
        return (True, "Pronto para atualização segura.")

    @staticmethod
    def get_data_directories() -> Dict[str, str]:
        """Return user data paths that must remain untouched during updates."""
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "")
        return {
            "appdata": appdata,
            "localappdata": localappdata,
            "userprofile": userprofile,
            "desktop_config": os.path.join(appdata, "ai-coordinator-desktop"),
        }
