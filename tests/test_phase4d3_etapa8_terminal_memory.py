"""Etapa 8 (native check, 18/11): the human terminal must run ordinary tools that need more than 256 MB.

Found in the diagnostic build 44328616887: Claude Code ("MemoryExhaustion") and OpenCode ("memory allocation of
128256 bytes failed") died at start in the human terminal, because every terminal ran in a Job Object with a 256 MB
limit for its whole process tree (since 4D.2.1). The human terminal runs with the operator's own rights, so the cap
was no protection; its tree is still contained and killed on close. Governed agents keep the cap.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import pytest

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.terminal.manager import TerminalManager
from desktop.terminal.session import SessionKind

# Commits 384 MB (above the old 256 MB job limit) and touches every page, then prints a marker.
ALLOCATE = (
    "import sys; b = bytearray(384 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); "
    "print('ALLOC_' + '384_OK'); sys.stdout.flush()"  # built at run time: a traceback echoes the source line
)


def _wait_for(session, marker: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    seen = ""
    while time.monotonic() < deadline:
        raw, _lines, _seq = session.get_new_output(0)
        seen = raw
        if marker in raw or "MemoryError" in raw:
            return raw
        if not session.is_alive:
            time.sleep(0.3)
            return session.get_new_output(0)[0]
        time.sleep(0.1)
    return seen


@pytest.fixture
def manager_and_project():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        profiles = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=profiles)
        token = authorizer.issue_session("default")
        project = tmp_path / "repo"
        project.mkdir()
        profiles.authorize_project("default", project)
        manager = TerminalManager(authorizer=authorizer)
        manager.create_tab(token=token, tab_id="mem", title="Memória", project_path=project)
        yield manager, token
        for session in list(manager.sessions.values()):
            try:
                session.terminate()
            except Exception:
                pass


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object memory limit is the Windows containment")
def test_human_terminal_runs_a_tool_that_needs_more_than_256_mb(manager_and_project):
    manager, token = manager_and_project
    session = manager.create_terminal(token=token, tab_id="mem", pane_id=0, kind=SessionKind.HUMAN_TERMINAL,
                                      command=[sys.executable, "-u", "-c", ALLOCATE])
    output = _wait_for(session, "ALLOC_384_OK")
    assert "ALLOC_384_OK" in output, output[-500:]
    assert "MemoryError" not in output


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object memory limit is the Windows containment")
def test_governed_agent_keeps_the_memory_cap(manager_and_project):
    manager, token = manager_and_project
    session = manager.create_terminal(token=token, tab_id="mem", pane_id=1, kind=SessionKind.GOVERNED_AGENT,
                                      command=[sys.executable, "-u", "-c", ALLOCATE])
    output = _wait_for(session, "ALLOC_384_OK")
    assert "ALLOC_384_OK" not in output, "a governed agent must stay under the 256 MB job limit"
    assert "MemoryError" in output
