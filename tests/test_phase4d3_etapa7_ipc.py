"""Etapa 7 (4D.3): profile, security, discovery and stored-settings contracts through the desktop IPC (K1-K7).

Every test runs with USERPROFILE/HOME/LOCALAPPDATA/APPDATA and AI_COORDINATOR_HOME pointing to temporary folders:
no operator profile, vault, routes.json or agent configuration is ever read. Values that look like credentials are
synthetic markers; the assertions only check that they never appear in a response.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desktop.auth.file_lock as file_lock
import desktop.auth.profile as profile_mod
import desktop.auth.vault as vault_mod
import desktop.ipc.server as server_mod
from desktop.auth.authorizer import DesktopAuthorizer, SecurityAuthorizationError
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.server import SecureIPCServer
from desktop.terminal.manager import TerminalManager

TOKEN = "etapa7-ipc-token"
FAKE = "FAKE_ETAPA7_MARKER"  # every synthetic "secret" below contains this text


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    core = tmp_path / "core-home"
    (home / "AppData" / "Local").mkdir(parents=True)
    (home / "AppData" / "Roaming").mkdir(parents=True)
    core.mkdir()
    for name in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("AI_COORDINATOR_HOME", str(core))
    for name in ("CLAUDE_CONFIG_DIR", "AI_COORDINATOR_ROLE", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    assert Path.home().resolve() == home.resolve(), "perfil não isolado no diretório temporário"
    return SimpleNamespace(home=home, core=core, tmp=tmp_path)


@pytest.fixture
def env(isolated):
    root = isolated.tmp / "profiles"
    profiles = DesktopProfileManager(root_dir=root)
    # AES-GCM with a fixture password: the same vault contract on every platform, never the operator's DPAPI vault.
    authorizer = DesktopAuthorizer(profile_manager=profiles, vault_backend="aes_gcm", master_password="fixture-only-password")
    token = authorizer.issue_session("default")
    manager = TerminalManager(authorizer=authorizer)
    server = SecureIPCServer(transport=MagicMock(), terminal_manager=manager, session_token=token)
    return SimpleNamespace(server=server, token=token, root=root, profiles=profiles, manager=manager,
                           profile_file=root / "default" / "profile.json", **vars(isolated))


def call(target, method, params=None, token=None):
    server = target.server if hasattr(target, "server") else target
    token = token or getattr(target, "token", TOKEN)
    payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}, "session_token": token}
    return server.handle_raw_payload(json.dumps(payload))


def update(target, category, values):
    return call(target, "settings.update", {"schema_version": 1, "category": category, "values": values})


def project(env, name="repo"):
    path = env.tmp / name
    path.mkdir(parents=True, exist_ok=True)
    assert call(env, "profile.authorize_project", {"project_path": str(path)}).error is None
    return path


# ---- K4: profile.revoke_project ----

def test_revoke_removes_authorization_and_clears_last_project(env):
    repo = project(env)
    assert call(env, "profile.remember_project", {"project_path": str(repo)}).result["remembered"] is True
    result = call(env, "profile.revoke_project", {"project_path": str(repo)}).result
    assert result == {"revoked": True, "authorized_projects": [], "last_project": None}
    active = call(env, "profile.get_active").result
    assert active["authorized_projects"] == [] and active["last_project"] is None
    assert env.profiles.is_project_authorized("default", repo) is False
    # Revoking again is a confirmed no-op, not an error.
    assert call(env, "profile.revoke_project", {"project_path": str(repo)}).result["revoked"] is False


def test_revoking_a_parent_clears_a_last_project_inside_it(env):
    parent = project(env, "work")
    inner = parent / "inner"
    inner.mkdir()
    assert call(env, "profile.remember_project", {"project_path": str(inner)}).error is None
    result = call(env, "profile.revoke_project", {"project_path": str(parent)}).result
    assert result["revoked"] is True and result["last_project"] is None


def test_revoke_with_a_registered_session_is_refused_and_changes_nothing(env):
    repo = project(env)
    (repo / "sub").mkdir()
    before = env.profile_file.read_bytes()
    # Only `cwd` is read by the guard; a registered session below the directory blocks the revocation.
    env.manager.sessions["fake-session"] = SimpleNamespace(cwd=repo / "sub")
    resp = call(env, "profile.revoke_project", {"project_path": str(repo)})
    assert resp.error["code"] == -32003
    assert resp.error["data"] == {"field": "project_path", "reason": "active_sessions"}
    assert env.profile_file.read_bytes() == before
    del env.manager.sessions["fake-session"]
    assert call(env, "profile.revoke_project", {"project_path": str(repo)}).result["revoked"] is True


def test_revoke_in_one_window_still_fences_new_terminals_in_another(env):
    """Each window is a process with its own terminal manager: the active_sessions refusal only sees this window's
    terminals (security.status says so), but the fence is read from disk, so no window opens a new terminal there."""
    repo = project(env)
    (repo / "sub").mkdir()
    other_authorizer = DesktopAuthorizer(profile_manager=DesktopProfileManager(root_dir=env.root),
                                         vault_backend="aes_gcm", master_password="fixture-only-password")
    other_token = other_authorizer.issue_session("default")
    other = TerminalManager(authorizer=other_authorizer)
    other.create_tab(other_token, "tab", "repo", repo)
    other.sessions["other-window-session"] = SimpleNamespace(cwd=repo / "sub")
    assert call(env, "profile.revoke_project", {"project_path": str(repo)}).result["revoked"] is True
    del other.sessions["other-window-session"]
    with pytest.raises(SecurityAuthorizationError):
        other.create_tab(other_token, "tab2", "sub", repo / "sub")
    with pytest.raises(SecurityAuthorizationError):
        other.create_terminal(other_token, "tab", 0, command=[sys.executable, "-c", "pass"])
    assert other.sessions == {}


def test_revoke_works_for_an_entry_that_can_no_longer_be_resolved(env, monkeypatch):
    """Contracts #8: a directory on a disconnected mapped drive makes Path.resolve raise (winerror 1328). Its listed
    text must still be revocable, and a terminal below it still blocks the revocation."""
    entry = str(env.tmp / "fake-net" / "proj")
    def change(profile):
        profile.authorized_projects = [entry]
        profile.last_project = entry
    env.profiles._mutate("default", change)
    real_resolve = Path.resolve
    def resolve(self, *args, **kwargs):
        if os.path.normcase(str(self)).startswith(os.path.normcase(entry)):
            raise OSError(None, "synthetic disconnected network drive", None, 1328)
        return real_resolve(self, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(OSError):
        Path(entry).resolve()
    env.manager.sessions["fake-session"] = SimpleNamespace(cwd=Path(entry) / "sub")
    refused = call(env, "profile.revoke_project", {"project_path": entry})
    assert refused.error["code"] == -32003 and refused.error["data"]["reason"] == "active_sessions"
    del env.manager.sessions["fake-session"]
    result = call(env, "profile.revoke_project", {"project_path": entry})
    assert result.error is None, result.error
    assert result.result == {"revoked": True, "authorized_projects": [], "last_project": None}
    active = env.profiles.get_active_profile()
    assert active.authorized_projects == [] and active.last_project is None


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b", 7, None])
def test_revoke_rejects_invalid_paths_with_structured_errors(env, bad):
    resp = call(env, "profile.revoke_project", {"project_path": bad})
    assert resp.error["code"] == -32602
    assert resp.error["data"] == {"field": "project_path", "reason": "invalid_value"}


@pytest.mark.skipif(sys.platform != "win32", reason="os.path.normcase only folds letter case on Windows")
def test_revoke_matches_letter_case_like_the_authorization_on_windows(env):
    repo = project(env, "CaseProj")
    listed = call(env, "profile.get_active").result["authorized_projects"]
    assert listed == [str(repo.resolve())]
    env.manager.sessions["fake-session"] = SimpleNamespace(cwd=Path(str(repo).lower()))
    assert call(env, "profile.revoke_project", {"project_path": str(repo).upper()}).error["data"]["reason"] == "active_sessions"
    del env.manager.sessions["fake-session"]
    assert call(env, "profile.revoke_project", {"project_path": str(repo).upper()}).result["revoked"] is True
    assert env.profiles.is_project_authorized("default", repo) is False


@pytest.mark.skipif(sys.platform != "win32", reason="renaming only the letter case keeps the same folder on Windows")
def test_folder_renamed_only_in_case_can_still_be_revoked_by_its_listed_text(env):
    repo = project(env, "renamed")
    listed = call(env, "profile.get_active").result["authorized_projects"][0]
    renamed = repo.with_name("RENAMED")
    os.rename(repo, renamed)
    assert call(env, "profile.revoke_project", {"project_path": listed}).result["revoked"] is True
    assert env.profiles.is_project_authorized("default", renamed) is False


def test_authorizing_an_equivalent_spelling_keeps_a_single_entry(env):
    repo = project(env)
    spelled = str(repo) + os.sep
    assert call(env, "profile.authorize_project", {"project_path": spelled}).error is None
    assert call(env, "profile.get_active").result["authorized_projects"] == [str(repo.resolve())]


# ---- K5: profile.remember_project ----

def test_remember_requires_an_authorized_existing_directory(env):
    loose = env.tmp / "not-authorized"
    loose.mkdir()
    before = env.profile_file.read_bytes()
    resp = call(env, "profile.remember_project", {"project_path": str(loose)})
    assert resp.error["code"] == -32003
    assert resp.error["data"] == {"field": "project_path", "reason": "not_authorized"}
    missing = call(env, "profile.remember_project", {"project_path": str(env.tmp / "missing")})
    assert missing.error["code"] == -32602
    assert missing.error["data"] == {"field": "project_path", "reason": "path_missing"}
    assert env.profile_file.read_bytes() == before


# ---- K3: profile.authorize_project and structured profile errors ----

def test_authorize_missing_or_invalid_directory_is_structured_and_does_not_echo_the_path(env):
    missing = env.tmp / f"{FAKE}-missing"
    resp = call(env, "profile.authorize_project", {"project_path": str(missing)})
    assert resp.error["code"] == -32602
    assert resp.error["data"] == {"field": "project_path", "reason": "path_missing"}
    assert FAKE not in json.dumps(resp.to_dict())
    file_path = env.tmp / "a-file.txt"
    file_path.write_text("x", encoding="utf-8")
    assert call(env, "profile.authorize_project", {"project_path": str(file_path)}).error["data"]["reason"] == "path_missing"
    for bad in ("", 5, None):
        resp = call(env, "profile.authorize_project", {"project_path": bad})
        assert resp.error["code"] == -32602 and resp.error["data"]["reason"] == "invalid_value"


def test_unrecoverable_profile_error_is_structured(env, monkeypatch):
    # A selection whose bytes cannot be read cannot be copied, so nothing is repaired or rewritten.
    env.profiles.active_file.write_bytes(b"default")
    real_read = Path.read_bytes
    def deny(self):
        if self.name == "active_profile.txt":
            raise OSError("synthetic read failure")
        return real_read(self)
    monkeypatch.setattr(Path, "read_bytes", deny)
    for method in ("profile.get_active", "settings.get", "security.status"):
        resp = call(env, method)
        assert resp.error["data"] == {"field": "profile", "reason": "profile_unreadable"}
        assert not resp.error["message"].lower().startswith("internal error")
    monkeypatch.undo()
    assert env.profiles.active_file.read_bytes() == b"default"
    assert list(env.root.glob("active_profile.txt.corrupt-*")) == []


# ---- K3: busy locks ----

HOLDER = r"""
import sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/src"]
from desktop.auth.file_lock import interprocess_lock
with interprocess_lock(Path(sys.argv[2])):
    print("held", flush=True)
    sys.stdin.readline()
