"""Regression coverage for installed shell discovery and supervised human-terminal startup."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.server import SecureIPCServer
from desktop.terminal import manager as manager_module
from desktop.terminal.interpreters import Interpreter, InterpreterError, discover_interpreters, resolve_interpreter, windows_to_wsl_path
from desktop.terminal.manager import TerminalManager, TerminalStartError
from desktop.terminal.pty_backend import BasePtyBackend


class RecordingBackend(BasePtyBackend):
    def __init__(self, fail: Exception | None = None) -> None:
        super().__init__()
        self.fail = fail
        self.spawn_args = None
        self.alive = False

    def spawn(self, cmd, cwd, env=None, max_memory_mb=256):
        if self.fail:
            raise self.fail
        self.spawn_args = (cmd, cwd, env, max_memory_mb)
        self.alive = True
        return SimpleNamespace(pid=None)

    def write(self, data): return len(data)
    def resize(self, cols, rows): return True
    def is_alive(self): return self.alive
    def get_exit_code(self): return None
    def terminate(self): self.alive = False; return True


@pytest.fixture
def terminal_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AI_COORDINATOR_HOME", str(tmp_path / "core"))
    profiles = DesktopProfileManager(root_dir=tmp_path / "profiles")
    authorizer = DesktopAuthorizer(profile_manager=profiles)
    token = authorizer.issue_session("default")
    project = tmp_path / "repo"
    project.mkdir()
    profiles.authorize_project("default", project)
    manager = TerminalManager(authorizer)
    manager.create_tab(token, "tab", "project", project)
    return SimpleNamespace(manager=manager, token=token, project=project)


def test_windows_shell_discovery_includes_only_present_interpreters():
    known = {
        Path(r"C:\Windows\System32\cmd.exe"),
        Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Windows\System32\wsl.exe"),
    }
    found = discover_interpreters(
        platform="win32",
        environ={"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)"},
        which=lambda _name: None,
        exists=lambda path: path in known,
        wsl_registered=True,
    )
    assert [(item.id, item.label) for item in found] == [
        ("cmd", "CMD"), ("powershell", "PowerShell 7"), ("bash_git", "Bash (Git Bash)"), ("bash_wsl", "Bash (WSL)")
    ]


def test_powershell_falls_back_to_windows_powershell_and_bash_requires_availability():
    exists = {Path(r"C:\Windows\System32\cmd.exe"), Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")}
    found = discover_interpreters(
        platform="win32",
        environ={"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)"},
        which=lambda _name: None,
        exists=lambda path: path in exists,
        wsl_registered=False,
    )
    assert [item.id for item in found] == ["cmd", "powershell"]
    with pytest.raises(InterpreterError):
        resolve_interpreter("bash_wsl", r"C:\work", available=found)


def test_git_bash_keeps_the_windows_directory_and_wsl_maps_drive_paths():
    git = Interpreter("bash_git", "Bash (Git Bash)", r"C:\Git\bin\bash.exe", ("--login", "-i"))
    resolved, command, cwd, extra_env = resolve_interpreter("bash_git", r"D:\ai-coordinator", [git])
    assert resolved is git and command == [git.executable, "--login", "-i"]
    assert cwd == r"D:\ai-coordinator" and extra_env == {"CHERE_INVOKING": "1"}
    wsl = Interpreter("bash_wsl", "Bash (WSL)", r"C:\Windows\System32\wsl.exe", cwd_kind="wsl")
    _, command, cwd, _ = resolve_interpreter("bash_wsl", r"C:\Users\10864", [wsl])
    assert command == [wsl.executable, "--cd", "/mnt/c/Users/10864", "--exec", "bash", "-l"]
    assert cwd == r"C:\Windows"
    with pytest.raises(InterpreterError):
        windows_to_wsl_path(r"\\server\share\project")


def test_default_human_terminal_uses_authorized_powershell_and_keeps_supervision(terminal_env, monkeypatch):
    pwsh = Interpreter("powershell", "PowerShell 7", r"C:\Program Files\PowerShell\7\pwsh.exe", ("-NoLogo",))
    monkeypatch.setattr(manager_module.sys, "platform", "win32")
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: [pwsh])
    backend = RecordingBackend()
    session = terminal_env.manager.create_terminal(
        terminal_env.token, "tab", 0, cwd=terminal_env.project, backend=backend
    )
    assert backend.spawn_args[0] == [pwsh.executable, "-NoLogo"]
    assert backend.spawn_args[3] is None
    assert session.interpreter_id == "powershell" and session.interpreter_label == "PowerShell 7"
    assert terminal_env.manager.get_session(session.session_id) is session
    assert terminal_env.manager.list_interpreters(terminal_env.token) == [{"id": "powershell", "label": "PowerShell 7"}]


def test_unavailable_interpreter_fails_before_spawn(terminal_env, monkeypatch):
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: [Interpreter("cmd", "CMD", "cmd.exe")])
    backend = RecordingBackend()
    with pytest.raises(manager_module.TerminalManagerError):
        terminal_env.manager.create_terminal(
            terminal_env.token, "tab", 0, interpreter_id="powershell", cwd=terminal_env.project, backend=backend
        )
    assert backend.spawn_args is None


def test_spawn_failure_preserves_original_type_and_safe_numeric_code(terminal_env, monkeypatch):
    pwsh = Interpreter("powershell", "PowerShell", "pwsh.exe")
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: [pwsh])
    class SpawnFailure(Exception):
        errno = 13

    error = SpawnFailure("raw path and environment must not be logged")
    with pytest.raises(TerminalStartError) as raised:
        terminal_env.manager.create_terminal(
            terminal_env.token, "tab", 0, cwd=terminal_env.project, backend=RecordingBackend(error)
        )
    assert raised.value.diagnostic() == {"stage": "spawn", "exception_type": "SpawnFailure", "error_code": 13}
    assert "raw path" not in str(raised.value)


def test_ipc_logs_terminal_failure_stage_without_raw_exception_or_path(terminal_env, monkeypatch):
    pwsh = Interpreter("powershell", "PowerShell", "pwsh.exe")
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: [pwsh])
    transport = MagicMock()
    server = SecureIPCServer(transport=transport, terminal_manager=terminal_env.manager, session_token=terminal_env.token)
    monkeypatch.setattr(terminal_env.manager, "create_terminal", lambda **_kwargs: (_ for _ in ()).throw(
        TerminalStartError("spawn", "PermissionError", 13)
    ))
    payload = {"jsonrpc": "2.0", "id": 1, "method": "terminal.create_session", "session_token": terminal_env.token,
               "params": {"tab_id": "tab", "pane_id": 0, "cwd": str(terminal_env.project)}}
    response = server.handle_raw_payload(json.dumps(payload))
    assert response.error["message"] == "Terminal operation failed"
    assert response.error["data"] == {
        "field": "terminal",
        "reason": "start_failed",
        "stage": "spawn",
        "exception_type": "PermissionError",
        "error_code": 13,
    }
    diagnostic = transport.log_diagnostic.call_args.args[0]
    assert "stage=spawn" in diagnostic and "exception_type=PermissionError" in diagnostic and "error_code=13" in diagnostic
    assert str(terminal_env.project) not in diagnostic and "secret" not in diagnostic


def test_open_initial_project_then_second_project_preserves_both_sessions_and_cwds(tmp_path, monkeypatch):
    home_proj = tmp_path / "10864"
    repo_proj = tmp_path / "ai-coordinator"
    home_proj.mkdir()
    repo_proj.mkdir()
    profiles = DesktopProfileManager(root_dir=tmp_path / "profiles")
    authorizer = DesktopAuthorizer(profile_manager=profiles)
    token = authorizer.issue_session("default")
    profiles.authorize_project("default", home_proj)
    profiles.authorize_project("default", repo_proj)

    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    monkeypatch.setattr(manager_module.sys, "platform", "win32")
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: [
        Interpreter("powershell", "PythonShell", sys.executable, ("-u", "-c", "import time; time.sleep(10)"))
    ])
    manager = TerminalManager(authorizer)
    server = SecureIPCServer(transport=MagicMock(), terminal_manager=manager, session_token=token)
    try:
        resp_tab1 = server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "terminal.create_tab", "session_token": token,
            "params": {"tab_id": "tab-main", "project_path": str(home_proj)},
        }))
        assert resp_tab1.error is None

        # Same tab_id + same canonical project_path is idempotent on reconnect/re-init.
        resp_tab1_again = server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "terminal.create_tab", "session_token": token,
            "params": {"tab_id": "tab-main", "project_path": str(home_proj)},
        }))
        assert resp_tab1_again.error is None

        resp_sess1 = server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "terminal.create_session", "session_token": token,
            "params": {"tab_id": "tab-main", "pane_id": 0, "kind": "human_terminal", "cwd": str(home_proj)},
        }))
        assert resp_sess1.error is None

        # Reusing tab-main for a DIFFERENT project returns explicit structured tab_exists without clobbering tab-main.
        resp_conflict = server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "terminal.create_tab", "session_token": token,
            "params": {"tab_id": "tab-main", "project_path": str(repo_proj)},
        }))
        assert resp_conflict.error["data"] == {"field": "tab_id", "reason": "tab_exists"}

        resp_tab2 = server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 5, "method": "terminal.create_tab", "session_token": token,
            "params": {"tab_id": "tab-2", "project_path": str(repo_proj)},
        }))
        assert resp_tab2.error is None

        resp_sess2 = server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 6, "method": "terminal.create_session", "session_token": token,
            "params": {"tab_id": "tab-2", "pane_id": 1, "kind": "human_terminal", "cwd": str(repo_proj)},
        }))
        assert resp_sess2.error is None

        listed = server.handle_raw_payload(json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "terminal.list_tabs", "session_token": token, "params": {},
        }))
        tabs_by_id = {t["tab_id"]: t for t in listed.result["tabs"]}
        assert tabs_by_id["tab-main"]["panes"]["0"]["cwd"] == str(home_proj.resolve())
        assert tabs_by_id["tab-main"]["panes"]["0"]["is_alive"] is True
        assert tabs_by_id["tab-2"]["panes"]["1"]["cwd"] == str(repo_proj.resolve())
        assert tabs_by_id["tab-2"]["panes"]["1"]["is_alive"] is True
    finally:
        manager.terminate_all()


def test_isolated_profile_preserves_path_and_keeps_logical_authorized_cwd(tmp_path, monkeypatch):
    import os
    isolated_home = tmp_path / "perfil-diag"
    isolated_home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    launch_dir = tmp_path / "launch-cwd"
    launch_dir.mkdir()
    monkeypatch.setenv("USERPROFILE", str(isolated_home))
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("PATH", f"{tmp_path / 'cli-bin'};{os.environ.get('PATH', '')}")

    profiles = DesktopProfileManager(root_dir=tmp_path / "profiles")
    authorizer = DesktopAuthorizer(profile_manager=profiles)
    token = authorizer.issue_session("default")
    profiles.authorize_project("default", proj)

    monkeypatch.setattr(manager_module.sys, "platform", "win32")
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: [
        Interpreter("bash_wsl", "Bash (WSL)", "wsl.exe", cwd_kind="wsl")
    ])
    monkeypatch.setattr(manager_module, "resolve_interpreter", lambda *_args, **_kwargs: (
        Interpreter("bash_wsl", "Bash (WSL)", "wsl.exe", cwd_kind="wsl"),
        ["wsl.exe", "--cd", "/mnt/c/proj"],
        str(launch_dir),
        {},
    ))
    backend = RecordingBackend()
    manager = TerminalManager(authorizer)
    manager.create_tab(token, "tab", "project", proj)
    session = manager.create_terminal(token, "tab", 0, interpreter_id="bash_wsl", cwd=proj, backend=backend)

    assert backend.spawn_args[1] == launch_dir.resolve()
    assert session.cwd == proj.resolve()
    assert backend.spawn_args[2]["USERPROFILE"] == str(isolated_home)
    assert str(tmp_path / "cli-bin") in backend.spawn_args[2]["PATH"]


def test_ipc_lists_installed_interpreters_and_rejects_non_allowlisted_ids(terminal_env, monkeypatch):
    pwsh = Interpreter("powershell", "PowerShell 7", "pwsh.exe")
    monkeypatch.setattr(manager_module, "discover_interpreters", lambda: [pwsh])
    server = SecureIPCServer(transport=MagicMock(), terminal_manager=terminal_env.manager, session_token=terminal_env.token)
    listed = server.handle_raw_payload(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "terminal.list_interpreters",
                                                   "session_token": terminal_env.token, "params": {}}))
    assert listed.result == {"interpreters": [{"id": "powershell", "label": "PowerShell 7"}]}
    rejected = server.handle_raw_payload(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "terminal.create_session",
                                                     "session_token": terminal_env.token,
                                                     "params": {"tab_id": "tab", "pane_id": 0, "interpreter_id": "C:\\malware.exe"}}))
    assert rejected.error["data"] == {"field": "interpreter_id", "reason": "invalid_value"}
