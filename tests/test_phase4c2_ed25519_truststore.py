"""Comprehensive Offline Adversarial Test Suite for Ed25519 & Trust Store (Phase 4C.2.2).

Validates the verifiable cryptographic authorization architecture:
1. Deterministic canonical JSON serialization.
2. Pure Ed25519 RFC 8032 signing and verification (via cryptography library).
3. Append-governed Trust Store lifecycle (creation, rotation, revocation, expiration, permissions).
4. Strict Tripartite Separation:
   - Tier 1: Cryptographic Validity
   - Tier 2: Key Authority
   - Tier 3: Human Operational Authorization
5. Anti-Replay protection and fail-closed security gates.

All tests execute strictly offline with zero external network access.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

from ai_coordinator.manifest_auth import (
    KEY_STATUS_ACTIVE,
    KEY_STATUS_REVOKED,
    KEY_STATUS_SUSPENDED,
    PERMISSION_AUDIT_VERIFY,
    PERMISSION_DISPATCH_OPENROUTER_FREE,
    PERMISSION_EMERGENCY_OVERRIDE,
    SCHEMA_VERSION_1_0,
    CanonicalAuthorizationManifest,
    CanonicalSerializationError,
    CryptographicSignatureError,
    Ed25519Signer,
    Ed25519Verifier,
    HumanOperationalAuthorizationError,
    KeyAuthorityError,
    ReplayAuthorizationError,
    SignatureMetadata,
    SignedAuthorizationManifest,
    TripartiteAuthorizationVerifier,
    TripartiteVerificationResult,
    TrustStore,
    TrustStoreKeyError,
    TrustedKeyEntry,
)


class TestPhase4C2Ed25519AndTrustStore(unittest.TestCase):
    """Offline test suite for verifiable cryptographic authorization."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="ai_coord_4c2_test_")
        self.trust_store_path = os.path.join(self.tmp_dir, "trust_store.json")

        # Fixed sample commit for tests
        self.valid_commit = "1a0adb93ad5c4ebe755beb3d6b07dccfc43eeb92"

        # Generate a test signer for tests
        self.operator_key_id = "ed25519:operator_primary_2026"
        self.operator_signer = Ed25519Signer.generate(key_id=self.operator_key_id)

        # Build standard Trust Store with active operator key
        self.trust_store = TrustStore(store_path=self.trust_store_path)
        now_utc = dt.datetime.now(dt.timezone.utc)
        exp_utc = now_utc + dt.timedelta(days=365)

        self.operator_entry = TrustedKeyEntry(
            key_id=self.operator_key_id,
            public_key_hex=self.operator_signer.public_key_hex,
            identity="security_operator_root",
            permissions=[PERMISSION_DISPATCH_OPENROUTER_FREE, PERMISSION_AUDIT_VERIFY],
            status=KEY_STATUS_ACTIVE,
            created_at_utc=now_utc.isoformat(),
            expires_at_utc=exp_utc.isoformat(),
        )
        self.trust_store.register_key(self.operator_entry)
        self.trust_store.save_to_file()

        # Helper for valid dates
        self.now = dt.datetime.now(dt.timezone.utc)
        self.issued_at = (self.now - dt.timedelta(minutes=5)).isoformat()
        self.valid_until = (self.now + dt.timedelta(hours=2)).isoformat()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _create_sample_manifest(
        self,
        auth_id: str = "auth-4c2-test-001",
        budget: float = 0.0,
        commit: Optional[str] = None,
        models: Optional[Sequence[str]] = None,
        modalities: Optional[Sequence[str]] = None,
        max_dispatches: int = 1,
        authorizer: str = "security_operator_root",
        permissions: Optional[Sequence[str]] = None,
        issued_at: Optional[str] = None,
        valid_until: Optional[str] = None,
    ) -> CanonicalAuthorizationManifest:
        """Helper to create a canonical manifest with valid defaults."""
        return CanonicalAuthorizationManifest(
            authorization_id=auth_id,
            execution_scope_id="scope-pilot-run-offline",
            code_commit=commit or self.valid_commit,
            allowed_models=tuple(models or ["liquid/lfm-3-1.7b:free", "meta-llama/llama-3.2-3b-instruct:free"]),
            allowed_modalities=tuple(modalities or ["text"]),
            max_dispatches=max_dispatches,
            max_budget_usd=budget,
            authorizer_identity=authorizer,
            authorizer_permissions=tuple(permissions or [PERMISSION_DISPATCH_OPENROUTER_FREE]),
            issued_at_utc=issued_at or self.issued_at,
            valid_until_utc=valid_until or self.valid_until,
            schema_version=SCHEMA_VERSION_1_0,
        )

    # =======================================================================
    # SECTION 1: Canonical Serialization & Model Invariants
    # =======================================================================

    def test_01_canonical_bytes_are_deterministic_and_sorted(self) -> None:
        """Validate that canonical serialization produces identical bytes regardless of dict ordering."""
        manifest1 = self._create_sample_manifest()
        bytes1 = manifest1.to_canonical_bytes()

        # Re-construct from dict with scrambled keys
        raw_dict = manifest1.to_canonical_dict()
        reversed_dict = {k: raw_dict[k] for k in reversed(list(raw_dict.keys()))}
        manifest2 = CanonicalAuthorizationManifest.from_dict(reversed_dict)
        bytes2 = manifest2.to_canonical_bytes()

        self.assertEqual(bytes1, bytes2)
        self.assertEqual(manifest1.canonical_sha256(), manifest2.canonical_sha256())
        self.assertIn(b'"max_budget_usd":0.0', bytes1)
        self.assertIn(b'"schema_version":"1.0"', bytes1)

    def test_02_manifest_construction_rejects_non_zero_budget(self) -> None:
        """Enforce strict US$ 0.00 budget invariant on manifest construction."""
        with self.assertRaises(CanonicalSerializationError):
            self._create_sample_manifest(budget=0.01)

        with self.assertRaises(CanonicalSerializationError):
            self._create_sample_manifest(budget=-1.0)

        with self.assertRaises(CanonicalSerializationError):
            self._create_sample_manifest(budget=1e-6)

    def test_03_manifest_construction_rejects_invalid_commit(self) -> None:
        """Enforce strict 40-character hex commit on manifest construction."""
        with self.assertRaises(CanonicalSerializationError):
            self._create_sample_manifest(commit="short_hash")

        with self.assertRaises(CanonicalSerializationError):
            self._create_sample_manifest(commit="g" * 40)  # 'g' is not hex

    def test_04_manifest_rejects_shadow_injected_fields(self) -> None:
        """Reject manifests containing unknown shadow fields to prevent bypass injection."""
        data = self._create_sample_manifest().to_canonical_dict()
        data["hidden_budget_override"] = 50.0

        with self.assertRaises(CanonicalSerializationError) as ctx:
            CanonicalAuthorizationManifest.from_dict(data)
        self.assertIn("Unknown fields present in manifest", str(ctx.exception))

    # =======================================================================
    # SECTION 2: Ed25519 Cryptographic Primitives (Pure RFC 8032)
    # =======================================================================

    def test_05_ed25519_key_generation_and_raw_sizes(self) -> None:
        """Verify RFC 8032 Ed25519 sizes: 32 bytes public (64 hex), 64 bytes signature (128 hex)."""
        signer = Ed25519Signer.generate(key_id="ed25519:test_key")
        self.assertEqual(len(signer.public_key_hex), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in signer.public_key_hex.lower()))

        msg = b"Offline reproducible authorization payload"
        sig_hex = signer.sign_bytes(msg)
        self.assertEqual(len(sig_hex), 128)

        is_valid = Ed25519Verifier.verify_signature(
            public_key_hex=signer.public_key_hex,
            canonical_bytes=msg,
            signature_hex=sig_hex,
        )
        self.assertTrue(is_valid)

    def test_06_ed25519_tampered_payload_verification_fails(self) -> None:
        """Verify that altering a single byte in the signed message invalidates the signature."""
        signer = Ed25519Signer.generate(key_id="ed25519:test_key")
        msg = b"Payload A"
        sig_hex = signer.sign_bytes(msg)

        tampered_msg = b"Payload B"
        is_valid = Ed25519Verifier.verify_signature(
            public_key_hex=signer.public_key_hex,
            canonical_bytes=tampered_msg,
            signature_hex=sig_hex,
        )
        self.assertFalse(is_valid)

    def test_07_ed25519_corrupted_signature_fails_cleanly(self) -> None:
        """Verify that corrupted signature hex, bit flips, or bad lengths fail cleanly without crash."""
        signer = Ed25519Signer.generate(key_id="ed25519:test_key")
        msg = b"Some test message"
        sig_hex = signer.sign_bytes(msg)

        # Bad length (truncated)
        self.assertFalse(Ed25519Verifier.verify_signature(signer.public_key_hex, msg, sig_hex[:100]))

        # Bad characters (non-hex)
        bad_chars = "zz" + sig_hex[2:]
        self.assertFalse(Ed25519Verifier.verify_signature(signer.public_key_hex, msg, bad_chars))

        # Bit flip in signature
        flipped = ("0" if sig_hex[0] != "0" else "1") + sig_hex[1:]
        self.assertFalse(Ed25519Verifier.verify_signature(signer.public_key_hex, msg, flipped))

    def test_08_ed25519_wrong_key_verification_fails(self) -> None:
        """Verify that a signature produced by key A is rejected when verified against key B."""
        signer_a = Ed25519Signer.generate(key_id="ed25519:key_a")
        signer_b = Ed25519Signer.generate(key_id="ed25519:key_b")
        msg = b"Canonical test payload"
        sig_a = signer_a.sign_bytes(msg)

        is_valid = Ed25519Verifier.verify_signature(
            public_key_hex=signer_b.public_key_hex,
            canonical_bytes=msg,
            signature_hex=sig_a,
        )
        self.assertFalse(is_valid)

    # =======================================================================
    # SECTION 3: Trust Store Governance & Policy
    # =======================================================================

    def test_09_trust_store_persistence_and_atomic_reload(self) -> None:
        """Verify atomic file persistence and reload of Trust Store."""
        loaded_store = TrustStore.load_from_file(self.trust_store_path)
        self.assertEqual(len(loaded_store), 1)

        key = loaded_store.get_key(self.operator_key_id)
        self.assertIsNotNone(key)
        self.assertEqual(key.identity, "security_operator_root")
        self.assertEqual(key.status, KEY_STATUS_ACTIVE)

    def test_10_trust_store_strictly_rejects_private_key_material(self) -> None:
        """Enforce architectural safety: Trust Store rejects private key injection."""
        malicious_key_dict = {
            "key_id": "ed25519:attacker",
            "public_key_hex": self.operator_signer.public_key_hex,
            "private_key_hex": "11" * 32,  # private key leak attempt
            "identity": "attacker",
            "permissions": ["dispatch:openrouter_free"],
            "status": "active",
            "created_at_utc": self.now.isoformat(),
        }
        with self.assertRaises(ValueError) as ctx:
            TrustedKeyEntry.from_dict(malicious_key_dict)
        self.assertIn("Attempted to load private key fields", str(ctx.exception))

    def test_11_trust_store_duplicate_key_rejection(self) -> None:
        """Trust Store rejects duplicate key registration to protect immutability."""
        with self.assertRaises(ValueError) as ctx:
            self.trust_store.register_key(self.operator_entry)
        self.assertIn("already registered", str(ctx.exception))

    def test_12_trust_store_key_revocation_procedure(self) -> None:
        """Verify explicit key revocation with mandatory reason and timestamp."""
        revoked_key = self.trust_store.revoke_key(
            key_id=self.operator_key_id,
            reason="Suspected operational key compromise during audit",
        )
        self.assertEqual(revoked_key.status, KEY_STATUS_REVOKED)
        self.assertIn("compromise", revoked_key.revocation_reason)
        self.assertIsNotNone(revoked_key.revoked_at_utc)

        # Check authorization fail-closed
        is_auth, reason = self.trust_store.is_key_authorized(self.operator_key_id)
        self.assertFalse(is_auth)
        self.assertIn("revoked", reason)

    def test_13_trust_store_key_rotation_procedure(self) -> None:
        """Verify atomic key rotation: retires old key and registers new key."""
        new_signer = Ed25519Signer.generate(key_id="ed25519:operator_q4_2026")
        new_entry = TrustedKeyEntry(
            key_id=new_signer.key_id,
            public_key_hex=new_signer.public_key_hex,
            identity="security_operator_root",
            permissions=[PERMISSION_DISPATCH_OPENROUTER_FREE],
            status=KEY_STATUS_ACTIVE,
            created_at_utc=self.now.isoformat(),
        )

        old_key, new_key = self.trust_store.rotate_key(
            old_key_id=self.operator_key_id,
            new_entry=new_entry,
            rotation_reason="Scheduled quarterly cryptographic key rotation",
        )

        self.assertEqual(old_key.status, KEY_STATUS_REVOKED)
        self.assertIn("Rotated", old_key.revocation_reason)
        self.assertEqual(new_key.status, KEY_STATUS_ACTIVE)

        # Old key is rejected; new key is authorized
        self.assertFalse(self.trust_store.is_key_authorized(self.operator_key_id)[0])
        self.assertTrue(self.trust_store.is_key_authorized(new_signer.key_id)[0])

    def test_14_trust_store_expired_key_rejection(self) -> None:
        """Verify that an expired public key is rejected fail-closed."""
        expired_signer = Ed25519Signer.generate(key_id="ed25519:expired_key")
        expired_entry = TrustedKeyEntry(
            key_id=expired_signer.key_id,
            public_key_hex=expired_signer.public_key_hex,
            identity="expired_operator",
            permissions=[PERMISSION_DISPATCH_OPENROUTER_FREE],
            status=KEY_STATUS_ACTIVE,
            created_at_utc=(self.now - dt.timedelta(days=10)).isoformat(),
            expires_at_utc=(self.now - dt.timedelta(days=1)).isoformat(),  # Expired yesterday
        )
        self.trust_store.register_key(expired_entry)

        is_auth, reason = self.trust_store.is_key_authorized(expired_signer.key_id)
        self.assertFalse(is_auth)
        self.assertIn("expired", reason)

    def test_15_trust_store_insufficient_permissions_rejection(self) -> None:
        """Verify that a key lacking required capability token is rejected."""
        auditor_signer = Ed25519Signer.generate(key_id="ed25519:auditor_only")
        auditor_entry = TrustedKeyEntry(
            key_id=auditor_signer.key_id,
            public_key_hex=auditor_signer.public_key_hex,
            identity="external_auditor",
            permissions=[PERMISSION_AUDIT_VERIFY],  # lacks PERMISSION_DISPATCH_OPENROUTER_FREE
            status=KEY_STATUS_ACTIVE,
            created_at_utc=self.now.isoformat(),
        )
        self.trust_store.register_key(auditor_entry)

        is_auth, reason = self.trust_store.is_key_authorized(
            auditor_signer.key_id,
            required_permission=PERMISSION_DISPATCH_OPENROUTER_FREE,
        )
        self.assertFalse(is_auth)
        self.assertIn("lacks required capability", reason)

    # =======================================================================
    # SECTION 4: Tripartite Verification Engine & 12 Adversarial Scenarios
    # =======================================================================

    def test_16_tripartite_golden_path_all_tiers_pass(self) -> None:
        """Scenario TC-01: Valid signature, active key in Trust Store, valid commit & consent -> FULL PASS."""
        manifest = self._create_sample_manifest()
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            requested_modality="text",
            requested_dispatches=1,
            human_operator_consent=True,
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertTrue(result.tier2_key_authority)
        self.assertTrue(result.tier3_human_authorized)
        self.assertTrue(result.is_fully_authorized)
        self.assertIn("AUTHORIZED", result.summary())

    def test_17_tripartite_tier1_tampered_manifest_fails_crypto(self) -> None:
        """Scenario TC-02: Manifest tampered after signature (e.g. max_dispatches altered) -> Tier 1 Fail."""
        manifest = self._create_sample_manifest(max_dispatches=1)
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        # Attacker tampers with manifest object in transit: changes max_dispatches to 5
        tampered_dict = signed_manifest.to_dict()
        tampered_dict["manifest"]["max_dispatches"] = 5
        tampered_signed = SignedAuthorizationManifest.from_dict(tampered_dict)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=tampered_signed,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            requested_modality="text",
            requested_dispatches=1,
            human_operator_consent=True,
        )

        self.assertFalse(result.tier1_crypto_valid)
        self.assertIn("mathematically invalid or canonical manifest was tampered", result.tier1_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_18_tripartite_tier1_model_substitution_fails_crypto(self) -> None:
        """Scenario TC-03: Model substitution (adding paid model into allowed_models) -> Tier 1 Fail."""
        manifest = self._create_sample_manifest(models=["liquid/lfm-3-1.7b:free"])
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        # Attacker injects a paid model into allowed_models
        tampered_dict = signed_manifest.to_dict()
        tampered_dict["manifest"]["allowed_models"] = ["liquid/lfm-3-1.7b:free", "openai/gpt-4o"]
        tampered_signed = SignedAuthorizationManifest.from_dict(tampered_dict)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=tampered_signed,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
        )

        self.assertFalse(result.tier1_crypto_valid)
        self.assertFalse(result.is_fully_authorized)

    def test_19_tripartite_tier2_unknown_public_key_fails_authority(self) -> None:
        """Scenario TC-04: Valid Ed25519 signature by unknown rogue key -> Tier 1 Pass, Tier 2 Fail."""
        rogue_signer = Ed25519Signer.generate(key_id="ed25519:rogue_unregistered_actor")
        manifest = self._create_sample_manifest(authorizer="rogue_unregistered_actor")
        signed_manifest = rogue_signer.sign_manifest(manifest)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
        )

        # Tier 1 passes because the mathematical signature over canonical bytes matches rogue public key
        self.assertTrue(result.tier1_crypto_valid)
        # Tier 2 FAILS because rogue key is not in Trust Store
        self.assertFalse(result.tier2_key_authority)
        self.assertIn("not present in TrustStore", result.tier2_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_20_tripartite_tier2_revoked_key_fails_authority(self) -> None:
        """Scenario TC-05: Valid signature by key that was subsequently revoked -> Tier 2 Fail."""
        manifest = self._create_sample_manifest()
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        # Revoke the operator key in Trust Store
        self.trust_store.revoke_key(
            key_id=self.operator_key_id,
            reason="Emergency revocation: operational workstation decommissioned",
        )

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertFalse(result.tier2_key_authority)
        self.assertIn("revoked", result.tier2_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_21_tripartite_tier2_authorizer_identity_mismatch_fails(self) -> None:
        """Scenario TC-06: Key ID belongs to user A, but manifest authorizer_identity claims user B."""
        # Key belongs to 'security_operator_root', manifest claims 'system_superadmin'
        manifest = self._create_sample_manifest(authorizer="system_superadmin")
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertFalse(result.tier2_key_authority)
        self.assertIn("identity mismatch", result.tier2_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_22_tripartite_tier3_commit_divergence_fails_operational(self) -> None:
        """Scenario TC-07: Valid signed manifest for commit X evaluated against commit Y -> Tier 3 Fail."""
        manifest = self._create_sample_manifest(commit="1a0adb93ad5c4ebe755beb3d6b07dccfc43eeb92")
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        different_commit = "9999999999999999999999999999999999999999"
        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=different_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertTrue(result.tier2_key_authority)
        self.assertFalse(result.tier3_human_authorized)
        self.assertIn("Code commit mismatch", result.tier3_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_23_tripartite_tier3_expired_validity_window_fails(self) -> None:
        """Scenario TC-08: Valid manifest whose validity window has expired -> Tier 3 Fail."""
        past_issued = (self.now - dt.timedelta(hours=5)).isoformat()
        past_expired = (self.now - dt.timedelta(hours=1)).isoformat()

        manifest = self._create_sample_manifest(issued_at=past_issued, valid_until=past_expired)
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertTrue(result.tier2_key_authority)
        self.assertFalse(result.tier3_human_authorized)
        self.assertIn("expired", result.tier3_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_24_tripartite_tier3_future_issued_timestamp_fails(self) -> None:
        """Scenario TC-09: Manifest with future issuance timestamp (clock skew attack) -> Tier 3 Fail."""
        future_issued = (self.now + dt.timedelta(hours=2)).isoformat()
        far_future = (self.now + dt.timedelta(days=1)).isoformat()

        manifest = self._create_sample_manifest(issued_at=future_issued, valid_until=far_future)
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertTrue(result.tier2_key_authority)
        self.assertFalse(result.tier3_human_authorized)
        self.assertIn("issued in the future", result.tier3_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_25_tripartite_tier3_unauthorized_model_dispatch_fails(self) -> None:
        """Scenario TC-10: Requesting dispatch to model not in allowed_models -> Tier 3 Fail."""
        manifest = self._create_sample_manifest(models=["liquid/lfm-3-1.7b:free"])
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="unauthorized/random-model:free",
            human_operator_consent=True,
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertTrue(result.tier2_key_authority)
        self.assertFalse(result.tier3_human_authorized)
        self.assertIn("not in manifest allowed_models", result.tier3_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_26_tripartite_tier3_missing_human_operator_consent_fails(self) -> None:
        """Scenario TC-11: Cryptographically flawless manifest rejected if human operator consent flag missing."""
        manifest = self._create_sample_manifest()
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)
        result = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=False,  # Operator flag NOT passed!
        )

        self.assertTrue(result.tier1_crypto_valid)
        self.assertTrue(result.tier2_key_authority)
        self.assertFalse(result.tier3_human_authorized)
        self.assertIn("Human operational consent is missing", result.tier3_reason)
        self.assertFalse(result.is_fully_authorized)

    def test_27_anti_replay_protection_prevents_duplicate_authorization(self) -> None:
        """Scenario TC-12: Prevents replay attack reusing the exact same authorization_id."""
        manifest = self._create_sample_manifest(auth_id="auth-unique-run-99")
        signed_manifest = self.operator_signer.sign_manifest(manifest)

        replay_cache: set[str] = set()
        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store, replay_cache=replay_cache)

        # First verification succeeds and consumes authorization_id
        res1 = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
            check_replay=True,
        )
        self.assertTrue(res1.is_fully_authorized)
        self.assertIn("auth-unique-run-99", replay_cache)

        # Second verification with exact same authorization_id is blocked fail-closed
        res2 = verifier.verify(
            signed_manifest=signed_manifest,
            expected_commit=self.valid_commit,
            requested_model="liquid/lfm-3-1.7b:free",
            human_operator_consent=True,
            check_replay=True,
        )
        self.assertFalse(res2.tier3_human_authorized)
        self.assertIn("Replay attack prevented", res2.tier3_reason)
        self.assertFalse(res2.is_fully_authorized)

    def test_28_verify_and_raise_throws_specific_tier_exceptions(self) -> None:
        """Verify that verify_and_raise raises typed exceptions per tier."""
        verifier = TripartiteAuthorizationVerifier(trust_store=self.trust_store)

        # 1. Tier 1 fail -> CryptographicSignatureError
        manifest1 = self._create_sample_manifest()
        signed1 = self.operator_signer.sign_manifest(manifest1)
        tampered_dict = signed1.to_dict()
        tampered_dict["manifest"]["allowed_models"] = ["other/model:free"]
        tampered_signed = SignedAuthorizationManifest.from_dict(tampered_dict)

        with self.assertRaises(CryptographicSignatureError):
            verifier.verify_and_raise(
                tampered_signed,
                self.valid_commit,
                "other/model:free",
                human_operator_consent=True,
            )

        # 2. Tier 2 fail -> KeyAuthorityError
        rogue_signer = Ed25519Signer.generate(key_id="ed25519:unknown_key")
        manifest2 = self._create_sample_manifest(authorizer="unknown_key")
        signed2 = rogue_signer.sign_manifest(manifest2)

        with self.assertRaises(KeyAuthorityError):
            verifier.verify_and_raise(
                signed2,
                self.valid_commit,
                "liquid/lfm-3-1.7b:free",
                human_operator_consent=True,
            )

        # 3. Tier 3 fail -> HumanOperationalAuthorizationError
        manifest3 = self._create_sample_manifest()
        signed3 = self.operator_signer.sign_manifest(manifest3)

        with self.assertRaises(HumanOperationalAuthorizationError):
            verifier.verify_and_raise(
                signed3,
                self.valid_commit,
                "liquid/lfm-3-1.7b:free",
                human_operator_consent=False,  # missing consent
            )

        # 4. Replay attack -> ReplayAuthorizationError
        cache = {"auth-4c2-test-001"}
        verifier_with_cache = TripartiteAuthorizationVerifier(trust_store=self.trust_store, replay_cache=cache)
        with self.assertRaises(ReplayAuthorizationError):
            verifier_with_cache.verify_and_raise(
                signed3,
                self.valid_commit,
                "liquid/lfm-3-1.7b:free",
                human_operator_consent=True,
                check_replay=True,
            )


if __name__ == "__main__":
    unittest.main()