"""


class HeldLock:
    """Another process holding a Desktop sidecar lock, like a second window."""

    def __init__(self, path: Path) -> None:
        self.proc = subprocess.Popen([sys.executable, "-B", "-c", HOLDER, str(ROOT), str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "held", self.proc.stderr.read()

    def release(self) -> None:
        if self.proc.poll() is None:
            self.proc.communicate("\n", timeout=20)


def test_profile_held_by_another_window_is_reported_busy(env, monkeypatch):
    monkeypatch.setattr(profile_mod, "interprocess_lock", partial(file_lock.interprocess_lock, timeout=0.3))
    before = env.profile_file.read_bytes()
    holder = HeldLock(env.profile_file.with_name("profile.lock"))
    try:
        resp = update(env, "terminal", {"font_size": 15})
        assert resp.error["code"] == server_mod.BUSY_CODE
        assert resp.error["data"] == {"field": "profile", "reason": "busy"}
        assert resp.error["message"] == "Outra janela está usando o perfil. Tente novamente."
    finally:
        holder.release()
    assert env.profile_file.read_bytes() == before
    assert update(env, "terminal", {"font_size": 15}).error is None


def test_vault_held_by_another_window_is_busy_not_unreadable(env, monkeypatch):
    monkeypatch.setattr(vault_mod, "LOCK_TIMEOUT", 0.3)
    monkeypatch.setattr(server_mod, "VAULT_STATUS_LOCK_TIMEOUT", 0.3)
    assert call(env, "vault.set_secret", {"key": "FIXTURE_KEY", "value": f"{FAKE}-value"}).error is None
    holder = HeldLock(env.root / "default" / "vault.enc.lock")
    try:
        assert call(env, "vault.status").result == {"available": False, "backend": "aes_gcm", "reason": "busy"}
        resp = call(env, "vault.set_secret", {"key": "OTHER_KEY", "value": f"{FAKE}-other"})
        assert resp.error["code"] == server_mod.BUSY_CODE
        assert resp.error["data"] == {"field": "vault", "reason": "busy"}
        assert call(env, "vault.list_keys").error["data"]["reason"] == "busy"
    finally:
        holder.release()
    assert call(env, "vault.status").result == {"available": True, "backend": "aes_gcm"}
    assert call(env, "vault.list_keys").result["keys"] == ["FIXTURE_KEY"]


@pytest.mark.parametrize("backend", ["aes_gcm", "dpapi"])
def test_unreadable_vault_is_reported_unreadable_and_never_overwritten(isolated, backend):
    """E2-3 (P1): a vault that cannot be decrypted is not available, list/set fail with a structured error and its
    bytes are kept. Reverting vault.status to {available: vault.available} makes this test fail."""
    if backend == "dpapi" and sys.platform != "win32":
        pytest.skip("DPAPI only exists on Windows")
    root = isolated.tmp / "profiles"
    profiles = DesktopProfileManager(root_dir=root)
    options = {"master_password": "fixture-only-password"} if backend == "aes_gcm" else {}
    authorizer = DesktopAuthorizer(profile_manager=profiles, vault_backend=backend, **options)
    token = authorizer.issue_session("default")
    server = SecureIPCServer(transport=MagicMock(), terminal_manager=TerminalManager(authorizer=authorizer), session_token=token)
    target = SimpleNamespace(server=server, token=token)
    vault_file = root / "default" / "vault.enc"
    garbage = b"\x00not-a-vault" + FAKE.encode("ascii") * 3
    vault_file.write_bytes(garbage)
    status = call(target, "vault.status")
    assert status.error is None, status.error
    assert status.result["available"] is False and status.result["reason"] == "unreadable"
    for method, params in (("vault.list_keys", {}), ("vault.set_secret", {"key": "FIXTURE_KEY", "value": f"{FAKE}-new"})):
        resp = call(target, method, params)
        assert resp.error is not None, resp.result
        assert resp.error["code"] == server_mod.VAULT_UNAVAILABLE_CODE
        assert resp.error["data"] == {"field": "vault", "reason": "unreadable"}
        assert FAKE not in json.dumps(resp.to_dict())
    assert FAKE not in json.dumps(status.to_dict())
    assert vault_file.read_bytes() == garbage


# ---- Vault: confirmation (E.1), blank values (E2-4), unpaired surrogates (E2-12) ----

def test_vault_confirmation_conflicts_are_structured(env):
    assert call(env, "vault.set_secret", {"key": "FIXTURE_KEY", "value": f"{FAKE}-one"}).error is None
    snapshot = call(env, "vault.list_keys").result
    exists = call(env, "vault.set_secret", {"key": "FIXTURE_KEY", "value": f"{FAKE}-two"})
    assert exists.error["code"] == -32005 and exists.error["data"] == {"field": "vault", "reason": "conflict"}
    assert call(env, "vault.set_secret", {"key": "OTHER_KEY", "value": f"{FAKE}-three"}).error is None
    stale = call(env, "vault.set_secret", {"key": "FIXTURE_KEY", "value": f"{FAKE}-two", "expected_revision": snapshot["revision"]})
    assert stale.error["code"] == -32005 and stale.error["data"]["reason"] == "conflict"
    current = call(env, "vault.list_keys").result["revision"]
    ok = call(env, "vault.set_secret", {"key": "FIXTURE_KEY", "value": f"{FAKE}-two", "expected_revision": current})
    assert ok.result == {"stored": True, "key": "FIXTURE_KEY"}
    assert FAKE not in json.dumps([r.to_dict() for r in (exists, stale, ok)])


def test_blank_credential_is_refused_and_surrounding_spaces_are_not_stored(env):
    for blank in ("", "   ", "\n\t "):
        resp = call(env, "vault.set_secret", {"key": "FIXTURE_KEY", "value": blank})
        assert resp.error["code"] == -32602 and resp.error["data"] == {"field": "value", "reason": "invalid_value"}
    assert call(env, "vault.list_keys").result["keys"] == []
    assert call(env, "vault.set_secret", {"key": "FIXTURE_KEY", "value": f"  {FAKE}-trimmed \r\n"}).error is None
    vault = env.manager.authorizer.get_vault("default")
    assert vault.get_secret("FIXTURE_KEY") == f"{FAKE}-trimmed"


def test_unpaired_surrogate_escape_is_refused_before_any_write(env):
    before = env.profile_file.read_bytes()
    for method, params in (("settings.update", {"schema_version": 1, "category": "perfil", "values": {"display_name": "x\udc00"}}),
                           ("vault.set_secret", {"key": "FIXTURE_KEY", "value": f"{FAKE}\ud800"})):
        line = json.dumps({"jsonrpc": "2.0", "id": "s", "method": method, "params": params, "session_token": env.token})
        assert "\\ud" in line  # only an escape can carry it; the host never writes one
        resp = env.server.handle_raw_payload(line)
        assert resp.error["code"] == -32600 and resp.error["data"] == {"field": "request", "reason": "lone_surrogate"}
        assert FAKE not in json.dumps(resp.to_dict())
    assert env.profile_file.read_bytes() == before
    assert call(env, "vault.list_keys").result["keys"] == []


# ---- K7: security.status ----

def test_security_status_never_claims_isolation_or_core_policy(env):
    repo = project(env)
    result = call(env, "security.status").result
    assert set(result) == {"authorized_projects", "broad_projects", "policy"}
    assert result["authorized_projects"] == [str(repo.resolve())]
    assert result["broad_projects"] == []  # L5: an ordinary project folder is not broad
    policy = result["policy"]
    assert set(policy) == {"project_fencing", "human_terminal", "governed_agent", "human_terminal_filesystem_isolation", "description"}
    assert policy["project_fencing"] is True
    assert policy["human_terminal_filesystem_isolation"] is False
    assert policy["governed_agent"] == "env_sanitized_only"
    assert "core_policy" not in json.dumps(policy)
    assert [key for key, value in policy.items() if value is True] == ["project_fencing"]
    description = policy["description"]
    assert "não há isolamento" in description and "não há sandbox" in description
    # Security #2: TerminalManager filters the agent environment by variable name only (secret, token, password,
    # key); a PAT, a URL with a password or SSH_AUTH_SOCK still reach the agent, and the text must say so.
    assert "sem variáveis de credenciais" not in description
    assert "outras credenciais" in description and "SSH_AUTH_SOCK" in description
    # Security #3: the active_sessions refusal only sees this window's terminals.
    assert "outra janela" in description


# ---- K6: discovery.agents and providers.routes ----

def write_routes(env, data):
    path = env.core / "routes.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def write_agent_configs(env):
    (env.home / ".codex").mkdir()
    (env.home / ".codex" / "config.toml").write_text(
        '[mcp_servers.codex-docs]\ncommand = "npx"\nargs = ["--token", "%s-toml-arg"]\n'
        'env = { API_KEY = "%s-toml-env" }\nurl = "https://user:%s-toml-pass@example.invalid/mcp"\n' % (FAKE, FAKE, FAKE),
        encoding="utf-8")
    (env.home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accessToken": f"{FAKE}-claude-oauth"},
        "mcpServers": {
            "claude-files": {"command": "npx", "args": ["--api-key", f"{FAKE}-claude-arg"],
                             "env": {"GITHUB_TOKEN": f"{FAKE}-claude-env"}},
            "remote-tool": {"type": "http", "url": f"https://example.invalid/mcp?token={FAKE}-claude-url",
                            "headers": {"Authorization": f"Bearer {FAKE}-claude-header"}},
        },
    }), encoding="utf-8")
    opencode = env.home / ".config" / "opencode"
    opencode.mkdir(parents=True)
    (opencode / "opencode.jsonc").write_text(
        '{\n  // comment with %s-comment\n  "mcp": {\n    "oc-search": {"url": "https://x.invalid/a//b?key=%s-oc-url",'
        ' "environment": {"SECRET": "%s-oc-env",},},\n  },\n  /* block %s-block */\n}\n' % (FAKE, FAKE, FAKE, FAKE),
        encoding="utf-8")


def test_discovery_reports_names_only_never_values(env):
    write_routes(env, {
        "launchers": {"claude": [sys.executable, "-c", "pass", f"--token={FAKE}-launcher-arg"],
                      "codex": [str(env.tmp / f"{FAKE}-missing" / "codex.exe"), f"--api-key={FAKE}-codex-arg"],
                      "opencode": f"opencode --password {FAKE}-string-launcher"},
        "worker_environment": {"OPENAI_API_KEY": f"{FAKE}-worker-env"},
        "routes": {"code": {"candidates": [{"client": "claude", "model": "fixture-model", "effort": "high",
                                            "api_key": f"{FAKE}-route-key", "url": f"https://{FAKE}.invalid/v1"}]}},
    })
    write_agent_configs(env)
    result = call(env, "discovery.agents").result
    text = json.dumps(result, ensure_ascii=False)
    assert FAKE not in text
    for private in (sys.executable, str(env.tmp), str(env.home)):
        assert private not in text and json.dumps(private)[1:-1] not in text
    assert result["source"] == "configured"
    assert result["mcp_servers"] == ["claude-files", "codex-docs", "oc-search", "remote-tool"]
    assert result["mcp_complete"] is True
    agents = {agent["client"]: agent for agent in result["agents"]}
    assert set(agents) <= {"codex", "claude", "opencode", "agy"}
    assert agents["claude"]["available"] is True
    assert agents["claude"]["reason"] == "Launcher encontrado; autenticação e cota não verificadas."
    assert agents["codex"]["available"] is False
    assert agents["codex"]["reason"] == "Executável ou script do launcher não encontrado."
    assert agents["opencode"]["available"] is False
    assert agents["opencode"]["reason"] == "Launcher com formato inválido na configuração."
    assert set(agents["claude"]) == {"name", "client", "available", "reason", "worker_headless", "coordinator_headless"}
    routes = call(env, "providers.routes").result
    assert FAKE not in json.dumps(routes)
    assert routes["routes"] == [{"category": "code", "candidates": [{"client": "claude", "model": "fixture-model", "effort": "high"}]}]
    assert routes["available"] is True and routes["source"] == "configured"
    assert sorted(p.name for p in env.core.iterdir()) == ["routes.json"]  # the Core data folder is not initialized


@pytest.mark.parametrize("client", [["bad"], {"x": 1}, 5, None, True])
def test_route_candidate_with_a_non_text_client_is_skipped_not_fatal(env, client):
    """Security #5: an unhashable client made providers.routes fail as a whole (-32603)."""
    write_routes(env, {"routes": {"code": {"candidates": [{"client": "codex", "model": "gpt-5"},
                                                          {"client": client, "model": "m"}]}}})
    resp = call(env, "providers.routes")
    assert resp.error is None, resp.error
    assert resp.result["available"] is True and resp.result["source"] == "configured"
    assert resp.result["routes"] == [{"category": "code", "candidates": [{"client": "codex", "model": "gpt-5"}]}]
    assert call(env, "discovery.agents").error is None


