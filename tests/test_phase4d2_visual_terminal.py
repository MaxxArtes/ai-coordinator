"""Tests for Subetapas 4D.2.2 & 4D.2.3: Visual Terminal, Streaming, and Multi-Pane Grid.

Verifies:
- ANSI escape codes and UTF-8 rendering in bounded buffer.
- Dynamic resize event propagation.
- Full IPC dispatch for all terminal methods (`create_tab`, `create_session`, `write`, `resize`, `get_output`, `save_sessions`, `close_session`).
- Ring buffer eviction under high-volume flood conditions (backpressure containment).
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

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.server import BaseTransport, SecureIPCServer
from desktop.terminal.buffer import BoundedTerminalBuffer
from desktop.terminal.manager import TerminalManager


def _wait_until(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    """Polls predicate until true or timeout (avoids fixed sleeps on slow CI runners)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class MemoryTransport(BaseTransport):
    """In-memory transport for deterministic IPC handler verification."""

    def __init__(self) -> None:
        self.incoming = []
        self.outgoing = []
        self.diagnostics = []

    def read_message(self):
        if self.incoming:
            return self.incoming.pop(0)
        return None

    def write_message(self, message: str) -> bool:
        self.outgoing.append(message)
        return True

    def log_diagnostic(self, message: str) -> None:
        self.diagnostics.append(message)


