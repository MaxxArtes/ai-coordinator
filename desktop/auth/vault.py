"""Secure Credential and Cryptographic Key Vault for AI Coordinator Desktop (Marco 4D.1.3).

Provides OS-native encryption via Windows DPAPI with per-profile entropy and
portable AEAD AES-256-GCM fallback with cryptographic key derivation.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .file_lock import FileLockTimeoutError, interprocess_lock, retry_sharing_violation

LOCK_TIMEOUT = 10.0


class VaultError(Exception):
    """Base exception for secure vault operations."""


class VaultDecryptionError(VaultError):
    """Raised when vault data cannot be decrypted (e.g. wrong key, tampered data, cross-profile)."""


class VaultConflictError(VaultError):
    """The vault changed after the operator inspected it, or replacement was not confirmed."""


class VaultExistsError(VaultConflictError):
    """A create-only write found the name already stored; replacing it needs the confirmed revision."""


class VaultBusyError(VaultError):
    """Another process held the vault lock for too long; nothing was read or changed."""


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
    def delete_secret(self, key: str, expected_revision: Optional[str] = None) -> bool:
        """Remove a secret from the vault; with `expected_revision`, only from that exact revision."""

    @abc.abstractmethod
    def list_keys(self) -> List[str]:
        """List all secret keys stored in the vault."""

    def get_keys_snapshot(self, lock_timeout: Optional[float] = None) -> Dict[str, Any]:
        """Return key names and an opaque ciphertext revision without exposing values."""
        raise VaultError("Esta implementação de cofre não oferece confirmação atômica.")

    def set_secret_conditional(self, key: str, value: str, expected_revision: Optional[str] = None) -> None:
        """Create only when absent, or replace only the revision the operator confirmed."""
        raise VaultError("Esta implementação de cofre não oferece confirmação atômica.")

    def has_secret(self, key: str) -> bool:
        """Check if a secret key exists."""
        return key in self.list_keys()


def _validated_payload(raw: bytes) -> Dict[str, str]:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
            raise ValueError("invalid vault payload")
        return data
    except Exception:
        # Never echo JSON, plaintext, backend exception text or the credential that failed to decode.
        raise VaultError("O conteúdo do cofre é inválido; nenhuma credencial foi alterada.") from None


class _LockedVault(BaseVault):
    """Shared transaction boundary for both encrypted formats; legacy callers participate in the same lock."""

    @contextmanager
    def _locked(self, timeout: Optional[float] = None) -> Iterator[None]:
        try:
            with interprocess_lock(self.vault_path.with_name(self.vault_path.name + ".lock"), timeout=timeout or LOCK_TIMEOUT):
                yield
        except VaultError:
            raise
        except FileLockTimeoutError:
            # Busy is not unreadable: the vault is intact and another window holds it.
            raise VaultBusyError("Outra janela está usando o cofre. Tente novamente.") from None
        except Exception:
            raise VaultError("Não foi possível acessar o cofre com segurança; nenhuma alteração foi confirmada.") from None

    def _read_vault_bytes(self) -> bytes:
        return retry_sharing_violation(self.vault_path.read_bytes)

    def _revision(self) -> str:
        try:
            return hashlib.sha256(self._read_vault_bytes()).hexdigest()
        except FileNotFoundError:
            return "absent"

    def _atomic_write(self, encrypted_bytes: bytes) -> None:
        if not encrypted_bytes:
            raise VaultError("Não foi possível cifrar o cofre; nenhuma credencial foi alterada.")
        temp_path = None
        try:
            self.vault_path.parent.mkdir(parents=True, exist_ok=True)
            temp_fd, temp_path = tempfile.mkstemp(dir=str(self.vault_path.parent), prefix="vault_", suffix=".tmp")
            with os.fdopen(temp_fd, "wb") as stream:
                stream.write(encrypted_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            retry_sharing_violation(lambda: os.replace(temp_path, self.vault_path))
        except Exception:
            raise VaultError("Não foi possível gravar o cofre; a versão anterior foi preservada.") from None
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def set_secret(self, key: str, value: str) -> None:
        with self._locked():
            data = self._load_dict()
            if not isinstance(key, str) or not isinstance(value, str):
                raise VaultError("Nome e valor da credencial devem ser texto.")
            data[key] = value
            self._save_dict(data)

    def set_secret_conditional(self, key: str, value: str, expected_revision: Optional[str] = None) -> None:
        with self._locked():
            data = self._load_dict()
            if not isinstance(key, str) or not isinstance(value, str):
                raise VaultError("Nome e valor da credencial devem ser texto.")
            if expected_revision is None:
                if key in data:
                    raise VaultExistsError("Já existe uma credencial com este nome. Recarregue e confirme a substituição.")
                valid = True
            else:
                valid = (
                    isinstance(expected_revision, str)
                    and len(expected_revision) == 64
                    and all(c in "0123456789abcdef" for c in expected_revision)
                    and key in data
                    and hmac.compare_digest(expected_revision, self._revision())
                )
            if not valid:
                raise VaultConflictError("O cofre mudou ou a substituição não foi confirmada. Recarregue e confirme novamente.")
            data[key] = value
            self._save_dict(data)

    def get_secret(self, key: str) -> Optional[str]:
        with self._locked():
            return self._load_dict().get(key)

    def delete_secret(self, key: str, expected_revision: Optional[str] = None) -> bool:
        """Without a revision, remove `key` if present. With one, remove it only when the vault is still that exact
        revision (the list the operator saw); any other revision raises VaultConflictError and nothing is removed."""
        with self._locked():
            data = self._load_dict()
            if expected_revision is not None and not (
                isinstance(expected_revision, str)
                and len(expected_revision) == 64
                and all(c in "0123456789abcdef" for c in expected_revision)
                and hmac.compare_digest(expected_revision, self._revision())
            ):
                raise VaultConflictError("O cofre mudou desde a conferência. Nada foi removido.")
            if key not in data:
                return False
            del data[key]
            self._save_dict(data)
            return True

    def get_keys_snapshot(self, lock_timeout: Optional[float] = None) -> Dict[str, Any]:
        with self._locked(lock_timeout):
            data = self._load_dict()
            return {"keys": sorted(data), "revision": self._revision()}

    def list_keys(self) -> List[str]:
        return self.get_keys_snapshot()["keys"]


class Win32DPAPIVault(_LockedVault):
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
        try:
            encrypted_bytes = self._read_vault_bytes()
        except FileNotFoundError:
            return {}
        except Exception:
            raise VaultError("Não foi possível ler o cofre; nenhuma credencial foi alterada.") from None
        if not encrypted_bytes:
            raise VaultDecryptionError("O arquivo do cofre está vazio ou truncado; nenhuma credencial foi alterada.")
        try:
            decrypted_bytes = self._unprotect(encrypted_bytes)
        except Exception:
            raise VaultDecryptionError("Não foi possível decifrar o cofre; nenhuma credencial foi alterada.") from None
        return _validated_payload(decrypted_bytes)

    def _save_dict(self, data: Dict[str, str]) -> None:
        try:
            encrypted_bytes = self._protect(json.dumps(data, indent=2).encode("utf-8"))
        except Exception:
            raise VaultError("Não foi possível cifrar o cofre; nenhuma credencial foi alterada.") from None
        self._atomic_write(encrypted_bytes)


class AESGCMVault(_LockedVault):
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
        try:
            raw = self._read_vault_bytes()
        except FileNotFoundError:
            return {}
        except Exception:
            raise VaultError("Não foi possível ler o cofre; nenhuma credencial foi alterada.") from None
        if len(raw) < 44:  # Salt + nonce + authentication tag; an existing truncated file is never an empty vault.
            raise VaultDecryptionError("O arquivo do cofre está vazio ou truncado; nenhuma credencial foi alterada.")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            salt, nonce, ciphertext = raw[:16], raw[16:28], raw[28:]
            plaintext = AESGCM(self._derive_key(salt)).decrypt(nonce, ciphertext, associated_data=self.profile_name.encode("utf-8"))
        except Exception:
            raise VaultDecryptionError("Não foi possível decifrar o cofre; nenhuma credencial foi alterada.") from None
        return _validated_payload(plaintext)

    def _save_dict(self, data: Dict[str, str]) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        try:
            salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
            plain_bytes = json.dumps(data, indent=2).encode("utf-8")
            ciphertext = AESGCM(self._derive_key(salt)).encrypt(nonce, plain_bytes, associated_data=self.profile_name.encode("utf-8"))
        except Exception:
            raise VaultError("Não foi possível cifrar o cofre; nenhuma credencial foi alterada.") from None
        self._atomic_write(salt + nonce + ciphertext)


class UnavailableVault(BaseVault):
    """Placeholder for platforms without secure persistent storage: refuses every operation, writes nothing."""

    available = False
    backend_name = "unavailable"

    def _refuse(self) -> None:
        raise VaultUnavailableError(UNAVAILABLE_MESSAGE)

    def set_secret(self, key: str, value: str) -> None:
        self._refuse()

    def set_secret_conditional(self, key: str, value: str, expected_revision: Optional[str] = None) -> None:
        self._refuse()

    def get_keys_snapshot(self, lock_timeout: Optional[float] = None) -> Dict[str, Any]:
        self._refuse()
        return {"keys": [], "revision": "unavailable"}

    def get_secret(self, key: str) -> Optional[str]:
        self._refuse()
        return None

    def delete_secret(self, key: str, expected_revision: Optional[str] = None) -> bool:
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