@pytest.mark.parametrize("launchers", [[], "codex", 5, {"codex": {"cmd": "x"}}, {"codex": [1, 2]}, {"codex": [""]}])
def test_malformed_launchers_never_break_discovery(env, launchers):
    write_routes(env, {"routes": {"code": {"candidates": []}}, "launchers": launchers})
    resp = call(env, "discovery.agents")
    assert resp.error is None, resp.error
    assert all(agent["available"] is False for agent in resp.result["agents"])
    assert call(env, "providers.routes").result["available"] is True


@pytest.mark.parametrize("contents", ["{not json", "[]", json.dumps({"routes": []}), "\x00\x01"])
def test_unreadable_routes_are_reported_honestly(env, contents):
    write_routes(env, contents)
    agents = call(env, "discovery.agents").result
    assert agents["source"] == "unreadable"
    assert {agent["reason"] for agent in agents["agents"]} == {"Configuração de rotas ilegível; launcher não verificado."}
    routes = call(env, "providers.routes").result
    assert routes["available"] is False and routes["source"] == "unreadable" and routes["routes"] == []
    assert routes["reason"] == "Configuração de rotas ilegível; nada foi alterado."


def test_packaged_routes_are_used_when_the_operator_has_none(env):
    routes = call(env, "providers.routes").result
    assert routes["available"] is True and routes["source"] == "packaged_defaults"
    assert routes["routes"] and all(set(route) == {"category", "candidates"} for route in routes["routes"])
    assert call(env, "discovery.agents").result["source"] == "packaged_defaults"
    assert list(env.core.iterdir()) == []


