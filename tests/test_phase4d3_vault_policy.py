"""Marco 4D.3, Etapa 3 — credential vault policy (decision D8).

- Windows keeps DPAPI.
- Where no secure system store is integrated, persistent credential storage is disabled with a clear error and
  nothing is written to disk.
- A password derivable from the profile name is never used as protection: AES-GCM requires an explicit password.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import pytest

import desktop.auth.vault as vault_mod
from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.auth.vault import AESGCMVault, UnavailableVault, VaultUnavailableError, create_vault


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        yield Path(tmp)


def test_source_has_no_password_derivable_from_the_profile_name():
    source = (_repo_root / "desktop" / "auth" / "vault.py").read_text(encoding="utf-8")
    assert "default-profile-key" not in source


def test_aes_gcm_without_explicit_password_is_refused(tmp_dir):
    with pytest.raises(VaultUnavailableError) as info:
        AESGCMVault("default", tmp_dir / "vault.enc")
    assert "indisponível" in str(info.value)
    assert not (tmp_dir / "vault.enc").exists()


def test_auto_vault_without_secure_store_is_unavailable_and_writes_nothing(tmp_dir, monkeypatch):
    monkeypatch.setattr(vault_mod.sys, "platform", "linux")
    vault = create_vault(profile_dir=tmp_dir, profile_name="default", backend="auto")

    assert isinstance(vault, UnavailableVault)
    assert vault.available is False
    assert vault.backend_name == "unavailable"
    for operation in (
        lambda: vault.set_secret("API_KEY", "sk-should-not-persist"),
        lambda: vault.get_secret("API_KEY"),
        lambda: vault.delete_secret("API_KEY"),
        lambda: vault.list_keys(),
    ):
        with pytest.raises(VaultUnavailableError):
            operation()
    assert not (tmp_dir / "vault.enc").exists()


def test_authorizer_reports_unavailable_storage_clearly(tmp_dir, monkeypatch):
    monkeypatch.setattr(vault_mod.sys, "platform", "darwin")
    profiles = DesktopProfileManager(root_dir=tmp_dir / "profiles")
    authorizer = DesktopAuthorizer(profile_manager=profiles)
    token = authorizer.issue_session("default")

    with pytest.raises(VaultUnavailableError) as info:
        authorizer.authorize_vault_write(token, "API_KEY", "sk-should-not-persist")
    assert "Nenhuma credencial foi gravada" in str(info.value)
    assert not any(p.name == "vault.enc" for p in (tmp_dir / "profiles").rglob("*"))


def test_explicit_master_password_keeps_aes_gcm_available(tmp_dir, monkeypatch):
    monkeypatch.setattr(vault_mod.sys, "platform", "linux")
    vault = create_vault(profile_dir=tmp_dir, profile_name="default", backend="auto", master_password="operator-chosen")
    assert vault.available is True
    assert vault.backend_name == "aes_gcm"
    vault.set_secret("K", "v")
    assert vault.get_secret("K") == "v"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-specific")
def test_windows_keeps_dpapi(tmp_dir):
    vault = create_vault(profile_dir=tmp_dir, profile_name="default", backend="auto")
    assert vault.available is True
    assert vault.backend_name == "dpapi"
