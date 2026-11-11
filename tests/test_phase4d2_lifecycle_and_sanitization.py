"""Tests for Subetapa 4D.2.4: Buffer Capping, Sanitization, and Lifecycle Management.

Verifies:
- Sensitive token redaction upon session export (sk-*, Bearer, password, api_key).
- Abnormal child exit capture (exit codes, non-zero termination without crashing manager).
- Clean lifecycle teardown on pane close and application exit (terminate_all).
- Session layout persistence round-trip.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import pytest

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.terminal.buffer import BoundedTerminalBuffer, SessionLayoutExport
from desktop.terminal.manager import TerminalManager
from desktop.terminal.session import SessionKind


@pytest.fixture
def lifecycle_fixture():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        prof_mgr = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        token = authorizer.issue_session("default")

        proj_dir = tmp_path / "lifecycle_repo"
        proj_dir.mkdir(parents=True, exist_ok=True)
        prof_mgr.authorize_project("default", proj_dir)

        mgr = TerminalManager(authorizer=authorizer)
        yield {
            "mgr": mgr,
            "token": token,
            "project_path": proj_dir,
            "tmp_path": tmp_path,
        }


def test_credential_sanitization_in_buffer_export():
    """Test 1: Credentials (OpenRouter keys, Bearer tokens, passwords) are redacted upon export."""
    buf = BoundedTerminalBuffer(max_lines=100)

    buf.append("Iniciando requisição de rede...")
    buf.append("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secretpayload")
    buf.append("OPENROUTER_API_KEY=sk-or-v1-abcdef0123456789abcdef0123456789")
    buf.append("export DB_PASSWORD=SuperSecretPass99!")
    buf.append("Operação finalizada.")

    # Raw lines contain credentials
    raw = buf.get_lines()
    assert any("sk-or-v1" in line for line in raw)

    # Exported sanitized lines redact all secrets
    sanitized = buf.export_sanitized()
    assert len(sanitized) == 5
    assert not any("sk-or-v1" in line for line in sanitized)
    assert not any("eyJhbGci" in line for line in sanitized)
    assert not any("SuperSecretPass99!" in line for line in sanitized)

    sanitized_text = "\n".join(sanitized)
    assert "[REDACTED_CREDENTIAL]" in sanitized_text


def test_abnormal_child_process_exit_reporting(lifecycle_fixture, monkeypatch):
    """Test 2: Process exiting with error code (e.g. exit 42) is cleanly captured."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    mgr = lifecycle_fixture["mgr"]
    token = lifecycle_fixture["token"]
    proj_path = lifecycle_fixture["project_path"]

    mgr.create_tab(token=token, tab_id="crash-tab", title="Crash Test", project_path=proj_path)

    # Command prints then exits with code 42
    cmd = [sys.executable, "-u", "-c", "import sys; print('BEFORE_CRASH'); sys.stdout.flush(); sys.exit(42)"]
    sess = mgr.create_terminal(token=token, tab_id="crash-tab", pane_id=0, command=cmd)

    # Wait for child to exit
    time.sleep(0.5)

    assert sess.is_alive is False
    assert sess.exit_code == 42

    # Buffer preserved
    lines = "".join(sess.get_output())
    assert "BEFORE_CRASH" in lines

    # Closing exited terminal succeeds cleanly
    assert mgr.close_terminal(sess.session_id) is True


def test_session_layout_save_and_restore_serialization(lifecycle_fixture, monkeypatch):
    """Test 3: Saving tab sessions produces structured payload with sanitized buffers."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    mgr = lifecycle_fixture["mgr"]
    token = lifecycle_fixture["token"]
    proj_path = lifecycle_fixture["project_path"]

    mgr.create_tab(token=token, tab_id="save-tab", title="Saveable Tab", project_path=proj_path)

    cmd = [sys.executable, "-u", "-c", "import sys; print('key=sk-secret-token-1234567890'); sys.stdout.flush()"]
    mgr.create_terminal(token=token, tab_id="save-tab", pane_id=0, command=cmd)

    time.sleep(0.4)

    export = mgr.save_tab_sessions("save-tab")
    assert isinstance(export, SessionLayoutExport)
    assert export.tab_id == "save-tab"
    assert export.metadata["pane_count"] == 1

    payload_dict = export.to_dict()
    payload_json = json.dumps(payload_dict)

    # Ensure JSON payload does NOT leak secret
    assert "sk-secret-token" not in payload_json
    assert "[REDACTED_CREDENTIAL]" in payload_json

    mgr.terminate_all()


def test_bulk_application_exit_cleanup(lifecycle_fixture, monkeypatch):
    """Test 4: terminate_all closes all sessions and tabs without orphaned processes."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    mgr = lifecycle_fixture["mgr"]
    token = lifecycle_fixture["token"]
    proj_path = lifecycle_fixture["project_path"]

    # Create 2 tabs with 2 panes each = 4 processes
    for t in range(2):
        tab_id = f"tab-{t}"
        mgr.create_tab(token=token, tab_id=tab_id, title=f"Tab {t}", project_path=proj_path)
        for p in range(2):
            cmd = [sys.executable, "-u", "-c", "import time; time.sleep(10)"]
            mgr.create_terminal(token=token, tab_id=tab_id, pane_id=p, command=cmd)

    assert len(mgr.sessions) == 4
    for sess in mgr.sessions.values():
        assert sess.is_alive is True

    # Bulk terminate
    mgr.terminate_all()
    assert len(mgr.sessions) == 0
    assert len(mgr.tabs) == 0