def test_malformed_mcp_source_marks_the_list_incomplete_without_failing(env):
    write_agent_configs(env)
    (env.home / ".claude.json").write_text('{"mcpServers": {"x": ', encoding="utf-8")
    result = call(env, "discovery.agents").result
    assert result["mcp_complete"] is False
    assert result["mcp_servers"] == ["codex-docs", "oc-search"]


def test_jsonc_with_many_commas_is_linear():
    from desktop.ipc.discovery import MAX_CONFIG_BYTES, _jsonc
    assert _jsonc('{"a": [1, 2,], "u": "http://x//y, ]", /* c, } */ "b": {"c": 1,},}') == {"a": [1, 2], "u": "http://x//y, ]", "b": {"c": 1}}
    text = '{"mcp": {"big": [' + "[1,]," * 200_000 + '[]]}}'
    assert len(text.encode()) <= MAX_CONFIG_BYTES
    started = time.perf_counter()
    assert len(_jsonc(text)["mcp"]["big"]) == 200_001
    # About 0.7 s here; the quadratic version needed 9 to 45 s for the same megabyte.
    assert time.perf_counter() - started < 3


def test_discovery_with_a_one_megabyte_jsonc_answers_quickly(env):
    opencode = env.home / ".config" / "opencode"
    opencode.mkdir(parents=True)
    body = '{"mcp": {"fast": {"x": [' + "1," * 500_000 + "1]}}}"
    (opencode / "opencode.jsonc").write_text(body, encoding="utf-8")
    started = time.perf_counter()
    result = call(env, "discovery.agents").result
    assert time.perf_counter() - started < 5  # the quadratic parser blocked the IPC loop for 9 to 45 s
    assert result["mcp_servers"] == ["fast"] and result["mcp_complete"] is True


