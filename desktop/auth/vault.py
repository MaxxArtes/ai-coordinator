"""Secure Credential and Cryptographic Key Vault for AI Coordinator Desktop (Marco 4D.1.3).

Provides OS-native encryption via Windows DPAPI with per-profile entropy and
portable AEAD AES-256-GCM fallback with cryptographic key derivation.
"""

from __future__ import annotations

import abc
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional


class VaultError(Exception):
    """Base exception for secure vault operations."""


class VaultDecryptionError(VaultError):
    """Raised when vault data cannot be decrypted (e.g. wrong key, tampered data, cross-profile)."""


class VaultKeyNotFoundError(VaultError):
    """Raised when the requested secret key does not exist."""


class VaultUnavailableError(VaultError):
    """Raised when persistent credential storage is not available on this platform (decision D8)."""


UNAVAILABLE_MESSAGE = (
    "Armazenamento persistente de credenciais indisponível nesta plataforma: não há cofre seguro do sistema "
    "integrado (DPAPI no Windows; Keychain e Secret Service planejados para a 4D.6). Nenhuma credencial foi gravada."
)


class BaseVault(abc.ABC):
    """Abstract base class for credential vaults."""

    #: Whether this vault can persist secrets with adequate protection.
    available: bool = True
    #: Backend identifier for diagnostics ("dpapi", "aes_gcm", "unavailable").
    backend_name: str = "unknown"

    def __init__(self, profile_name: str, vault_path: Path) -> None:
        self.profile_name = profile_name
        self.vault_path = vault_path

    @abc.abstractmethod
    def set_secret(self, key: str, value: str) -> None:
        """Store an encrypted secret under the given key."""

    @abc.abstractmethod
    def get_secret(self, key: str) -> Optional[str]:
        """Retrieve and decrypt the secret under the given key."""

    @abc.abstractmethod
    def delete_secret(self, key: str) -> bool:
        """Remove a secret from the vault."""

    @abc.abstractmethod
    def list_keys(self) -> List[str]:
        """List all secret keys stored in the vault."""

    def has_secret(self, key: str) -> bool:
        """Check if a secret key exists."""
        return key in self.list_keys()


