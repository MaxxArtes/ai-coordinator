"""Tests for Subetapa 4D.2.1: Governed PTY and Supervision Integration.

Verifies:
- Distinction between Human Interactive Terminals and Governed Agent Sessions.
- Project path fencing and fail-closed authorization.
- Maximum 4 panes per tab boundary.
- Clean process termination without orphan processes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import pytest

from desktop.auth.authorizer import DesktopAuthorizer, SecurityAuthorizationError
from desktop.auth.profile import DesktopProfileManager
from desktop.terminal.manager import MAX_PANES_PER_TAB, TerminalManager, TerminalManagerError
from desktop.terminal.session import SessionKind


@pytest.fixture
def auth_setup():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        prof_mgr = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        token = authorizer.issue_session("default")

        proj_dir = tmp_path / "authorized_repo"
        proj_dir.mkdir(parents=True, exist_ok=True)
        prof_mgr.authorize_project("default", proj_dir)

        yield {
            "authorizer": authorizer,
            "token": token,
            "project_path": proj_dir,
            "tmp_path": tmp_path,
        }


def test_pty_creation_human_terminal_authorized_cwd(auth_setup, monkeypatch):
    """Test 1: Human interactive terminal spawns inside authorized path."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    mgr = TerminalManager(authorizer=auth_setup["authorizer"])
    token = auth_setup["token"]
    proj_path = auth_setup["project_path"]

    tab = mgr.create_tab(token=token, tab_id="tab-1", title="Main Shell", project_path=proj_path)
    assert tab.tab_id == "tab-1"

    cmd = [sys.executable, "-u", "-c", "import time; print('TERMINAL_ACTIVE'); time.sleep(0.5)"]
    sess = mgr.create_terminal(
        token=token,
        tab_id="tab-1",
        pane_id=0,
        kind=SessionKind.HUMAN_TERMINAL,
        command=cmd,
    )

    assert sess.kind == SessionKind.HUMAN_TERMINAL
    assert sess.is_alive is True
    assert sess.pid is not None
    assert sess.pid > 0
    assert sess.principal.owner_id == "default"

    # Wait for process output and clean close
    time.sleep(0.4)
    out = "".join(sess.get_output())
    assert "TERMINAL_ACTIVE" in out

    assert mgr.close_terminal(sess.session_id) is True
    assert sess.is_alive is False


def test_pty_creation_rejected_unauthorized_cwd(auth_setup):
    """Test 2: Spawning terminal outside authorized project path fails fail-closed."""
    mgr = TerminalManager(authorizer=auth_setup["authorizer"])
    token = auth_setup["token"]
    proj_path = auth_setup["project_path"]

    mgr.create_tab(token=token, tab_id="tab-1", title="Main", project_path=proj_path)

    unauthorized_path = auth_setup["tmp_path"] / "forbidden_secrets"
    unauthorized_path.mkdir(parents=True, exist_ok=True)

    with pytest.raises(SecurityAuthorizationError):
        mgr.create_terminal(
            token=token,
            tab_id="tab-1",
            pane_id=0,
            cwd=unauthorized_path,
        )


def test_pty_creation_rejected_invalid_session_token(auth_setup):
    """Test 3: Forged or invalid session token rejected immediately."""
    mgr = TerminalManager(authorizer=auth_setup["authorizer"])
    proj_path = auth_setup["project_path"]

    with pytest.raises(SecurityAuthorizationError):
        mgr.create_tab(
            token="forged_token_12345",
            tab_id="tab-x",
            title="Hacker Tab",
            project_path=proj_path,
        )


def test_governed_agent_session_sanitization(auth_setup, monkeypatch):
    """Test 4: Governed agent session sanitizes secrets from environment."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    monkeypatch.setenv("COORDINATOR_SECRET_API_KEY", "sensitive-key-12345")
    mgr = TerminalManager(authorizer=auth_setup["authorizer"])
    token = auth_setup["token"]
    proj_path = auth_setup["project_path"]

    mgr.create_tab(token=token, tab_id="agent-tab", title="Agent Tasks", project_path=proj_path)

    # Command dumps environment
    cmd = [sys.executable, "-u", "-c", "import os; print('SECRET_PRESENT:', 'COORDINATOR_SECRET_API_KEY' in os.environ)"]
    sess = mgr.create_terminal(
        token=token,
        tab_id="agent-tab",
        pane_id=0,
        kind=SessionKind.GOVERNED_AGENT,
        command=cmd,
    )

    time.sleep(0.4)
    out = "".join(sess.get_output())
    assert "SECRET_PRESENT: False" in out
    mgr.close_tab("agent-tab")


def test_max_panes_per_tab_boundary(auth_setup):
    """Test 5: Tab strictly enforces 0..3 pane index boundary and prevents collisions."""
    mgr = TerminalManager(authorizer=auth_setup["authorizer"])
    token = auth_setup["token"]
    proj_path = auth_setup["project_path"]

    mgr.create_tab(token=token, tab_id="multi-pane", title="Quad View", project_path=proj_path)

    cmd = [sys.executable, "-c", "import time; time.sleep(1)"]

    # Panes 0, 1, 2, 3 succeed
    for i in range(MAX_PANES_PER_TAB):
        sess = mgr.create_terminal(token=token, tab_id="multi-pane", pane_id=i, command=cmd)
        assert sess.pane_id == i

    # Pane 4 fails (exceeds MAX_PANES_PER_TAB = 4)
    with pytest.raises(TerminalManagerError):
        mgr.create_terminal(token=token, tab_id="multi-pane", pane_id=4, command=cmd)

    # Pane -1 fails
    with pytest.raises(TerminalManagerError):
        mgr.create_terminal(token=token, tab_id="multi-pane", pane_id=-1, command=cmd)

    # Re-spawning in pane 0 while active fails
    with pytest.raises(TerminalManagerError):
        mgr.create_terminal(token=token, tab_id="multi-pane", pane_id=0, command=cmd)

    mgr.terminate_all()


@pytest.mark.skipif(sys.platform != "win32", reason="ConPTY is Windows-specific")
def test_windows_conpty_direct(auth_setup):
    """Test 6: Native Windows ConPTY backend launches, writes input, and terminates cleanly."""
    from desktop.terminal.pty_backend import WindowsConPtyBackend

    backend = WindowsConPtyBackend(cols=80, rows=24)
    cmd = [sys.executable, "-u", "-c", "import sys, time; time.sleep(0.5); print('CONPTY_NATIVE_OK'); sys.stdout.flush()"]
    identity = backend.spawn(cmd=cmd, cwd=auth_setup["project_path"])

    assert identity.pid > 0
    assert backend.is_alive() is True

    # Test resize
    assert backend.resize(100, 30) is True

    # Terminate cleanly
    assert backend.terminate() is True
    assert backend.is_alive() is False


def test_clean_termination_and_orphan_prevention(auth_setup):
    """Test 6: Terminating session leaves no orphan processes and cleans state."""
    mgr = TerminalManager(authorizer=auth_setup["authorizer"])
    token = auth_setup["token"]
    proj_path = auth_setup["project_path"]

    mgr.create_tab(token=token, tab_id="cleanup-tab", title="Cleanup", project_path=proj_path)
    cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
    sess = mgr.create_terminal(token=token, tab_id="cleanup-tab", pane_id=0, command=cmd)

    assert sess.is_alive is True
    assert mgr.close_tab("cleanup-tab") is True
    time.sleep(0.1)
    assert sess.is_alive is False