# ---- K1/E2-6: stored settings never block reading ----

def test_future_schema_is_read_only_and_update_changes_no_byte(env):
    env.profiles.update_settings("default", {"schema_version": 2, "values": {"terminal": {"font_size": 21}}, "future": [1]})
    before = env.profile_file.read_bytes()
    snap = call(env, "settings.get").result
    assert snap["read_only"] is True and snap["stored_schema_version"] == 2
    assert snap["stored_issue"]["reason"] == "incompatible_version" and snap["stored_issue"]["message"]
    assert all(category["validated"] is False for category in snap["categories"].values())
    resp = update(env, "terminal", {"font_size": 15})
    assert resp.error == {"code": -32602,
                          "message": "As configurações foram gravadas por outra versão e estão protegidas. Nada foi alterado.",
                          "data": {"field": "settings", "reason": "read_only_stored_schema"}}
    assert env.profile_file.read_bytes() == before


def test_invalid_stored_field_is_shown_as_default_preserved_and_others_stay_writable(env):
    env.profiles.update_settings("default", {"schema_version": 1, "values": {"terminal": {"font_size": 99}}})
    snap = call(env, "settings.get").result
    terminal = snap["categories"]["terminal"]
    assert snap["read_only"] is False and snap["stored_issue"]["reason"] == "invalid_stored_value"
    assert terminal["invalid_fields"] == ["font_size"] and terminal["values"]["font_size"] == 13
    assert terminal["validated"] is False and terminal["saved_fields"] == ["font_size"]
    assert snap["categories"]["aparencia"]["invalid_fields"] == [] and snap["categories"]["aparencia"]["validated"] is True
    assert update(env, "terminal", {"cursor_blink": False}).error is None
    stored = json.loads(env.profile_file.read_text(encoding="utf-8"))["settings"]["values"]["terminal"]
    assert stored == {"font_size": 99, "cursor_blink": False}
    fixed = update(env, "terminal", {"font_size": 14}).result
    assert fixed["categories"]["terminal"]["invalid_fields"] == [] and fixed["stored_issue"] is None


