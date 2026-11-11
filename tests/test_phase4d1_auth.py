"""Tests for AI Coordinator Desktop Authentication, Profiles, and Secure Vault (Marco 4D.1.3).

Verifies:
- Profile isolation, project path fencing, and name validation.
- SecureVault round-trip encryption/decryption with Win32 DPAPI and AES-256-GCM.
- Cryptographic cross-profile isolation (Profile A cannot decrypt Profile B).
- 5-Tier security authorizer and fail-closed worker lockout.
- HumanPrincipal integration preserving Core v0.6.0 authority invariants.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import pytest

from ai_coordinator.human_workspace import HumanPrincipal
from desktop.auth.authorizer import DesktopAuthorizer, SecurityAuthorizationError
from desktop.auth.profile import (
    DesktopProfileManager,
    ProfileNotFoundError,
    ProfileValidationError,
)
from desktop.auth.vault import (
    AESGCMVault,
    VaultDecryptionError,
    Win32DPAPIVault,
    create_vault,
)


@pytest.fixture
def temp_profiles_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ---------------------------------------------------------------------------
# 1. Profile Manager & Fencing Tests
# ---------------------------------------------------------------------------


def test_profile_manager_init_and_default(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    active = mgr.get_active_profile()
    assert active.name == "default"
    assert active.display_name == "Default Profile"
    assert Path(active.data_dir).exists()


def test_profile_creation_and_switching(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    work = mgr.create_profile("work_profile", display_name="Work Tasks")
    assert work.name == "work_profile"
    assert work.display_name == "Work Tasks"

    # Switch
    mgr.set_active_profile("work_profile")
    active = mgr.get_active_profile()
    assert active.name == "work_profile"

    # List profiles
    profs = mgr.list_profiles()
    names = [p.name for p in profs]
    assert "default" in names
    assert "work_profile" in names


def test_profile_name_validation(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    with pytest.raises(ProfileValidationError):
        mgr.create_profile("invalid name with spaces!")

    with pytest.raises(ProfileValidationError):
        mgr.create_profile("../path_traversal")

    with pytest.raises(ProfileValidationError):
        mgr.create_profile("")


def test_profile_deletion_rules(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    mgr.create_profile("temp_prof")

    # Cannot delete default
    with pytest.raises(ProfileValidationError):
        mgr.delete_profile("default")

    # Cannot delete currently active
    mgr.set_active_profile("temp_prof")
    with pytest.raises(ProfileValidationError):
        mgr.delete_profile("temp_prof")

    # Switch back to default and delete temp
    mgr.set_active_profile("default")
    assert mgr.delete_profile("temp_prof") is True
    assert mgr.get_profile("temp_prof") is None


def test_project_authorization_fencing(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    with tempfile.TemporaryDirectory() as proj_dir:
        canon_proj = str(Path(proj_dir).resolve())
        sub_dir = Path(proj_dir) / "subpkg" / "src"
        sub_dir.mkdir(parents=True, exist_ok=True)

        mgr.authorize_project("default", canon_proj)
        assert mgr.is_project_authorized("default", canon_proj) is True
        assert mgr.is_project_authorized("default", sub_dir) is True

        # Sibling or outside directory is unauthorized
        outside = Path(proj_dir).parent / "unauthorized_neighbor"
        assert mgr.is_project_authorized("default", outside) is False


# ---------------------------------------------------------------------------
# 2. Vault Tests (DPAPI and AES-256-GCM)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-specific")
def test_win32_dpapi_vault_roundtrip(temp_profiles_dir):
    vault_file = temp_profiles_dir / "profile_a" / "vault.enc"
    vault = Win32DPAPIVault("profile_a", vault_file)

    vault.set_secret("OPENROUTER_API_KEY", "sk-or-v1-mocked-synthetic-key")
    vault.set_secret("DATABASE_PASSWORD", "super-secret-pass-99")

    assert vault.get_secret("OPENROUTER_API_KEY") == "sk-or-v1-mocked-synthetic-key"
    assert vault.get_secret("DATABASE_PASSWORD") == "super-secret-pass-99"
    assert vault.has_secret("OPENROUTER_API_KEY") is True
    assert vault.list_keys() == ["DATABASE_PASSWORD", "OPENROUTER_API_KEY"]

    assert vault.delete_secret("DATABASE_PASSWORD") is True
    assert vault.get_secret("DATABASE_PASSWORD") is None


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-specific")
def test_win32_dpapi_cross_profile_isolation(temp_profiles_dir):
    """Profile B cannot decrypt secrets written by Profile A due to different DPAPI entropy."""
    vault_file = temp_profiles_dir / "vault_shared.enc"

    vault_a = Win32DPAPIVault("profile_alice", vault_file)
    vault_a.set_secret("SECRET_KEY", "alice-classified-credentials")

    # Vault B targets the same file but with profile_bob entropy
    vault_b = Win32DPAPIVault("profile_bob", vault_file)
    with pytest.raises(VaultDecryptionError):
        vault_b.get_secret("SECRET_KEY")


def test_aes_gcm_vault_roundtrip(temp_profiles_dir):
    vault_file = temp_profiles_dir / "profile_c" / "vault.enc"
    vault = AESGCMVault("profile_c", vault_file, master_password="secure-master-password-123")

    vault.set_secret("TOKEN", "jwt.token.value.xyz")
    assert vault.get_secret("TOKEN") == "jwt.token.value.xyz"
    assert vault.list_keys() == ["TOKEN"]
    assert vault.delete_secret("TOKEN") is True
    assert vault.get_secret("TOKEN") is None


def test_aes_gcm_cross_profile_isolation(temp_profiles_dir):
    """AEAD authentication tag verifies associated_data (profile name), failing if mismatched."""
    vault_file = temp_profiles_dir / "vault_shared_aes.enc"

    vault_alice = AESGCMVault("alice", vault_file, master_password="shared-password")
    vault_alice.set_secret("SECRET", "alice-only")

    # Bob tries with same password but different profile name
    vault_bob = AESGCMVault("bob", vault_file, master_password="shared-password")
    with pytest.raises(VaultDecryptionError):
        vault_bob.get_secret("SECRET")


# ---------------------------------------------------------------------------
# 3. DesktopAuthorizer & 5-Tier Boundary Tests
# ---------------------------------------------------------------------------


def test_authorizer_session_flow_and_constant_time(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    authorizer = DesktopAuthorizer(profile_manager=mgr, vault_backend="aes_gcm")

    token = authorizer.issue_session("default")
    assert token.startswith("dtsess_default_")

    # Validation succeeds
    assert authorizer.validate_session(token) == "default"

    # Forged token fails
    with pytest.raises(SecurityAuthorizationError):
        authorizer.validate_session("dtsess_default_forged00000000000000000000000000")

    # Empty token fails
    with pytest.raises(SecurityAuthorizationError):
        authorizer.validate_session("")

    # Revoke session
    assert authorizer.revoke_session(token) is True
    with pytest.raises(SecurityAuthorizationError):
        authorizer.validate_session(token)


def test_authorizer_vault_delegation(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    authorizer = DesktopAuthorizer(profile_manager=mgr, vault_backend="aes_gcm")

    token = authorizer.issue_session("default")
    authorizer.authorize_vault_write(token, "API_KEY", "sk-live-desktop-safe")
    val = authorizer.authorize_vault_read(token, "API_KEY")
    assert val == "sk-live-desktop-safe"


def test_authorizer_worker_lockout_fail_closed(temp_profiles_dir, monkeypatch):
    """Worker environment variables trigger immediate fail-closed exception."""
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    authorizer = DesktopAuthorizer(profile_manager=mgr)

    token = authorizer.issue_session("default")

    # Simulate worker environment
    monkeypatch.setenv("AI_COORDINATOR_ROLE", "worker")

    with pytest.raises(SecurityAuthorizationError) as exc_info:
        authorizer.issue_session("default")
    assert "Subordinate workers are strictly forbidden" in str(exc_info.value)

    with pytest.raises(SecurityAuthorizationError):
        authorizer.validate_session(token)

    with pytest.raises(SecurityAuthorizationError):
        authorizer.authorize_vault_read(token, "API_KEY")


def test_authorizer_human_principal_creation(temp_profiles_dir):
    mgr = DesktopProfileManager(root_dir=temp_profiles_dir)
    authorizer = DesktopAuthorizer(profile_manager=mgr)

    token = authorizer.issue_session("default")
    principal = authorizer.create_human_principal(token)

    assert isinstance(principal, HumanPrincipal)
    assert principal.owner_id == "default"
    assert principal.role == "human"
    assert principal.session_token == token