class Win32DPAPIVault(BaseVault):
    """Windows Data Protection API (DPAPI) vault with per-profile entropy isolation.

    Uses CryptProtectData / CryptUnprotectData from crypt32.dll via ctypes,
    eliminating external binary dependencies and guaranteeing that even within
    the same OS user account, Profile A's entropy cannot decrypt Profile B's data.
    """

    backend_name = "dpapi"

    def __init__(self, profile_name: str, vault_path: Path) -> None:
        super().__init__(profile_name, vault_path)
        if sys.platform != "win32":
            raise RuntimeError("Win32DPAPIVault is only supported on Windows")

        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        self._DATA_BLOB = DATA_BLOB
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32
        self._entropy = f"ai-coordinator:profile:{profile_name}".encode("utf-8")

    def _protect(self, data: bytes) -> bytes:
        import ctypes

        blob_in = self._DATA_BLOB(
            len(data),
            ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),
        )
        entropy_blob = self._DATA_BLOB(
            len(self._entropy),
            ctypes.cast(
                ctypes.create_string_buffer(self._entropy),
                ctypes.POINTER(ctypes.c_char),
            ),
        )
        blob_out = self._DATA_BLOB()

        # CRYPTPROTECT_UI_FORBIDDEN = 0x01
        res = self._crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            "AI Coordinator Desktop Vault",
            ctypes.byref(entropy_blob),
            None,
            None,
            0x01,
            ctypes.byref(blob_out),
        )
        if not res:
            raise VaultError("DPAPI CryptProtectData failed")

        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)

    def _unprotect(self, data: bytes) -> bytes:
        import ctypes

        blob_in = self._DATA_BLOB(
            len(data),
            ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),
        )
        entropy_blob = self._DATA_BLOB(
            len(self._entropy),
            ctypes.cast(
                ctypes.create_string_buffer(self._entropy),
                ctypes.POINTER(ctypes.c_char),
            ),
        )
        blob_out = self._DATA_BLOB()

        res = self._crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            0x01,
            ctypes.byref(blob_out),
        )
        if not res:
            raise VaultDecryptionError("DPAPI CryptUnprotectData failed (entropy or key mismatch)")

        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            self._kernel32.LocalFree(blob_out.pbData)

    def _load_dict(self) -> Dict[str, str]:
        if not self.vault_path.exists():
            return {}
        try:
            encrypted_bytes = self.vault_path.read_bytes()
            if not encrypted_bytes:
                return {}
            decrypted_bytes = self._unprotect(encrypted_bytes)
            return json.loads(decrypted_bytes.decode("utf-8"))
        except VaultDecryptionError:
            raise
        except Exception as exc:
            raise VaultError(f"Failed to read vault file: {exc}") from exc

    def _save_dict(self, data: Dict[str, str]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        raw_bytes = json.dumps(data, indent=2).encode("utf-8")
        encrypted_bytes = self._protect(raw_bytes)

        # Atomic file write to avoid corruption
        temp_fd, temp_path = tempfile.mkstemp(
            dir=str(self.vault_path.parent),
            prefix="vault_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(encrypted_bytes)
            os.replace(temp_path, self.vault_path)
        except Exception as exc:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise VaultError(f"Failed to save vault: {exc}") from exc

    def set_secret(self, key: str, value: str) -> None:
        data = self._load_dict()
        data[key] = value
        self._save_dict(data)

    def get_secret(self, key: str) -> Optional[str]:
        data = self._load_dict()
        return data.get(key)

    def delete_secret(self, key: str) -> bool:
        data = self._load_dict()
        if key in data:
            del data[key]
            self._save_dict(data)
            return True
        return False

    def list_keys(self) -> List[str]:
        return sorted(self._load_dict().keys())


class AESGCMVault(BaseVault):
    """Cross-platform AEAD AES-256-GCM vault with PBKDF2-HMAC-SHA256 key derivation.

    Format of encrypted file:
    [16 bytes salt] + [12 bytes nonce] + [ciphertext + 16 bytes authentication tag]
    """

    ITERATIONS = 100_000
    backend_name = "aes_gcm"

    def __init__(
        self,
        profile_name: str,
        vault_path: Path,
        master_password: Optional[str] = None,
    ) -> None:
        super().__init__(profile_name, vault_path)
        # Decision D8: a password derivable from the profile name is not protection. An explicit master password
        # (supplied by the operator or a test fixture) is mandatory; there is no implicit default.
        if not master_password:
            raise VaultUnavailableError(UNAVAILABLE_MESSAGE)
        self.master_password = master_password

    def _derive_key(self, salt: bytes) -> bytes:
        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=SHA256(),
            length=32,
            salt=salt,
            iterations=self.ITERATIONS,
        )
        return kdf.derive(self.master_password.encode("utf-8"))

    def _load_dict(self) -> Dict[str, str]:
        if not self.vault_path.exists():
            return {}

        raw = self.vault_path.read_bytes()
        if len(raw) < 28:  # 16 salt + 12 nonce
            return {}

        salt = raw[:16]
        nonce = raw[16:28]
        ciphertext = raw[28:]

        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = self._derive_key(salt)
        aesgcm = AESGCM(key)

        try:
            decrypted = aesgcm.decrypt(nonce, ciphertext, associated_data=self.profile_name.encode("utf-8"))
            return json.loads(decrypted.decode("utf-8"))
        except InvalidTag as exc:
            raise VaultDecryptionError("AEAD AES-GCM decryption failed: invalid tag or wrong password") from exc
        except Exception as exc:
            raise VaultError(f"Failed to decode vault payload: {exc}") from exc

    def _save_dict(self, data: Dict[str, str]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = self._derive_key(salt)
        aesgcm = AESGCM(key)

        plain_bytes = json.dumps(data, indent=2).encode("utf-8")
        ciphertext = aesgcm.encrypt(nonce, plain_bytes, associated_data=self.profile_name.encode("utf-8"))

        blob = salt + nonce + ciphertext

        temp_fd, temp_path = tempfile.mkstemp(
            dir=str(self.vault_path.parent),
            prefix="vault_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(temp_fd, "wb") as f:
                f.write(blob)
            os.replace(temp_path, self.vault_path)
        except Exception as exc:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise VaultError(f"Failed to save vault: {exc}") from exc

    def set_secret(self, key: str, value: str) -> None:
        data = self._load_dict()
        data[key] = value
        self._save_dict(data)

    def get_secret(self, key: str) -> Optional[str]:
        data = self._load_dict()
        return data.get(key)

    def delete_secret(self, key: str) -> bool:
        data = self._load_dict()
        if key in data:
            del data[key]
            self._save_dict(data)
            return True
        return False

    def list_keys(self) -> List[str]:
        return sorted(self._load_dict().keys())


class UnavailableVault(BaseVault):
    """Placeholder for platforms without secure persistent storage: refuses every operation, writes nothing."""

    available = False
    backend_name = "unavailable"

    def _refuse(self) -> None:
        raise VaultUnavailableError(UNAVAILABLE_MESSAGE)

    def set_secret(self, key: str, value: str) -> None:
        self._refuse()

    def get_secret(self, key: str) -> Optional[str]:
        self._refuse()
        return None

    def delete_secret(self, key: str) -> bool:
        self._refuse()
        return False

    def list_keys(self) -> List[str]:
        self._refuse()
        return []


def create_vault(
    profile_dir: Path,
    profile_name: str,
    backend: str = "auto",
    master_password: Optional[str] = None,
) -> BaseVault:
    """Factory creating the appropriate Vault instance based on OS or explicit configuration.

    "auto": DPAPI on Windows. Elsewhere, AES-GCM only with an explicit master password; without one there is no
    secure system store yet (Keychain / Secret Service are planned for 4D.6), so an UnavailableVault is returned
    and every operation fails with a clear VaultUnavailableError (decision D8). Nothing is written to disk.
    """
    vault_file = profile_dir / "vault.enc"
    if backend == "auto":
        if sys.platform == "win32":
            backend = "dpapi"
        elif master_password:
            backend = "aes_gcm"
        else:
            return UnavailableVault(profile_name=profile_name, vault_path=vault_file)

    if backend == "dpapi":
        return Win32DPAPIVault(profile_name=profile_name, vault_path=vault_file)
    elif backend == "aes_gcm":
        return AESGCMVault(
            profile_name=profile_name,
            vault_path=vault_file,
            master_password=master_password,
        )
    else:
        raise ValueError(f"Unsupported vault backend: {backend}")