def test_first_migrating_write_keeps_a_byte_copy(env):
    env.profiles.update_settings("default", {"terminal": {"font_size": 17}, "legacy_plugin": {"keep": True}})
    legacy = env.profile_file.read_bytes()
    assert call(env, "settings.get").result["migration"]["required"] is True
    assert update(env, "aparencia", {"density": "compact"}).error is None
    assert env.profile_file.with_name("profile.json.pre-v1.bak").read_bytes() == legacy
    saved = json.loads(env.profile_file.read_text(encoding="utf-8"))["settings"]
    assert saved["schema_version"] == 1 and saved["values"]["terminal"] == {"font_size": 17}
    assert saved["legacy_plugin"] == {"keep": True}


def app_server(isolated):
    """A server as the host starts it: profiles under the (temporary) home, created lazily on the first call."""
    return SecureIPCServer(transport=MagicMock(), session_token=TOKEN)


def test_corrupt_active_profile_is_quarantined_and_the_app_opens(isolated):
    root = isolated.home / ".ai-coordinator" / "desktop" / "profiles"
    DesktopProfileManager(root_dir=root)
    profile_file = root / "default" / "profile.json"
    corrupt = ('{"name": "default", "authorized_projects": ["%s-path"], "settings": {"values": ' % FAKE).encode("utf-8")
    profile_file.write_bytes(corrupt)
    server = app_server(isolated)
    active = call(server, "profile.get_active")
    assert active.error is None, active.error
    recovery = active.result["recovery"]
    assert recovery["reason"] == "profile_unreadable" and recovery["message"]
    assert re.fullmatch(r"profile\.json\.corrupt-\d{8}T\d{6}Z(-\d+)?", recovery["backup_file"])
    assert (root / "default" / recovery["backup_file"]).read_bytes() == corrupt
    assert active.result["authorized_projects"] == [] and active.result["last_project"] is None
    assert FAKE not in json.dumps(active.result)
    snap = call(server, "settings.get").result
    assert snap["read_only"] is False and snap["stored_issue"] is None
    assert call(server, "security.status").result["authorized_projects"] == []
    repo = isolated.tmp / "repo"
    repo.mkdir()
    assert call(server, "profile.authorize_project", {"project_path": str(repo)}).error is None
    assert (root / "default" / recovery["backup_file"]).read_bytes() == corrupt  # never overwritten


