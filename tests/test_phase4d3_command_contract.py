"""Marco 4D.3, Etapa 3 — contract of the terminal `command` parameter between the Tauri host and the Core IPC.

The host used to send `command` as a single string while the Python side expects an argv list; a string reaching
the ConPTY backend was split by list2cmdline into one argument per character. Now:
- the host types it as Option<Vec<String>> (a string fails deserialization);
- the IPC server validates it and rejects anything that is not a non-empty argv list with INVALID_PARAMS,
  a structured error ({"field": "command", "reason": ...}), creating no session.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

import pytest

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.protocol import RPCErrorCode
from desktop.ipc.server import SecureIPCServer, validate_command_argv
from desktop.terminal.manager import TerminalManager


def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture
def ipc_env(monkeypatch):
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        profiles = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=profiles)
        token = authorizer.issue_session("default")
        project = tmp_path / "proj"
        project.mkdir()
        profiles.authorize_project("default", project)
        manager = TerminalManager(authorizer=authorizer)
        server = SecureIPCServer(transport=MagicMock(), terminal_manager=manager, session_token=token)
        try:
            yield {"server": server, "manager": manager, "token": token, "project": project}
        finally:
            manager.terminate_all()


def _call(env, method: str, params: dict):
    payload = {"jsonrpc": "2.0", "id": method, "method": method, "params": params, "session_token": env["token"]}
    return env["server"].handle_raw_payload(json.dumps(payload))


def _open_tab(env, tab_id: str = "tab-cmd") -> None:
    resp = _call(env, "terminal.create_tab", {"tab_id": tab_id, "project_path": str(env["project"])})
    assert resp.error is None, resp.error


def _argv_probe() -> list[str]:
    return [sys.executable, "-u", "-c", "import sys; print('ARGV_OK', len(sys.argv))"]


@pytest.mark.parametrize("backend", ["pipe", "conpty"])
def test_argv_list_reaches_the_process_unchanged(ipc_env, monkeypatch, backend):
    if backend == "conpty" and sys.platform != "win32":
        pytest.skip("ConPTY is Windows-specific")
    if backend == "pipe":
        monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    else:
        monkeypatch.delenv("COORDINATOR_PTY_BACKEND", raising=False)
    _open_tab(ipc_env)

    resp = _call(
        ipc_env,
        "terminal.create_session",
        {"tab_id": "tab-cmd", "pane_id": 0, "kind": "human_terminal", "command": _argv_probe(), "cwd": str(ipc_env["project"])},
    )
    assert resp.error is None, resp.error
    session = ipc_env["manager"].get_session(resp.result["session_id"])

    def visible_output() -> str:
        # ConPTY interleaves VT sequences (window title OSC, cursor CSI) with the program output.
        raw = "".join(session.get_output())
        return re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]", "", raw)

    assert _wait_until(lambda: "ARGV_OK 1" in visible_output())
    # Program plus "-c" script: sys.argv == ['-c'] inside the child, i.e. exactly one argument, not one per character.
    assert "ARGV_OK 1" in visible_output()


@pytest.mark.parametrize(
    "command, reason",
    [
        ("python -c \"print('x')\"", "type"),
        ([], "type"),
        ({"argv": ["python"]}, "type"),
        (["python", 5], "item"),
        (["python", ""], "item"),
        (["python", "a\x00b"], "item"),
    ],
)
def test_invalid_command_is_rejected_with_structured_error_and_no_session(ipc_env, monkeypatch, command, reason):
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    _open_tab(ipc_env)
    before = len(ipc_env["manager"].sessions)

    resp = _call(
        ipc_env,
        "terminal.create_session",
        {"tab_id": "tab-cmd", "pane_id": 0, "kind": "human_terminal", "command": command},
    )

    assert resp.result is None
    assert resp.error["code"] == RPCErrorCode.INVALID_PARAMS.value
    assert resp.error["data"] == {"field": "command", "reason": reason}
    assert len(ipc_env["manager"].sessions) == before


def test_missing_command_keeps_default_shell_contract():
    assert validate_command_argv(None) is None
    assert validate_command_argv(["pwsh", "-NoLogo"]) == ["pwsh", "-NoLogo"]


def test_host_types_command_as_argv_list():
    main_rs = (_repo_root / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    start = main_rs.index("fn terminal_create_session(")
    signature = main_rs[start : main_rs.index(") -> ", start)]
    assert "command: Option<Vec<String>>" in signature
    assert "command: Option<String>" not in signature
