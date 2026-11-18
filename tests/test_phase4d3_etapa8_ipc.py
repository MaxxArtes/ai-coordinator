"""Etapa 8 (4D.3): broad authorizations need an explicit confirmation (L5) and vault removal can be conditioned on the
revision the operator saw (L6), both through the desktop IPC.

Every test runs with USERPROFILE/HOME/LOCALAPPDATA/APPDATA pointing to a temporary folder, so Path.home() is that
folder and the profile and vault are temporary. Authorizing a drive root here only writes the temporary profile.
Values that look like credentials are synthetic markers; the assertions only check that they never appear in a response.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desktop.ipc.server as server_mod
from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager, is_broad_directory
from desktop.auth.vault import UnavailableVault, VaultUnavailableError
from desktop.ipc.server import SecureIPCServer
from desktop.terminal.manager import TerminalManager

FAKE = "FAKE_ETAPA8_MARKER"  # every synthetic "secret" below contains this text
BROAD_MESSAGE = ("Este diretório dá acesso amplo (raiz do disco ou pasta pessoal inteira). "
                 "Confirme explicitamente para autorizar.")
CONFLICT_MESSAGE = "O cofre mudou desde a conferência. Recarregue e confirme novamente."
WINDOWS = sys.platform == "win32"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "AppData" / "Local").mkdir(parents=True)
    (home / "AppData" / "Roaming").mkdir(parents=True)
    for name in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("AI_COORDINATOR_HOME", str(tmp_path / "core-home"))
    monkeypatch.delenv("AI_COORDINATOR_ROLE", raising=False)
    assert Path.home().resolve() == home.resolve(), "perfil não isolado no diretório temporário"
    return SimpleNamespace(home=home, tmp=tmp_path)


def make_env(isolated, **vault_options):
    root = isolated.tmp / "profiles"
    profiles = DesktopProfileManager(root_dir=root)
    options = vault_options or {"vault_backend": "aes_gcm", "master_password": "fixture-only-password"}
    authorizer = DesktopAuthorizer(profile_manager=profiles, **options)
    token = authorizer.issue_session("default")
    manager = TerminalManager(authorizer=authorizer)
    server = SecureIPCServer(transport=MagicMock(), terminal_manager=manager, session_token=token)
    return SimpleNamespace(server=server, token=token, root=root, profiles=profiles, manager=manager,
                           profile_file=root / "default" / "profile.json",
                           vault_file=root / "default" / "vault.enc", **vars(isolated))


@pytest.fixture
def env(isolated):
    # AES-GCM with a fixture password: the same vault contract on every platform, never the operator's DPAPI vault.
    return make_env(isolated)


def call(env, method, params=None):
    payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}, "session_token": env.token}
    return env.server.handle_raw_payload(json.dumps(payload))


def authorize(env, path, **extra):
    return call(env, "profile.authorize_project", {"project_path": str(path), **extra})


def assert_broad_refusal(env, resp, requested):
    assert resp.result is None, resp.result
    assert resp.error == {"code": -32602, "message": BROAD_MESSAGE,
                          "data": {"field": "project_path", "reason": "broad_scope"}}
    assert str(requested) not in json.dumps(resp.to_dict(), ensure_ascii=False)  # the path is never echoed


def stored_projects(env):
    return json.loads(env.profile_file.read_text(encoding="utf-8")).get("authorized_projects", [])


# ---- L5: broad directories need confirm_broad ----

def drive_roots(env):
    anchor = Path(env.tmp.resolve().anchor)
    roots = [str(anchor), "/"]
    if WINDOWS:
        roots += [str(anchor).replace("\\", "/"), "\\\\?\\" + str(anchor)]
    return roots


def test_drive_roots_are_refused_without_confirmation(env):
    before = env.profile_file.read_bytes()
    for requested in drive_roots(env):
        assert Path(requested).is_dir(), requested
        assert_broad_refusal(env, authorize(env, requested), requested)
    assert env.profile_file.read_bytes() == before  # nothing was written


def test_home_folder_is_refused_without_confirmation(env):
    before = env.profile_file.read_bytes()
    for requested in (env.home, str(env.home) + os.sep, env.home / "AppData" / ".."):
        assert_broad_refusal(env, authorize(env, requested), requested)
    assert env.profile_file.read_bytes() == before


@pytest.mark.skipif(not WINDOWS, reason="os.path.normcase only folds letter case on Windows")
def test_home_folder_written_in_another_letter_case_is_refused(env):
    before = env.profile_file.read_bytes()
    for requested in (str(env.home).upper(), str(env.home).lower(), str(env.home).swapcase()):
        assert_broad_refusal(env, authorize(env, requested), requested)
    assert env.profile_file.read_bytes() == before


def test_folder_containing_the_home_folder_is_broad(env):
    """The parent of the home folder (C:\\Users, /home) contains the whole home folder: same confirmation."""
    assert_broad_refusal(env, authorize(env, env.home.parent), env.home.parent)
    assert stored_projects(env) == []


def test_ordinary_folders_are_authorized_without_confirmation(env):
    inside_home = env.home / "projects" / "repo"
    inside_home.mkdir(parents=True)
    outside_home = env.tmp / "work" / "repo"
    outside_home.mkdir(parents=True)
    for target in (inside_home, outside_home):
        resp = authorize(env, target)
        assert resp.error is None, resp.error
        assert resp.result["success"] is True and resp.result["project_path"] == str(target.resolve())
    status = call(env, "security.status").result
    assert sorted(status["authorized_projects"]) == sorted([str(inside_home.resolve()), str(outside_home.resolve())])
    assert status["broad_projects"] == []


@pytest.mark.parametrize("flag", [False, None])
def test_false_or_null_is_not_a_confirmation(env, flag):
    assert_broad_refusal(env, authorize(env, env.home, confirm_broad=flag), env.home)
    assert stored_projects(env) == []


def test_confirmed_broad_directories_are_authorized_and_reported(env):
    repo = env.tmp / "repo"
    repo.mkdir()
    assert authorize(env, repo).error is None
    root = Path(env.tmp.resolve().anchor)
    for target in (env.home, root):
        resp = authorize(env, target, confirm_broad=True)
        assert resp.error is None, resp.error
        assert resp.result["success"] is True and resp.result["project_path"] == str(target.resolve())
    status = call(env, "security.status").result
    expected = sorted([str(env.home.resolve()), str(root.resolve())])
    assert sorted(status["broad_projects"]) == expected
    assert set(status["broad_projects"]) <= set(status["authorized_projects"])
    assert str(repo.resolve()) in status["authorized_projects"] and str(repo.resolve()) not in status["broad_projects"]
    # Confirming an ordinary folder changes nothing: it is simply authorized.
    other = env.tmp / "other"
    other.mkdir()
    assert authorize(env, other, confirm_broad=True).error is None
    assert str(other.resolve()) not in call(env, "security.status").result["broad_projects"]


@pytest.mark.parametrize("flag", ["true", "yes", 1, 0, [], {}, [True]])
def test_confirm_broad_of_another_type_is_invalid(env, flag):
    ordinary = env.tmp / "repo"
    ordinary.mkdir()
    before = env.profile_file.read_bytes()
    for target in (ordinary, env.home):
        resp = authorize(env, target, confirm_broad=flag)
        assert resp.error["code"] == -32602
        assert resp.error["data"] == {"field": "confirm_broad", "reason": "invalid_value"}
    assert env.profile_file.read_bytes() == before


def test_invalid_project_path_is_still_reported_first(env):
    resp = call(env, "profile.authorize_project", {"project_path": "", "confirm_broad": True})
    assert resp.error["data"] == {"field": "project_path", "reason": "invalid_value"}
    missing = call(env, "profile.authorize_project", {"project_path": str(env.tmp / "missing"), "confirm_broad": True})
    assert missing.error["data"] == {"field": "project_path", "reason": "path_missing"}


def test_broad_entries_stored_before_are_kept_and_reported(env):
    """Entries written before the confirmation existed are not removed nor rewritten; security.status lists them."""
    root = str(Path(env.tmp.resolve().anchor))
    repo = env.tmp / "repo"
    repo.mkdir()
    legacy = sorted([root, str(env.home), str(repo)])

    def change(profile):
        profile.authorized_projects = legacy
    env.profiles._mutate("default", change)
    before = env.profile_file.read_bytes()
    status = call(env, "security.status").result
    assert status["authorized_projects"] == legacy
    assert sorted(status["broad_projects"]) == sorted([root, str(env.home)])
    assert call(env, "profile.get_active").result["authorized_projects"] == legacy
    assert env.profile_file.read_bytes() == before


def test_broad_check_follows_the_link_to_where_it_leads(env):
    """A junction (Windows) or symlink to the home folder authorizes the home folder: it is broad too."""
    link = env.tmp / "link-to-home"
    try:
        if WINDOWS:
            import _winapi
            _winapi.CreateJunction(str(env.home), str(link))
        else:
            os.symlink(env.home, link, target_is_directory=True)
    except (OSError, ImportError, AttributeError) as exc:
        pytest.skip(f"link not available here: {type(exc).__name__}")
    assert_broad_refusal(env, authorize(env, link), link)
    assert stored_projects(env) == []


@pytest.mark.skipif(not WINDOWS, reason="UNC paths only exist on Windows")
def test_network_share_root_is_broad_and_a_folder_inside_it_is_not(env, monkeypatch):
    """No share is reachable in the test: is_dir/resolve are answered locally for the fixture server only."""
    share = "\\\\fixture-server\\share"
    real_resolve, real_is_dir = Path.resolve, Path.is_dir

    def on_share(path):
        return str(path).lower().startswith(share.lower())

    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: Path(os.path.normpath(str(self))) if on_share(self)
                        else real_resolve(self, *a, **k))
    monkeypatch.setattr(Path, "is_dir", lambda self, *a, **k: True if on_share(self) else real_is_dir(self, *a, **k))
    for requested in (share, share + "\\", share.replace("\\", "/"), share.upper()):
        assert_broad_refusal(env, authorize(env, requested), requested)
    assert stored_projects(env) == []
    project = share + "\\team\\repo"
    resp = authorize(env, project)
    assert resp.error is None, resp.error
    confirmed = authorize(env, share, confirm_broad=True)
    assert confirmed.error is None, confirmed.error
    status = call(env, "security.status").result
    assert [os.path.normcase(p) for p in status["broad_projects"]] == [os.path.normcase(share + "\\")]


def test_broad_classification_reads_only_the_text(env, monkeypatch):
    """security.status must not wait for a disconnected network drive: a stored entry is never resolved there."""
    assert is_broad_directory(env.tmp.resolve().anchor) is True
    assert is_broad_directory(env.home) is True
    assert is_broad_directory(env.home / "projects") is False
    assert is_broad_directory(env.tmp / "elsewhere") is False
    assert is_broad_directory("") is False and is_broad_directory(".") is False
    entry = str(env.tmp / "fake-net" / "proj")
    real_resolve = Path.resolve

    def resolve(self, *args, **kwargs):
        if os.path.normcase(str(self)).startswith(os.path.normcase(entry)):
            raise AssertionError("a stored entry was resolved")
        return real_resolve(self, *args, **kwargs)

    def change(profile):
        profile.authorized_projects = [entry]
    env.profiles._mutate("default", change)
    monkeypatch.setattr(Path, "resolve", resolve)
    status = call(env, "security.status")
    assert status.error is None, status.error
    assert status.result["broad_projects"] == []


# ---- L6: vault.delete_secret with expected_revision ----

def set_secret(env, key, suffix):
    resp = call(env, "vault.set_secret", {"key": key, "value": f"{FAKE}-{suffix}"})
    assert resp.error is None, resp.error


def keys_and_revision(env):
    result = call(env, "vault.list_keys").result
    return result["keys"], result["revision"]


def test_delete_with_the_current_revision_removes_the_key(env):
    set_secret(env, "FIXTURE_KEY", "one")
    set_secret(env, "OTHER_KEY", "two")
    _, revision = keys_and_revision(env)
    resp = call(env, "vault.delete_secret", {"key": "FIXTURE_KEY", "expected_revision": revision})
    assert resp.error is None, resp.error
    assert resp.result == {"deleted": True, "key": "FIXTURE_KEY"}
    assert keys_and_revision(env)[0] == ["OTHER_KEY"]
    assert FAKE not in json.dumps(resp.to_dict())


def test_delete_with_a_stale_revision_is_a_conflict_and_removes_nothing(env):
    set_secret(env, "FIXTURE_KEY", "one")
    _, seen = keys_and_revision(env)
    set_secret(env, "OTHER_KEY", "two")  # another window changed the vault after the operator's reading
    before = env.vault_file.read_bytes()
    for key in ("FIXTURE_KEY", "MISSING_KEY"):
        resp = call(env, "vault.delete_secret", {"key": key, "expected_revision": seen})
        assert resp.result is None
        assert resp.error == {"code": server_mod.VAULT_CONFLICT_CODE, "message": CONFLICT_MESSAGE,
                              "data": {"field": "vault", "reason": "conflict"}}
        assert FAKE not in json.dumps(resp.to_dict())
    assert env.vault_file.read_bytes() == before
    keys, current = keys_and_revision(env)
    assert keys == ["FIXTURE_KEY", "OTHER_KEY"]
    # Reading again (as the interface does after a conflict) gives a revision that removes it.
    assert call(env, "vault.delete_secret", {"key": "FIXTURE_KEY", "expected_revision": current}).result == {
        "deleted": True, "key": "FIXTURE_KEY"}


@pytest.mark.parametrize("revision", ["", "abc", "g" * 64, "0" * 63, "0" * 65, " " + "0" * 63, 123, True, [], {}])
def test_delete_with_an_invalid_revision_is_refused(env, revision):
    set_secret(env, "FIXTURE_KEY", "one")
    before = env.vault_file.read_bytes()
    resp = call(env, "vault.delete_secret", {"key": "FIXTURE_KEY", "expected_revision": revision})
    assert resp.error["code"] == -32602
    assert resp.error["data"] == {"field": "expected_revision", "reason": "invalid_value"}
    assert env.vault_file.read_bytes() == before
    assert keys_and_revision(env)[0] == ["FIXTURE_KEY"]


def test_delete_without_a_revision_keeps_the_previous_behaviour(env):
    set_secret(env, "FIXTURE_KEY", "one")
    set_secret(env, "OTHER_KEY", "two")
    assert call(env, "vault.delete_secret", {"key": "FIXTURE_KEY"}).result == {"deleted": True, "key": "FIXTURE_KEY"}
    assert call(env, "vault.delete_secret", {"key": "FIXTURE_KEY"}).result == {"deleted": False, "key": "FIXTURE_KEY"}
    # null is the host's encoding of an absent Option<String>.
    assert call(env, "vault.delete_secret", {"key": "OTHER_KEY", "expected_revision": None}).result == {
        "deleted": True, "key": "OTHER_KEY"}
    assert keys_and_revision(env)[0] == []
    bad = call(env, "vault.delete_secret", {"key": "not a key", "expected_revision": "0" * 64})
    assert bad.error["data"] == {"field": "key", "reason": "invalid_value"}


def test_delete_with_a_revision_on_an_unreadable_vault_keeps_its_bytes(env):
    garbage = b"\x00not-a-vault" + FAKE.encode("ascii") * 3
    env.vault_file.parent.mkdir(parents=True, exist_ok=True)
    env.vault_file.write_bytes(garbage)
    resp = call(env, "vault.delete_secret", {"key": "FIXTURE_KEY", "expected_revision": "0" * 64})
    assert resp.error["code"] == server_mod.VAULT_UNAVAILABLE_CODE
    assert resp.error["data"] == {"field": "vault", "reason": "unreadable"}
    assert FAKE not in json.dumps(resp.to_dict())
    assert env.vault_file.read_bytes() == garbage


def test_delete_with_a_revision_where_no_vault_exists_is_unavailable(isolated):
    env = make_env(isolated, vault_backend="aes_gcm", master_password=None)  # no secure store: D8
    resp = call(env, "vault.delete_secret", {"key": "FIXTURE_KEY", "expected_revision": "0" * 64})
    assert resp.error["code"] == server_mod.VAULT_UNAVAILABLE_CODE
    assert resp.error["data"] == {"field": "vault", "reason": "unavailable"}
    with pytest.raises(VaultUnavailableError):
        UnavailableVault("default", isolated.tmp / "vault.enc").delete_secret("FIXTURE_KEY", expected_revision="0" * 64)
    assert not (isolated.tmp / "vault.enc").exists()