def test_corrupt_inactive_profile_is_ignored(isolated):
    root = isolated.home / ".ai-coordinator" / "desktop" / "profiles"
    DesktopProfileManager(root_dir=root)
    other = root / "work" / "profile.json"
    other.parent.mkdir()
    other.write_bytes(b'{"name":')
    server = app_server(isolated)
    active = call(server, "profile.get_active")
    assert active.error is None and active.result["name"] == "default" and active.result["recovery"] is None
    assert call(server, "settings.get").error is None
    assert other.read_bytes() == b'{"name":'
    assert sorted(p.name for p in other.parent.iterdir()) == ["profile.json"]


@pytest.mark.parametrize("contents,repaired", [
    (b"", True), (b"\xff\xfe\x00", True), (b"ghost", True), (b"\x00\x00", True), ("default".encode("utf-16"), True),
    (b"\xef\xbb\xbfdefault", False), (b"default\r\n", False)],
    ids=["empty", "invalid_utf8", "dangling", "nul", "utf16_out_file", "bom", "crlf"])
def test_app_opens_whatever_the_active_selection_contains(isolated, contents, repaired):
    """Security #4: an empty, unreadable or dangling active_profile.txt made every IPC method fail (the app could
    not open). The default profile is opened, the selection is kept byte for byte and a missing profile is never
    created."""
    root = isolated.home / ".ai-coordinator" / "desktop" / "profiles"
    DesktopProfileManager(root_dir=root)
    profile_bytes = (root / "default" / "profile.json").read_bytes()
    (root / "active_profile.txt").write_bytes(contents)
    server = app_server(isolated)
    for method in ("profile.get_active", "terminal.list_tabs", "settings.get", "vault.status", "security.status"):
        resp = call(server, method)
        assert resp.error is None, (method, resp.error)
    active = call(server, "profile.get_active").result
    assert active["name"] == "default"
    assert (root / "default" / "profile.json").read_bytes() == profile_bytes
    assert not (root / "ghost").exists()
    copies = sorted(root.glob("active_profile.txt.corrupt-*"))
    if repaired:
        recovery = active["recovery"]
        assert recovery["reason"] == "active_selection_invalid" and recovery["message"]
        assert re.fullmatch(r"active_profile\.txt\.corrupt-\d{8}T\d{6}Z(-\d+)?", recovery["backup_file"])
        assert [c.name for c in copies] == [recovery["backup_file"]] and copies[0].read_bytes() == contents
        assert "ghost" not in json.dumps(recovery)
        assert (root / "active_profile.txt").read_bytes() == b"default"
    else:
        assert active["recovery"] is None and copies == []
        assert (root / "active_profile.txt").read_bytes() == contents


# ---- CM-5: every command the interface calls exists in the host and in the Core IPC ----

def _tauri_commands(main_rs: str) -> set:
    return set(re.findall(r"#\[tauri::command\]\s*(?:pub\s+)?(?:async\s+)?fn\s+([a-z_0-9]+)\s*[(<]", main_rs))


def _interface_commands() -> set:
    found = set()
    patterns = [
        r"""callTauri\(\s*["']([a-z_0-9]+)["']""",
        r"""\binvoke(?:<[^>()]*>)?\(\s*["']([a-z_0-9]+)["']""",
        r"""\(\s*callTauri\s*,\s*["']([a-z_0-9]+)["']""",
    ]
    for source in (ROOT / "desktop" / "src").rglob("*.ts*"):
        if re.search(r"\.(test|spec)\.tsx?$", source.name) or source.suffix not in (".ts", ".tsx"):
            continue
        text = source.read_text(encoding="utf-8")
        for pattern in patterns:
            found.update(re.findall(pattern, text))
        # Command tables such as `const COMMAND = { agentes: "discovery_agents", ... }`.
        for table in re.findall(r"\bCOMMANDS?\b[^=;]*=\s*\{([^}]*)\}", text):
            found.update(re.findall(r"""["']([a-z]+(?:_[a-z0-9]+)+)["']""", table))
    return found


def test_interface_commands_exist_in_the_host_and_reach_registered_core_methods():
    main_rs = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    server_py = (ROOT / "desktop" / "ipc" / "server.py").read_text(encoding="utf-8")
    commands = _tauri_commands(main_rs)
    handler_block = main_rs[main_rs.index("generate_handler![") :]
    handler_block = handler_block[: handler_block.index("]")]
    handlers = set(re.findall(r"[a-z_0-9]+", handler_block.split("[", 1)[1]))
    assert commands == handlers, {"not_registered": sorted(commands - handlers), "not_defined": sorted(handlers - commands)}

    registered = set(re.findall(r"""self\.register\(\s*["']([a-z_.]+)["']""", server_py))
    forwarded = set(re.findall(r"""\.call(?:_structured|_locked)?\(\s*["']([a-z_.]+)["']""", main_rs))
    assert forwarded and forwarded <= registered, sorted(forwarded - registered)

    used = _interface_commands()
    expected = {"settings_get", "settings_update", "vault_status", "vault_list_keys", "vault_set_secret",
                "vault_delete_secret", "profile_get_active", "profile_authorize_project", "profile_revoke_project",
                "profile_remember_project", "security_status", "discovery_agents", "providers_routes",
                "terminal_create_tab", "terminal_create_session"}
    assert expected <= used, sorted(expected - used)  # the scanner itself still finds the known calls
    assert used <= commands, sorted(used - commands)
    for command in sorted(used):
        body = main_rs[re.search(rf"fn\s+{command}\s*[(<]", main_rs).start():]
        body = body[: body.index("\n}\n")]
        for method in re.findall(r"""\.call(?:_structured|_locked)?\(\s*["']([a-z_.]+)["']""", body):
            assert method in registered, (command, method)


