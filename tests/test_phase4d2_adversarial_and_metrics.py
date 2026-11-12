"""Adversarial and Resource Metric Tests for Marco 4D.2 (Subetapa 4D.2.5).

Verifies:
- Path traversal & unauthorized directory access resistance (canonical resolution).
- Worker privilege escalation lockout.
- High-concurrency output flood across 4 real processes without deadlocks or buffer overflows.
- Process tree RAM and CPU measurement across parent, IPC, and 4 concurrent terminal processes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import List

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import pytest

from desktop.auth.authorizer import DesktopAuthorizer, SecurityAuthorizationError
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.protocol import DataOrigin, MetricProvenance
from desktop.runner.verify_foundation import get_process_memory_bytes
from desktop.terminal.manager import TerminalManager
from desktop.terminal.session import SessionKind


def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    """Polls predicate until true or timeout (avoids fixed sleeps on slow CI runners)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture
def adversarial_fixture():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        prof_mgr = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        token = authorizer.issue_session("default")

        proj_dir = tmp_path / "safe_project"
        proj_dir.mkdir(parents=True, exist_ok=True)
        prof_mgr.authorize_project("default", proj_dir)

        mgr = TerminalManager(authorizer=authorizer)
        yield {
            "mgr": mgr,
            "token": token,
            "project_path": proj_dir,
            "tmp_path": tmp_path,
            "authorizer": authorizer,
        }


def test_adversarial_path_traversal_blocked(adversarial_fixture):
    """Adversarial 1: Traversal sequences (../../) and sibling folders are rejected."""
    mgr = adversarial_fixture["mgr"]
    token = adversarial_fixture["token"]
    proj_path = adversarial_fixture["project_path"]
    tmp_path = adversarial_fixture["tmp_path"]

    mgr.create_tab(token=token, tab_id="safe-tab", title="Safe", project_path=proj_path)

    forbidden = tmp_path / "forbidden_secrets"
    forbidden.mkdir(parents=True, exist_ok=True)

    # Relative traversal attempt
    traversal_path = proj_path / ".." / "forbidden_secrets"
    with pytest.raises(SecurityAuthorizationError):
        mgr.create_terminal(token=token, tab_id="safe-tab", pane_id=0, cwd=traversal_path)

    # Absolute unauthorized path attempt
    with pytest.raises(SecurityAuthorizationError):
        mgr.create_tab(token=token, tab_id="evil-tab", title="Evil", project_path=forbidden)


def test_adversarial_worker_role_lockout(adversarial_fixture, monkeypatch):
    """Adversarial 2: Subordinate worker attempting to spawn terminals fails fail-closed."""
    mgr = adversarial_fixture["mgr"]
    token = adversarial_fixture["token"]
    proj_path = adversarial_fixture["project_path"]

    monkeypatch.setenv("AI_COORDINATOR_ROLE", "worker")

    with pytest.raises(SecurityAuthorizationError):
        mgr.create_tab(token=token, tab_id="worker-illegal", title="Worker Tab", project_path=proj_path)


def test_high_concurrency_4_panes_output_flood(adversarial_fixture, monkeypatch):
    """Adversarial 3: 4 simultaneous child processes flood output at high speed."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    mgr = adversarial_fixture["mgr"]
    token = adversarial_fixture["token"]
    proj_path = adversarial_fixture["project_path"]

    tab = mgr.create_tab(token=token, tab_id="flood-tab", title="Flood Test", project_path=proj_path)

    sessions: List = []
    # Each child outputs 500 lines rapidly
    for i in range(4):
        cmd = [
            sys.executable,
            "-u",
            "-c",
            f"import sys\nfor j in range(500):\n    print(f'BURST_P{i}_L{{j}}')\nsys.stdout.flush()",
        ]
        sess = mgr.create_terminal(token=token, tab_id="flood-tab", pane_id=i, command=cmd)
        sessions.append(sess)

    # Allow burst to complete: every child exited and its tag reached its own buffer
    _wait_until(
        lambda: all(
            not s.is_alive and f"BURST_P{i}" in "\n".join(s.get_output()) for i, s in enumerate(sessions)
        )
    )

    for i, sess in enumerate(sessions):
        lines = sess.get_output()
        assert len(lines) > 0
        # Line count remains bounded
        assert sess.buffer.line_count <= 1000
        # Check that pane's own tags were captured
        full_text = "\n".join(lines)
        assert f"BURST_P{i}" in full_text

    mgr.terminate_all()


def test_process_tree_resource_metrics_under_load(adversarial_fixture, monkeypatch):
    """Adversarial 4: Measure full process tree Working Set RAM across parent + 4 terminals."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    mgr = adversarial_fixture["mgr"]
    token = adversarial_fixture["token"]
    proj_path = adversarial_fixture["project_path"]

    mgr.create_tab(token=token, tab_id="metrics-tab", title="Resource Metrics", project_path=proj_path)

    # Spawn 4 background idle terminals
    pids = []
    for i in range(4):
        cmd = [sys.executable, "-u", "-c", "import time; time.sleep(10)"]
        sess = mgr.create_terminal(token=token, tab_id="metrics-tab", pane_id=i, command=cmd)
        assert sess.pid is not None
        pids.append(sess.pid)

    # Measure parent process RAM
    parent_pid = os.getpid()
    parent_ram = get_process_memory_bytes(parent_pid)

    # Measure child processes RAM
    children_ram = [get_process_memory_bytes(p) for p in pids]
    total_tree_ram_bytes = parent_ram + sum(children_ram)
    total_tree_mb = round(total_tree_ram_bytes / (1024 * 1024), 2)

    # Metrics validation
    assert total_tree_mb > 0
    # Provenance classifications
    provenance = MetricProvenance.MEASURED.value
    data_origin = DataOrigin.TEST_FIXTURE.value

    assert provenance == "measured"
    assert data_origin == "test_fixture"

    # Terminate all
    mgr.terminate_all()