@pytest.fixture
def terminal_ipc_fixture():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        prof_mgr = DesktopProfileManager(root_dir=tmp_path / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        token = authorizer.issue_session("default")

        proj_dir = tmp_path / "terminal_repo"
        proj_dir.mkdir(parents=True, exist_ok=True)
        prof_mgr.authorize_project("default", proj_dir)

        term_mgr = TerminalManager(authorizer=authorizer)
        transport = MemoryTransport()
        server = SecureIPCServer(transport=transport, session_token=token, terminal_manager=term_mgr)

        yield {
            "server": server,
            "transport": transport,
            "token": token,
            "project_path": str(proj_dir),
            "term_mgr": term_mgr,
        }


def test_ansi_and_utf8_rendering():
    """Test 1: Terminal buffer accurately handles ANSI color codes and UTF-8 multi-byte glyphs."""
    buf = BoundedTerminalBuffer(max_lines=100, max_bytes=64 * 1024)

    # ANSI SGR colored text + UTF-8 accents and symbols
    ansi_sample = "\x1b[31;1mErro Crítico:\x1b[0m Falha na execução: αβγ 🚀"
    buf.append(ansi_sample)

    assert buf.line_count == 1
    lines = buf.get_lines()
    assert "Erro Crítico:" in lines[0]
    assert "🚀" in lines[0]


def test_buffer_flood_and_backpressure_eviction():
    """Test 2: Flooding 2,500 lines evicts oldest lines and respects max_lines (1,000) and max_bytes."""
    max_lines = 1000
    max_bytes = 100 * 1024  # 100 KB
    buf = BoundedTerminalBuffer(max_lines=max_lines, max_bytes=max_bytes)

    # Flood with 2500 numbered lines
    for i in range(2500):
        buf.append(f"LOG_LINE_{i:05d}: Operation payload data testing backpressure")

    # Bounded invariants
    assert buf.line_count <= max_lines
    assert buf.byte_count <= max_bytes

    # Oldest lines evicted, newest lines preserved
    lines = buf.get_lines()
    assert "LOG_LINE_00000" not in lines[0]
    assert "LOG_LINE_02499" in lines[-1]


def test_ipc_terminal_lifecycle_roundtrip(terminal_ipc_fixture, monkeypatch):
    """Test 3: Full IPC round-trip for tab creation, session spawning, input, output, resize, and save."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    server = terminal_ipc_fixture["server"]
    token = terminal_ipc_fixture["token"]
    proj_path = terminal_ipc_fixture["project_path"]

    # 1. terminal.create_tab
    resp = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":1,"method":"terminal.create_tab","session_token":"{token}","params":{{"tab_id":"tab-alpha","title":"Alpha Work","project_path":"{proj_path.replace(chr(92), "/")}"}}}}'
    )
    assert resp.error is None
    assert resp.result["tab_id"] == "tab-alpha"

    # 2. terminal.create_session
    cmd = [sys.executable, "-u", "-c", "import sys, time; print('READY_FOR_INPUT'); sys.stdout.flush(); sys.stdin.readline()"]
    import json
    cmd_json = json.dumps(cmd)

    resp = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":2,"method":"terminal.create_session","session_token":"{token}","params":{{"tab_id":"tab-alpha","pane_id":0,"command":{cmd_json},"cols":80,"rows":24}}}}'
    )
    assert resp.error is None
    session_id = resp.result["session_id"]
    assert resp.result["pane_id"] == 0

    # 3. terminal.resize
    resp = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":3,"method":"terminal.resize","session_token":"{token}","params":{{"session_id":"{session_id}","cols":120,"rows":40}}}}'
    )
    assert resp.error is None
    assert resp.result["resized"] is True

    # 4. Wait for initial output and read via terminal.get_output
    def _read_output():
        return server.handle_raw_payload(
            f'{{"jsonrpc":"2.0","id":4,"method":"terminal.get_output","session_token":"{token}","params":{{"session_id":"{session_id}"}}}}'
        )

    _wait_until(lambda: "READY_FOR_INPUT" in "".join((_read_output().result or {}).get("lines", [])))
    resp = _read_output()
    assert resp.error is None
    lines = "".join(resp.result["lines"])
    assert "READY_FOR_INPUT" in lines

    # 5. terminal.write (send input line)
    resp = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":5,"method":"terminal.write","session_token":"{token}","params":{{"session_id":"{session_id}","data":"hello\\n"}}}}'
    )
    assert resp.error is None
    assert resp.result["bytes_written"] > 0

    # 6. terminal.save_sessions
    resp = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":6,"method":"terminal.save_sessions","session_token":"{token}","params":{{"tab_id":"tab-alpha"}}}}'
    )
    assert resp.error is None
    assert resp.result["tab_id"] == "tab-alpha"
    assert "pane_0" in resp.result["terminals"]

    # 7. terminal.close_session
    resp = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":7,"method":"terminal.close_session","session_token":"{token}","params":{{"session_id":"{session_id}"}}}}'
    )
    assert resp.error is None
    assert resp.result["closed"] is True


def test_four_panes_simultaneous_streaming(terminal_ipc_fixture, monkeypatch):
    """Test 4: Spawns up to 4 real concurrent terminal sessions in a single tab without collision."""
    monkeypatch.setenv("COORDINATOR_PTY_BACKEND", "pipe")
    server = terminal_ipc_fixture["server"]
    token = terminal_ipc_fixture["token"]
    proj_path = terminal_ipc_fixture["project_path"]
    term_mgr = terminal_ipc_fixture["term_mgr"]

    # Create tab
    tab = term_mgr.create_tab(token=token, tab_id="quad-tab", title="Quad Terminal", project_path=proj_path)

    sessions = []
    for i in range(4):
        cmd = [sys.executable, "-u", "-c", f"import time; print('PANE_{i}_ONLINE'); time.sleep(0.5)"]
        sess = term_mgr.create_terminal(
            token=token,
            tab_id="quad-tab",
            pane_id=i,
            command=cmd,
        )
        sessions.append(sess)

    assert len(tab.panes) == 4

    _wait_until(
        lambda: all(f"PANE_{i}_ONLINE" in "".join(s.get_output()) for i, s in enumerate(sessions))
    )
    for i, sess in enumerate(sessions):
        out = "".join(sess.get_output())
        assert f"PANE_{i}_ONLINE" in out

    # Terminate all
    term_mgr.terminate_all()
    assert len(term_mgr.sessions) == 0
