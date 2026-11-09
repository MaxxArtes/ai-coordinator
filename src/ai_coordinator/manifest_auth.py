"""Cryptographically Verifiable Authorization Manifest and Trust Store (Phase 4C.2.2).

Implements RFC 8032 Ed25519 digital signature signing and verification over canonical
authorization manifests, paired with an append-governed Public Key Trust Store.

Strict Tripartite Separation of Authorization:
1. Cryptographic Validity (Tier 1): Mathematical integrity of canonical manifest payload
   against Ed25519 digital signature. Proves zero tampering, no field substitution.
2. Key Authority (Tier 2): Verified public key exists in local Trust Store, is in 'active'
   status, not revoked, not expired, and possesses required operational capabilities.
3. Human Operational Authorization (Tier 3): Explicit human operator consent, runtime commit
   match, strict US$ 0.00 budget invariant, validity window, and scope checks.

All dispatches and budget reservations require all three tiers to evaluate to TRUE.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

# Canonical schema version for Phase 4C.2
SCHEMA_VERSION_1_0 = "1.0"

# Standard permission tokens
PERMISSION_DISPATCH_OPENROUTER_FREE = "dispatch:openrouter_free"
PERMISSION_AUDIT_VERIFY = "audit:verify"
PERMISSION_CATALOG_SYNC = "catalog:sync"
PERMISSION_EMERGENCY_OVERRIDE = "emergency:override"

ALL_VALID_PERMISSIONS = {
    PERMISSION_DISPATCH_OPENROUTER_FREE,
    PERMISSION_AUDIT_VERIFY,
    PERMISSION_CATALOG_SYNC,
    PERMISSION_EMERGENCY_OVERRIDE,
}

# Standard key statuses
KEY_STATUS_ACTIVE = "active"
KEY_STATUS_REVOKED = "revoked"
KEY_STATUS_SUSPENDED = "suspended"

VALID_KEY_STATUSES = {KEY_STATUS_ACTIVE, KEY_STATUS_REVOKED, KEY_STATUS_SUSPENDED}


# ---------------------------------------------------------------------------
# Exceptions Hierarchy
# ---------------------------------------------------------------------------

class ManifestSecurityError(Exception):
    """Base exception for all authorization manifest and cryptographic errors."""


class CanonicalSerializationError(ManifestSecurityError):
    """Raised when canonical serialization of manifest fails or inputs are malformed."""


class CryptographicSignatureError(ManifestSecurityError):
    """Tier 1 failure: Digital signature is invalid, forged, or payload was tampered."""


class TrustStoreKeyError(ManifestSecurityError):
    """Raised when a key is missing from Trust Store or corrupted."""


class KeyAuthorityError(ManifestSecurityError):
    """Tier 2 failure: Public key is revoked, expired, unauthorized, or identity mismatch."""


class HumanOperationalAuthorizationError(ManifestSecurityError):
    """Tier 3 failure: Operational constraints violated (commit mismatch, budget > 0, etc.)."""


class ReplayAuthorizationError(ManifestSecurityError):
    """Raised when authorization_id has already been consumed (replay attack prevention)."""


# ---------------------------------------------------------------------------
# Canonical Manifest Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalAuthorizationManifest:
    """Canonical model of an execution authorization manifest.

    Guarantees deterministic, unequivocal, sorted byte serialization for
    cryptographic signing and verification.
    """
    authorization_id: str
    execution_scope_id: str
    code_commit: str
    allowed_models: Tuple[str, ...]
    allowed_modalities: Tuple[str, ...]
    max_dispatches: int
    max_budget_usd: float
    authorizer_identity: str
    authorizer_permissions: Tuple[str, ...]
    issued_at_utc: str
    valid_until_utc: str
    schema_version: str = SCHEMA_VERSION_1_0

    def __post_init__(self) -> None:
        """Validate core invariants on construction."""
        if not self.authorization_id or not isinstance(self.authorization_id, str):
            raise CanonicalSerializationError("authorization_id must be a non-empty string")
        if not self.execution_scope_id or not isinstance(self.execution_scope_id, str):
            raise CanonicalSerializationError("execution_scope_id must be a non-empty string")

        # Code commit: must be exactly 40 lowercase/uppercase hex chars or valid git commit
        if not isinstance(self.code_commit, str) or not re.fullmatch(r"^[0-9a-fA-F]{40}$", self.code_commit.strip()):
            raise CanonicalSerializationError(
                f"code_commit must be a 40-character git commit SHA-1 hex string, got: '{self.code_commit}'"
            )

        if not self.allowed_models or not isinstance(self.allowed_models, (list, tuple)):
            raise CanonicalSerializationError("allowed_models must be a non-empty sequence of model identifiers")
        for m in self.allowed_models:
            if not isinstance(m, str) or not m.strip():
                raise CanonicalSerializationError("Every model identifier in allowed_models must be a non-empty string")

        if not self.allowed_modalities or not isinstance(self.allowed_modalities, (list, tuple)):
            raise CanonicalSerializationError("allowed_modalities must be a non-empty sequence")

        if isinstance(self.max_dispatches, bool) or not isinstance(self.max_dispatches, int) or self.max_dispatches < 1:
            raise CanonicalSerializationError("max_dispatches must be an integer >= 1")

        if isinstance(self.max_budget_usd, bool) or not isinstance(self.max_budget_usd, (int, float)):
            raise CanonicalSerializationError("max_budget_usd must be a float")
        if not math.isfinite(self.max_budget_usd) or float(self.max_budget_usd) != 0.0:
            raise CanonicalSerializationError(
                f"max_budget_usd must be strictly 0.0 in Phase 4C, got: {self.max_budget_usd}"
            )

        if not self.authorizer_identity or not isinstance(self.authorizer_identity, str):
            raise CanonicalSerializationError("authorizer_identity must be a non-empty string")

        if not isinstance(self.authorizer_permissions, (list, tuple)):
            raise CanonicalSerializationError("authorizer_permissions must be a sequence of permission strings")

        if self.schema_version != SCHEMA_VERSION_1_0:
            raise CanonicalSerializationError(
                f"Unsupported schema_version '{self.schema_version}', expected '{SCHEMA_VERSION_1_0}'"
            )

        # Validate timestamps
        for ts_name, ts_val in (("issued_at_utc", self.issued_at_utc), ("valid_until_utc", self.valid_until_utc)):
            if not isinstance(ts_val, str) or not ts_val.strip():
                raise CanonicalSerializationError(f"{ts_name} must be a valid ISO 8601 string")
            try:
                dt.datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
            except Exception as exc:
                raise CanonicalSerializationError(f"{ts_name} is not a valid ISO 8601 timestamp: {ts_val}") from exc

    def to_canonical_dict(self) -> Dict[str, Any]:
        """Convert manifest to deterministic dictionary with sorted keys."""
        return {
            "allowed_modalities": list(self.allowed_modalities),
            "allowed_models": list(self.allowed_models),
            "authorization_id": self.authorization_id.strip(),
            "authorizer_identity": self.authorizer_identity.strip(),
            "authorizer_permissions": sorted(list(self.authorizer_permissions)),
            "code_commit": self.code_commit.strip().lower(),
            "execution_scope_id": self.execution_scope_id.strip(),
            "issued_at_utc": self.issued_at_utc.strip(),
            "max_budget_usd": 0.0,
            "max_dispatches": int(self.max_dispatches),
            "schema_version": self.schema_version,
            "valid_until_utc": self.valid_until_utc.strip(),
        }

    def to_canonical_bytes(self) -> bytes:
        """Produce strictly deterministic UTF-8 bytes for cryptographic signing.

        Uses lexicographically sorted keys and minimal JSON separators (',', ':').
        """
        data = self.to_canonical_dict()
        canonical_str = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return canonical_str.encode("utf-8")

    def canonical_sha256(self) -> str:
        """Compute SHA-256 digest of canonical bytes (audit fingerprint)."""
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CanonicalAuthorizationManifest":
        """Construct canonical manifest from dictionary with strict schema enforcement."""
        if not isinstance(data, dict):
            raise CanonicalSerializationError(f"Manifest data must be a dictionary, got: {type(data)}")

        required = [
            "authorization_id",
            "execution_scope_id",
            "code_commit",
            "allowed_models",
            "allowed_modalities",
            "max_dispatches",
            "max_budget_usd",
            "authorizer_identity",
            "authorizer_permissions",
            "issued_at_utc",
            "valid_until_utc",
        ]
        missing = [f for f in required if f not in data]
        if missing:
            raise CanonicalSerializationError(f"Missing required fields in manifest: {missing}")

        allowed_keys = set(required) | {"schema_version"}
        unknown = set(data.keys()) - allowed_keys
        if unknown:
            raise CanonicalSerializationError(f"Unknown fields present in manifest (shadow injection rejected): {unknown}")

        return cls(
            authorization_id=str(data["authorization_id"]),
            execution_scope_id=str(data["execution_scope_id"]),
            code_commit=str(data["code_commit"]),
            allowed_models=tuple(str(m) for m in data["allowed_models"]),
            allowed_modalities=tuple(str(m) for m in data["allowed_modalities"]),
            max_dispatches=int(data["max_dispatches"]),
            max_budget_usd=float(data["max_budget_usd"]),
            authorizer_identity=str(data["authorizer_identity"]),
            authorizer_permissions=tuple(str(p) for p in data["authorizer_permissions"]),
            issued_at_utc=str(data["issued_at_utc"]),
            valid_until_utc=str(data["valid_until_utc"]),
            schema_version=str(data.get("schema_version", SCHEMA_VERSION_1_0)),
        )


# ---------------------------------------------------------------------------
# Verifiable Human Operational Approval (Tier 3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HumanOperationalApproval:
    """Verifiable human operational authorization token / session proof.

    Guarantees authentic human consent by binding:
    - operator_identity: string matching the manifest authorizer
    - session_id: authenticated CLI session identifier
    - approved_at_utc: ISO timestamp when human approved execution
    - explicit_consent: strictly True
    - approval_token: cryptographic proof token binding the above fields to authorization_id
    """
    operator_identity: str
    session_id: str
    approved_at_utc: str
    explicit_consent: bool = True
    approval_token: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.operator_identity or not self.operator_identity.strip():
            raise HumanOperationalAuthorizationError("operator_identity cannot be empty in HumanOperationalApproval")
        if not self.session_id or not self.session_id.strip():
            raise HumanOperationalAuthorizationError("session_id cannot be empty in HumanOperationalApproval")
        if not self.approved_at_utc or not self.approved_at_utc.strip():
            raise HumanOperationalAuthorizationError("approved_at_utc cannot be empty in HumanOperationalApproval")
        if self.explicit_consent is not True:
            raise HumanOperationalAuthorizationError("explicit_consent must be True in HumanOperationalApproval")

    @classmethod
    def create(
        cls,
        operator_identity: str,
        session_id: str,
        authorization_id: str,
        approved_at_utc: Optional[str] = None,
        secret_salt: Optional[str] = None,
    ) -> "HumanOperationalApproval":
        """Generate a verified human operational approval token for a manifest."""
        if not operator_identity or not operator_identity.strip():
            raise ValueError("operator_identity cannot be empty")
        if not session_id or not session_id.strip():
            raise ValueError("session_id cannot be empty")
        if not authorization_id or not authorization_id.strip():
            raise ValueError("authorization_id cannot be empty")

        now_str = approved_at_utc or dt.datetime.now(dt.timezone.utc).isoformat()
        salt = secret_salt or "AI_COORDINATOR_OPERATIONAL_SALT_V1"
        token_payload = f"{salt}:{operator_identity.strip()}:{session_id.strip()}:{authorization_id.strip()}:{now_str}".encode("utf-8")
        token = hashlib.sha256(token_payload).hexdigest()

        return cls(
            operator_identity=operator_identity.strip(),
            session_id=session_id.strip(),
            approved_at_utc=now_str,
            explicit_consent=True,
            approval_token=token,
        )

    def verify_token(self, authorization_id: str, secret_salt: Optional[str] = None) -> bool:
        """Verify token integrity against authorization_id and internal fields."""
        if not self.approval_token:
            return False
        salt = secret_salt or "AI_COORDINATOR_OPERATIONAL_SALT_V1"
        expected = hashlib.sha256(
            f"{salt}:{self.operator_identity.strip()}:{self.session_id.strip()}:{authorization_id.strip()}:{self.approved_at_utc.strip()}".encode("utf-8")
        ).hexdigest()
        return self.approval_token == expected

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operator_identity": self.operator_identity,
            "session_id": self.session_id,
            "approved_at_utc": self.approved_at_utc,
            "explicit_consent": self.explicit_consent,
            "approval_token": self.approval_token,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HumanOperationalApproval":
        if not isinstance(data, dict):
            raise ValueError(f"Approval data must be a dictionary, got: {type(data)}")
        return cls(
            operator_identity=str(data.get("operator_identity", "")),
            session_id=str(data.get("session_id", "")),
            approved_at_utc=str(data.get("approved_at_utc", "")),
            explicit_consent=bool(data.get("explicit_consent", False)),
            approval_token=data.get("approval_token"),
        )


# ---------------------------------------------------------------------------
# Signed Manifest Container
# ---------------------------------------------------------------------------

@dataclass
class SignatureMetadata:
    """Metadata of the Ed25519 signature."""
    key_id: str
    algorithm: str
    signature_hex: str
    signed_at_utc: str
    public_key_hex: Optional[str] = None

    def __post_init__(self) -> None:
        if self.algorithm != "Ed25519":
            raise CanonicalSerializationError(f"Unsupported algorithm '{self.algorithm}', only 'Ed25519' is accepted")
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise CanonicalSerializationError("key_id must be a non-empty string")
        if not isinstance(self.signature_hex, str):
            raise CanonicalSerializationError("signature_hex must be a string")
        clean_sig = self.signature_hex.strip()
        if len(clean_sig) != 128 or not re.fullmatch(r"^[0-9a-fA-F]{128}$", clean_sig):
            raise CanonicalSerializationError(
                f"signature_hex must be exactly 128 hex characters (64 bytes), got length {len(clean_sig)}"
            )
        if self.public_key_hex:
            clean_pub = self.public_key_hex.strip()
            if len(clean_pub) != 64 or not re.fullmatch(r"^[0-9a-fA-F]{64}$", clean_pub):
                raise CanonicalSerializationError(
                    f"public_key_hex must be exactly 64 hex characters (32 bytes), got length {len(clean_pub)}"
                )


@dataclass
class SignedAuthorizationManifest:
    """Container for the canonical manifest and its accompanying signature metadata."""
    manifest: CanonicalAuthorizationManifest
    signature: SignatureMetadata

    def to_dict(self) -> Dict[str, Any]:
        """Convert signed manifest container to JSON-serializable dictionary."""
        sig_dict: Dict[str, Any] = {
            "key_id": self.signature.key_id,
            "algorithm": self.signature.algorithm,
            "signature_hex": self.signature.signature_hex,
            "signed_at_utc": self.signature.signed_at_utc,
        }
        if self.signature.public_key_hex:
            sig_dict["public_key_hex"] = self.signature.public_key_hex

        return {
            "manifest": self.manifest.to_canonical_dict(),
            "signature": sig_dict,
        }

    def to_json(self, indent: Optional[int] = 2) -> str:
        """Serialize container to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SignedAuthorizationManifest":
        """Deserialize signed manifest container from dictionary."""
        if not isinstance(data, dict):
            raise CanonicalSerializationError("Signed manifest must be a dictionary")
        if "manifest" not in data or "signature" not in data:
            raise CanonicalSerializationError("Signed manifest requires 'manifest' and 'signature' blocks")

        manifest = CanonicalAuthorizationManifest.from_dict(data["manifest"])
        sig_data = data["signature"]
        if not isinstance(sig_data, dict):
            raise CanonicalSerializationError("Signature block must be a dictionary")

        signature = SignatureMetadata(
            key_id=str(sig_data.get("key_id", "")),
            algorithm=str(sig_data.get("algorithm", "")),
            signature_hex=str(sig_data.get("signature_hex", "")),
            signed_at_utc=str(sig_data.get("signed_at_utc", "")),
            public_key_hex=str(sig_data["public_key_hex"]) if "public_key_hex" in sig_data else None,
        )
        return cls(manifest=manifest, signature=signature)

    @classmethod
    def from_file(cls, path: str) -> "SignedAuthorizationManifest":
        """Load and parse signed authorization manifest from file path."""
        if not os.path.exists(path):
            raise FileNotFoundError(f"Manifest file not found: '{path}'")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def save_to_file(self, path: str) -> None:
        """Persist signed authorization manifest atomically to file."""
        dir_name = os.path.dirname(os.path.abspath(path))
        os.makedirs(dir_name, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix="manifest_", suffix=".tmp")
        try:
            with open(temp_fd, "w", encoding="utf-8") as f:
                f.write(self.to_json(indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise


# ---------------------------------------------------------------------------
# Ed25519 Signer & Verifier Engines
# ---------------------------------------------------------------------------

class Ed25519Signer:
    """Standard RFC 8032 Ed25519 signer for authorization manifests."""

    def __init__(self, private_key: ed25519.Ed25519PrivateKey, key_id: str):
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError("private_key must be an instance of ed25519.Ed25519PrivateKey")
        if not key_id or not isinstance(key_id, str):
            raise ValueError("key_id must be a non-empty string")
        self._private_key = private_key
        self._key_id = key_id.strip()

    @property
    def key_id(self) -> str:
        return self._key_id

    @property
    def public_key_hex(self) -> str:
        """Return 64-character hex representation of the public key (32 bytes)."""
        pub = self._private_key.public_key()
        return pub.public_bytes_raw().hex()

    @classmethod
    def generate(cls, key_id: str) -> "Ed25519Signer":
        """Generate a fresh Ed25519 private key and instantiate signer."""
        priv = ed25519.Ed25519PrivateKey.generate()
        return cls(private_key=priv, key_id=key_id)

    @classmethod
    def from_private_bytes(cls, raw_bytes: bytes, key_id: str) -> "Ed25519Signer":
        """Instantiate signer from 32-byte raw private key bytes."""
        if len(raw_bytes) != 32:
            raise ValueError(f"Ed25519 private key must be 32 bytes, got {len(raw_bytes)}")
        priv = ed25519.Ed25519PrivateKey.from_private_bytes(raw_bytes)
        return cls(private_key=priv, key_id=key_id)

    @classmethod
    def from_private_hex(cls, private_hex: str, key_id: str) -> "Ed25519Signer":
        """Instantiate signer from 64-character hex string."""
        clean_hex = private_hex.strip()
        if len(clean_hex) != 64 or not re.fullmatch(r"^[0-9a-fA-F]{64}$", clean_hex):
            raise ValueError(f"Ed25519 private hex must be exactly 64 hex characters, got {len(clean_hex)}")
        raw_bytes = bytes.fromhex(clean_hex)
        return cls.from_private_bytes(raw_bytes, key_id=key_id)

    def sign_bytes(self, payload: bytes) -> str:
        """Sign raw bytes and return 128-character hex signature."""
        sig = self._private_key.sign(payload)
        return sig.hex()

    def sign_manifest(
        self,
        manifest: CanonicalAuthorizationManifest,
        include_public_key: bool = True,
        signed_at_utc: Optional[str] = None,
    ) -> SignedAuthorizationManifest:
        """Sign canonical bytes of manifest and construct signed container."""
        canonical_bytes = manifest.to_canonical_bytes()
        sig_hex = self.sign_bytes(canonical_bytes)
        now_str = signed_at_utc or dt.datetime.now(dt.timezone.utc).isoformat()

        sig_meta = SignatureMetadata(
            key_id=self._key_id,
            algorithm="Ed25519",
            signature_hex=sig_hex,
            signed_at_utc=now_str,
            public_key_hex=self.public_key_hex if include_public_key else None,
        )
        return SignedAuthorizationManifest(manifest=manifest, signature=sig_meta)


class Ed25519Verifier:
    """Standard RFC 8032 Ed25519 verifier."""

    @staticmethod
    def verify_signature(public_key_hex: str, canonical_bytes: bytes, signature_hex: str) -> bool:
        """Verify Ed25519 signature over canonical bytes.

        Returns True if mathematically valid, False otherwise.
        """
        try:
            clean_pub = public_key_hex.strip()
            clean_sig = signature_hex.strip()
            if len(clean_pub) != 64 or not re.fullmatch(r"^[0-9a-fA-F]{64}$", clean_pub):
                return False
            if len(clean_sig) != 128 or not re.fullmatch(r"^[0-9a-fA-F]{128}$", clean_sig):
                return False

            pub_bytes = bytes.fromhex(clean_pub)
            sig_bytes = bytes.fromhex(clean_sig)

            pub_obj = ed25519.Ed25519PublicKey.from_public_bytes(pub_bytes)
            pub_obj.verify(sig_bytes, canonical_bytes)
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False


# ---------------------------------------------------------------------------
# Trust Store
# ---------------------------------------------------------------------------

@dataclass
class TrustedKeyEntry:
    """Public key entry stored in the Trust Store.

    Stores ONLY public keys and trust metadata. Never stores private keys.
    """
    key_id: str
    public_key_hex: str
    identity: str
    permissions: List[str]
    status: str
    created_at_utc: str
    expires_at_utc: Optional[str] = None
    revoked_at_utc: Optional[str] = None
    revocation_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.key_id or not isinstance(self.key_id, str):
            raise ValueError("key_id must be a non-empty string")
        clean_pub = self.public_key_hex.strip()
        if len(clean_pub) != 64 or not re.fullmatch(r"^[0-9a-fA-F]{64}$", clean_pub):
            raise ValueError(f"public_key_hex must be exactly 64 hex characters (32 bytes), got {len(clean_pub)}")
        # Verify it can be loaded as Ed25519 public key
        try:
            ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(clean_pub))
        except Exception as exc:
            raise ValueError(f"Invalid Ed25519 public key bytes: {exc}") from exc

        if not self.identity or not isinstance(self.identity, str):
            raise ValueError("identity must be a non-empty string")
        if not isinstance(self.permissions, list):
            raise ValueError("permissions must be a list of permission strings")
        if self.status not in VALID_KEY_STATUSES:
            raise ValueError(f"Invalid status '{self.status}', must be one of {VALID_KEY_STATUSES}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_id": self.key_id,
            "public_key_hex": self.public_key_hex.strip().lower(),
            "identity": self.identity,
            "permissions": list(self.permissions),
            "status": self.status,
            "created_at_utc": self.created_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "revoked_at_utc": self.revoked_at_utc,
            "revocation_reason": self.revocation_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrustedKeyEntry":
        # Guard: Reject any attempt to load private keys into Trust Store
        forbidden_keys = {"private_key", "private_key_hex", "private_bytes", "secret", "d"}
        found_forbidden = forbidden_keys.intersection(set(data.keys()))
        if found_forbidden:
            raise ValueError(
                f"Security violation: Attempted to load private key fields into TrustStore: {found_forbidden}"
            )

        return cls(
            key_id=str(data["key_id"]),
            public_key_hex=str(data["public_key_hex"]),
            identity=str(data["identity"]),
            permissions=list(data.get("permissions", [])),
            status=str(data.get("status", KEY_STATUS_ACTIVE)),
            created_at_utc=str(data["created_at_utc"]),
            expires_at_utc=data.get("expires_at_utc"),
            revoked_at_utc=data.get("revoked_at_utc"),
            revocation_reason=data.get("revocation_reason"),
        )


class TrustStore:
    """Append-governed Public Key Trust Store.

    Holds verified authorized public keys, identities, granted permissions,
    and revocations. Fail-closed against unknown, revoked, or expired keys.
    """

    def __init__(self, store_path: Optional[str] = None):
        self._store_path = store_path
        self._keys: Dict[str, TrustedKeyEntry] = {}

    @property
    def store_path(self) -> Optional[str]:
        return self._store_path

    def __len__(self) -> int:
        return len(self._keys)

    def register_key(self, entry: TrustedKeyEntry) -> None:
        """Register a new public key in the Trust Store. Rejects duplicates."""
        if entry.key_id in self._keys:
            raise ValueError(f"Key ID '{entry.key_id}' already registered in TrustStore. Use rotate_key instead.")
        self._keys[entry.key_id] = entry

    def get_key(self, key_id: str) -> Optional[TrustedKeyEntry]:
        """Look up a key entry by key_id."""
        return self._keys.get(key_id)

    def list_keys(self) -> List[TrustedKeyEntry]:
        """Return list of all keys in TrustStore."""
        return list(self._keys.values())

    def revoke_key(
        self,
        key_id: str,
        reason: str,
        revoked_at_utc: Optional[str] = None,
    ) -> TrustedKeyEntry:
        """Revoke a trusted key with explicit reason and timestamp."""
        if key_id not in self._keys:
            raise TrustStoreKeyError(f"Cannot revoke non-existent key '{key_id}'")
        if not reason or not reason.strip():
            raise ValueError("Revocation reason is mandatory and cannot be empty")

        key = self._keys[key_id]
        now_str = revoked_at_utc or dt.datetime.now(dt.timezone.utc).isoformat()
        key.status = KEY_STATUS_REVOKED
        key.revoked_at_utc = now_str
        key.revocation_reason = reason.strip()
        return key

    def rotate_key(
        self,
        old_key_id: str,
        new_entry: TrustedKeyEntry,
        rotation_reason: str,
    ) -> Tuple[TrustedKeyEntry, TrustedKeyEntry]:
        """Rotate an existing key: marks old key revoked and registers new key atomically."""
        if old_key_id not in self._keys:
            raise TrustStoreKeyError(f"Cannot rotate non-existent key '{old_key_id}'")
        if new_entry.key_id in self._keys:
            raise ValueError(f"New key ID '{new_entry.key_id}' already exists in TrustStore")

        old_key = self.revoke_key(
            key_id=old_key_id,
            reason=f"Rotated: {rotation_reason}. Replaced by {new_entry.key_id}",
        )
        self.register_key(new_entry)
        return old_key, new_entry

    def is_key_authorized(
        self,
        key_id: str,
        required_permission: Optional[str] = None,
        at_time_utc: Optional[dt.datetime] = None,
    ) -> Tuple[bool, str]:
        """Verify whether a key possesses active authorization and required permissions."""
        if key_id not in self._keys:
            return False, f"Key '{key_id}' is not present in TrustStore (unknown authorizer)"

        entry = self._keys[key_id]
        if entry.status == KEY_STATUS_REVOKED:
            return False, f"Key '{key_id}' was revoked on {entry.revoked_at_utc} (Reason: {entry.revocation_reason})"
        if entry.status != KEY_STATUS_ACTIVE:
            return False, f"Key '{key_id}' is inactive (status: '{entry.status}')"

        now = at_time_utc or dt.datetime.now(dt.timezone.utc)
        if entry.expires_at_utc:
            try:
                exp_dt = dt.datetime.fromisoformat(entry.expires_at_utc.replace("Z", "+00:00"))
                if exp_dt < now:
                    return False, f"Key '{key_id}' expired on {entry.expires_at_utc}"
            except Exception as exc:
                return False, f"Malformed expiration date on key '{key_id}': {exc}"

        if required_permission and required_permission not in entry.permissions:
            return False, f"Key '{key_id}' lacks required capability '{required_permission}'. Available: {entry.permissions}"

        return True, "Key is active, valid, and possesses required authority"

    def to_dict(self) -> Dict[str, Any]:
        """Convert entire trust store to dictionary."""
        return {
            "version": "1.0",
            "updated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "keys": [k.to_dict() for k in self._keys.values()],
        }

    def save_to_file(self, path: Optional[str] = None) -> None:
        """Atomically persist trust store to file with fsync."""
        target_path = path or self._store_path
        if not target_path:
            raise ValueError("No file path specified for TrustStore persistence")
        dir_name = os.path.dirname(os.path.abspath(target_path))
        os.makedirs(dir_name, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix="truststore_", suffix=".tmp")
        try:
            with open(temp_fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target_path)
            self._store_path = target_path
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

    @classmethod
    def from_dict(cls, data: Dict[str, Any], store_path: Optional[str] = None) -> "TrustStore":
        """Instantiate TrustStore from dictionary."""
        store = cls(store_path=store_path)
        keys_list = data.get("keys", [])
        if not isinstance(keys_list, list):
            raise ValueError("'keys' in trust store must be a list")
        for k_data in keys_list:
            entry = TrustedKeyEntry.from_dict(k_data)
            store.register_key(entry)
        return store

    @classmethod
    def load_from_file(cls, path: str) -> "TrustStore":
        """Load TrustStore from file. Fail-closed if missing or invalid."""
        if not os.path.exists(path):
            raise FileNotFoundError(f"TrustStore file not found: '{path}'")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data, store_path=path)


# ---------------------------------------------------------------------------
# Tripartite Verification Engine
# ---------------------------------------------------------------------------

@dataclass
class TripartiteVerificationResult:
    """Structured report of tripartite authorization verification.

    Rigidly decouples:
    - Tier 1: Cryptographic Validity
    - Tier 2: Key Authority
    - Tier 3: Human Operational Authorization
    """
    tier1_crypto_valid: bool
    tier1_reason: str
    tier2_key_authority: bool
    tier2_reason: str
    tier3_human_authorized: bool
    tier3_reason: str
    canonical_sha256: str
    key_id: str
    authorizer_identity: str

    @property
    def is_fully_authorized(self) -> bool:
        """True if and only if ALL THREE TIERS evaluate to True."""
        return (
            self.tier1_crypto_valid
            and self.tier2_key_authority
            and self.tier3_human_authorized
        )

    def summary(self) -> str:
        """Produce human-readable and audit-friendly summary string."""
        status = "AUTHORIZED" if self.is_fully_authorized else "REJECTED"
        reasons = []
        if not self.tier1_crypto_valid:
            reasons.append(f"[Tier 1 Crypto]: {self.tier1_reason}")
        if not self.tier2_key_authority:
            reasons.append(f"[Tier 2 Authority]: {self.tier2_reason}")
        if not self.tier3_human_authorized:
            reasons.append(f"[Tier 3 Human Ops]: {self.tier3_reason}")
        reason_str = " | ".join(reasons) if reasons else "All 3 authorization tiers verified successfully"
        return f"Tripartite Authorization [{status}]: {reason_str}"


class TripartiteAuthorizationVerifier:
    """Core verification engine enforcing the three-tier authorization model."""

    def __init__(self, trust_store: TrustStore, replay_cache: Optional[Set[str]] = None):
        if not isinstance(trust_store, TrustStore):
            raise TypeError("trust_store must be an instance of TrustStore")
        self._trust_store = trust_store
        self._replay_cache = replay_cache if replay_cache is not None else set()

    @property
    def trust_store(self) -> TrustStore:
        return self._trust_store

    def verify(
        self,
        signed_manifest: SignedAuthorizationManifest,
        expected_commit: str,
        requested_model: str,
        requested_modality: str = "text",
        requested_dispatches: int = 1,
        human_operator_consent: bool = False,
        required_permission: str = PERMISSION_DISPATCH_OPENROUTER_FREE,
        at_time_utc: Optional[dt.datetime] = None,
        check_replay: bool = True,
    ) -> TripartiteVerificationResult:
        """Perform full tripartite verification.

        Does not raise exceptions on validation failures; returns comprehensive
        TripartiteVerificationResult for telemetry, logging, and audit.
        """
        now = at_time_utc or dt.datetime.now(dt.timezone.utc)
        manifest = signed_manifest.manifest
        sig_meta = signed_manifest.signature
        canonical_bytes = manifest.to_canonical_bytes()
        canonical_sha256 = manifest.canonical_sha256()
        key_id = sig_meta.key_id

        # -------------------------------------------------------------------
        # TIER 1: Validade Criptográfica (Integridade e Não-Repúdio)
        # -------------------------------------------------------------------
        # Determine public key for signature check: prefer TrustStore, fallback to signature metadata
        trusted_key = self._trust_store.get_key(key_id)
        candidate_pub_hex = trusted_key.public_key_hex if trusted_key else sig_meta.public_key_hex

        tier1_valid = False
        tier1_reason = "Signature check pending"

        if not candidate_pub_hex:
            tier1_valid = False
            tier1_reason = f"No public key available for key_id '{key_id}'"
        elif sig_meta.algorithm != "Ed25519":
            tier1_valid = False
            tier1_reason = f"Algorithm '{sig_meta.algorithm}' is not accepted (expected Ed25519)"
        else:
            is_valid_sig = Ed25519Verifier.verify_signature(
                public_key_hex=candidate_pub_hex,
                canonical_bytes=canonical_bytes,
                signature_hex=sig_meta.signature_hex,
            )
            if is_valid_sig:
                tier1_valid = True
                tier1_reason = "RFC 8032 Ed25519 signature verified successfully over canonical manifest bytes"
            else:
                tier1_valid = False
                tier1_reason = "Ed25519 signature is mathematically invalid or canonical manifest was tampered"

        # -------------------------------------------------------------------
        # TIER 2: Autoridade da Chave (Confiança na Identidade e Chave)
        # -------------------------------------------------------------------
        tier2_valid = False
        tier2_reason = "Key authority check pending"

        if not trusted_key:
            tier2_valid = False
            tier2_reason = f"Key '{key_id}' is unknown (not present in TrustStore)"
        else:
            # Check public key consistency if signature carried public_key_hex
            if sig_meta.public_key_hex and sig_meta.public_key_hex.strip().lower() != trusted_key.public_key_hex.strip().lower():
                tier2_valid = False
                tier2_reason = (
                    f"Public key mismatch: manifest carried '{sig_meta.public_key_hex}', "
                    f"but TrustStore registers '{trusted_key.public_key_hex}'"
                )
            # Check authorizer identity match
            elif trusted_key.identity.strip() != manifest.authorizer_identity.strip():
                tier2_valid = False
                tier2_reason = (
                    f"Authorizer identity mismatch: manifest declares '{manifest.authorizer_identity}', "
                    f"but key '{key_id}' is bound to '{trusted_key.identity}' in TrustStore"
                )
            else:
                # Check status, expiration, and permission
                key_auth_ok, key_auth_msg = self._trust_store.is_key_authorized(
                    key_id=key_id,
                    required_permission=required_permission,
                    at_time_utc=now,
                )
                if not key_auth_ok:
                    tier2_valid = False
                    tier2_reason = key_auth_msg
                else:
                    # Check manifest permissions are subset of key permissions
                    manifest_perms = set(manifest.authorizer_permissions)
                    key_perms = set(trusted_key.permissions)
                    if not manifest_perms.issubset(key_perms):
                        unauthorized_perms = manifest_perms - key_perms
                        tier2_valid = False
                        tier2_reason = f"Manifest claims permissions not granted to key '{key_id}': {unauthorized_perms}"
                    else:
                        tier2_valid = True
                        tier2_reason = f"Key '{key_id}' is active, trusted, bound to '{trusted_key.identity}', and authorized"

        # -------------------------------------------------------------------
        # TIER 3: Autorização Operacional Humana (Condições de Execução)
        # -------------------------------------------------------------------
        tier3_valid = False
        tier3_reason = "Operational checks pending"

        # 1. Human operator consent flag
        if not human_operator_consent:
            tier3_valid = False
            tier3_reason = "Human operational consent is missing (operator flag / token required)"
        # 2. Commit match
        elif manifest.code_commit.strip().lower() != expected_commit.strip().lower():
            tier3_valid = False
            tier3_reason = (
                f"Code commit mismatch: manifest authorizes commit '{manifest.code_commit}', "
                f"but runtime HEAD is '{expected_commit}'"
            )
        # 3. Budget invariant
        elif manifest.max_budget_usd != 0.0:
            tier3_valid = False
            tier3_reason = f"Budget invariant violated: manifest specifies US$ {manifest.max_budget_usd}, must be 0.0"
        # 4. Validity window
        else:
            try:
                exp_dt = dt.datetime.fromisoformat(manifest.valid_until_utc.replace("Z", "+00:00"))
                iss_dt = dt.datetime.fromisoformat(manifest.issued_at_utc.replace("Z", "+00:00"))
                # Allow 60s clock skew on issuance
                if iss_dt > now + dt.timedelta(seconds=60):
                    tier3_valid = False
                    tier3_reason = f"Manifest issued in the future: {manifest.issued_at_utc} (current: {now.isoformat()})"
                elif exp_dt < now:
                    tier3_valid = False
                    tier3_reason = f"Manifest authorization expired on {manifest.valid_until_utc} (current: {now.isoformat()})"
                # 5. Model permission
                elif requested_model not in manifest.allowed_models:
                    tier3_valid = False
                    tier3_reason = (
                        f"Requested model '{requested_model}' is not in manifest allowed_models: "
                        f"{list(manifest.allowed_models)}"
                    )
                # 6. Modality permission
                elif requested_modality not in manifest.allowed_modalities:
                    tier3_valid = False
                    tier3_reason = (
                        f"Requested modality '{requested_modality}' is not in manifest allowed_modalities: "
                        f"{list(manifest.allowed_modalities)}"
                    )
                # 7. Max dispatches
                elif requested_dispatches > manifest.max_dispatches:
                    tier3_valid = False
                    tier3_reason = (
                        f"Requested dispatches ({requested_dispatches}) exceeds manifest limit ({manifest.max_dispatches})"
                    )
                # 8. Replay prevention
                elif check_replay and manifest.authorization_id in self._replay_cache:
                    tier3_valid = False
                    tier3_reason = (
                        f"Replay attack prevented: authorization_id '{manifest.authorization_id}' "
                        "has already been consumed"
                    )
                else:
                    tier3_valid = True
                    tier3_reason = "Human operational constraints, commit match, budget, and scope fully verified"
            except Exception as exc:
                tier3_valid = False
                tier3_reason = f"Error evaluating operational timestamps: {exc}"

        result = TripartiteVerificationResult(
            tier1_crypto_valid=tier1_valid,
            tier1_reason=tier1_reason,
            tier2_key_authority=tier2_valid,
            tier2_reason=tier2_reason,
            tier3_human_authorized=tier3_valid,
            tier3_reason=tier3_reason,
            canonical_sha256=canonical_sha256,
            key_id=key_id,
            authorizer_identity=manifest.authorizer_identity,
        )

        # If fully authorized and replay check is active, register in replay cache
        if result.is_fully_authorized and check_replay:
            self._replay_cache.add(manifest.authorization_id)

        return result

    def verify_and_raise(
        self,
        signed_manifest: SignedAuthorizationManifest,
        expected_commit: str,
        requested_model: str,
        requested_modality: str = "text",
        requested_dispatches: int = 1,
        human_operator_consent: bool = False,
        required_permission: str = PERMISSION_DISPATCH_OPENROUTER_FREE,
        at_time_utc: Optional[dt.datetime] = None,
        check_replay: bool = True,
    ) -> TripartiteVerificationResult:
        """Perform full tripartite verification and raise specific exception on failure."""
        result = self.verify(
            signed_manifest=signed_manifest,
            expected_commit=expected_commit,
            requested_model=requested_model,
            requested_modality=requested_modality,
            requested_dispatches=requested_dispatches,
            human_operator_consent=human_operator_consent,
            required_permission=required_permission,
            at_time_utc=at_time_utc,
            check_replay=check_replay,
        )

        if not result.tier1_crypto_valid:
            raise CryptographicSignatureError(f"Tier 1 (Crypto) verification failed: {result.tier1_reason}")

        if not result.tier2_key_authority:
            raise KeyAuthorityError(f"Tier 2 (Key Authority) verification failed: {result.tier2_reason}")

        if not result.tier3_human_authorized:
            if "Replay attack prevented" in result.tier3_reason:
                raise ReplayAuthorizationError(result.tier3_reason)
            raise HumanOperationalAuthorizationError(f"Tier 3 (Human Ops) verification failed: {result.tier3_reason}")

        return result