# ---- CM-5 (Etapa 8): argument names; Tauri 2 renames each Rust parameter to camelCase ----

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _desktop_static as static  # noqa: E402

CORE_METHOD = re.compile(
    r"""[(,]\s*"((?:system|profile|security|discovery|providers|terminal|settings|vault|telemetry|mission)\.[a-z_]+)"\s*,""")


def test_static_reader_understands_the_argument_forms_in_use():
    keys = static.js_object_keys('{ tabId, projectPath: path, "kind": k, // note, x: 1\n cols: f(a, { b: 1 }), '
                                 "...(rev !== undefined ? { expectedRevision: rev } : {}), "
                                 "...(c && { confirmBroad: true }), }")
    assert keys.keys == {"tabId", "projectPath", "kind", "cols"}
    assert keys.conditional == {"expectedRevision", "confirmBroad"}
    assert keys.unresolved == []
    odd = static.js_object_keys("{ [name]: 1, ...extra }")
    assert odd.keys == set() and len(odd.unresolved) == 2
    fn = static.rust_functions("#[tauri::command]\nasync fn x(project_path: String, // c, d\n "
                               "confirm_broad: Option<bool>, bridge: State<'_, Arc<IpcBridge>>) "
                               "-> Result<Value, Value> { Ok(json!({})) }\n")[0]
    assert fn.is_command and fn.is_async and fn.returns == "Result<Value, Value>"
    assert static.expected_keys(fn) == {"projectPath": ("project_path", False), "confirmBroad": ("confirm_broad", True)}


def test_interface_arguments_match_the_host_command_parameters():
    """A key the host does not declare is silently dropped and a missing non-Option parameter rejects the call; neither
    shows before the native build runs. For every literal callTauri/invoke in desktop/src (tests excluded), each key
    must be the camelCase name of a parameter of the #[tauri::command] in main.rs (State/AppHandle/Window are injected
    and never sent), and every non-Option parameter must be sent unconditionally."""
    analysis = static.analyze_ipc(static.MAIN_RS.read_text(encoding="utf-8"))
    sites = static.interface_call_sites()
    problems = []
    for site in sites:
        fn = analysis.commands.get(site.command)
        if fn is None:
            problems.append(f"{site.where()}: o comando {site.command} não existe em main.rs")
            continue
        expected = static.expected_keys(fn)
        for entry in site.args.unresolved:
            problems.append(f"{site.where()}: argumento de {site.command} ilegível estaticamente: {entry!r}")
        for key in sorted((site.args.keys | site.args.conditional) - set(expected)):
            problems.append(f"{site.where()}: {site.command} recebe '{key}', que não é parâmetro do comando "
                            f"(aceitos: {sorted(expected)})")
        for key in sorted(k for k, (_, optional) in expected.items() if not optional and k not in site.args.keys):
            problems.append(f"{site.where()}: {site.command} não envia sempre o parâmetro obrigatório '{key}'")
    assert not problems, "\n".join(problems)
    with_args = {site.command for site in sites if site.has_args}
    # The reader still sees the known calls with their arguments.
    assert {"terminal_create_tab", "terminal_create_session", "terminal_get_output", "settings_update",
            "vault_set_secret", "vault_delete_secret", "profile_authorize_project", "profile_revoke_project",
            "profile_remember_project", "updater_download_and_install"} <= with_args


def test_interface_sends_the_etapa8_optional_arguments():
    """L7 reaches the host: the broad-scope confirmation and the revision of the vault removal are really sent."""
    sent: dict = {}
    for site in static.interface_call_sites():
        sent.setdefault(site.command, set()).update(site.args.keys | site.args.conditional)
    assert "confirmBroad" in sent.get("profile_authorize_project", set()), sent.get("profile_authorize_project")
    assert "expectedRevision" in sent.get("vault_delete_secret", set()), sent.get("vault_delete_secret")


def test_every_host_command_that_reaches_the_core_forwards_registered_methods():
    """Also through helpers (count_active_sessions, a spawn_blocking wrapper): every Core method a command forwards is
    registered in desktop/ipc/server.py."""
    main_rs = static.MAIN_RS.read_text(encoding="utf-8")
    registered = set(re.findall(r"""self\.register\(\s*["']([a-z_.]+)["']""",
                                (ROOT / "desktop" / "ipc" / "server.py").read_text(encoding="utf-8")))
    analysis = static.analyze_ipc(main_rs)
    for name, fn in sorted(analysis.commands.items()):
        if not analysis.uses_ipc(fn):
            continue
        methods = set(CORE_METHOD.findall(fn.body)) or set(CORE_METHOD.findall(analysis.expanded_body(fn)))
        assert methods, f"{name}: nenhum método do Core encontrado"
        assert methods <= registered, (name, sorted(methods - registered))
