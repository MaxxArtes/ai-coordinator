"""Integration verification for AI Coordinator Desktop Alpha (Marco 4D.2).

Validates:
1. Complete frontend build bundle assets (dist/index.html, JS, CSS).
2. Tauri 2 native host implementation (src-tauri/src/main.rs) and registered commands.
3. Native icon assets present for Windows packager.
4. GitHub Actions CI workflow for non-local Rust build.
5. Live end-to-end JSON-RPC terminal lifecycle emulation matching Tauri + React execution.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

# Ensure repo root is on sys.path
_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from desktop.ipc.server import SecureIPCServer, BaseTransport
from desktop.terminal.manager import TerminalManager
from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager


class InMemoryTransport(BaseTransport):
    def __init__(self) -> None:
        self.inbox: list[str] = []
        self.outbox: list[str] = []
        self.diags: list[str] = []

    def read_message(self) -> str | None:
        return self.inbox.pop(0) if self.inbox else None

    def write_message(self, message: str) -> bool:
        self.outbox.append(message)
        return True

    def log_diagnostic(self, text: str) -> None:
        self.diags.append(text)


class TestDesktopAlphaIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.desktop_dir = _repo_root / "desktop"
        self.dist_dir = self.desktop_dir / "dist"
        self.tauri_dir = self.desktop_dir / "src-tauri"
        self.workflow_file = _repo_root / ".github" / "workflows" / "desktop-alpha-build.yml"

    def test_01_frontend_bundle_artifacts_present(self) -> None:
        """Verifies dist/index.html and built assets exist after npm run build."""
        index_html = self.dist_dir / "index.html"
        self.assertTrue(index_html.exists(), "dist/index.html must exist")
        content = index_html.read_text(encoding="utf-8")
        self.assertIn("AI Coordinator Desktop", content)

        assets = list(self.dist_dir.glob("assets/*.js"))
        self.assertGreaterEqual(len(assets), 1, "At least one bundled JS chunk must exist")

    def test_02_tauri_host_command_handlers_registered(self) -> None:
        """Verifies main.rs defines and registers all required IPC command handlers."""
        main_rs = self.tauri_dir / "src" / "main.rs"
        self.assertTrue(main_rs.exists(), "src-tauri/src/main.rs must exist")
        code = main_rs.read_text(encoding="utf-8")

        required_handlers = [
            "ping",
            "get_runtime_status",
            "get_process_tree_metrics",
            "terminal_create_tab",
            "terminal_create_session",
            "terminal_write",
            "terminal_resize",
            "terminal_get_output",
            "terminal_close_session",
            "terminal_save_sessions",
            "terminal_list_tabs",
        ]
        for handler in required_handlers:
            self.assertIn(handler, code, f"Handler '{handler}' must be present in main.rs")

    def test_03_native_icons_present(self) -> None:
        """Verifies icon files exist for Windows bundling."""
        icons_dir = self.tauri_dir / "icons"
        self.assertTrue((icons_dir / "icon.ico").exists(), "icon.ico must exist")
        self.assertTrue((icons_dir / "32x32.png").exists(), "32x32.png must exist")
        self.assertTrue((icons_dir / "128x128.png").exists(), "128x128.png must exist")

    def test_04_ci_workflow_validity(self) -> None:
        """Verifies desktop-alpha-build.yml is present and contains required build steps."""
        self.assertTrue(self.workflow_file.exists(), "desktop-alpha-build.yml must exist")
        text = self.workflow_file.read_text(encoding="utf-8")
        self.assertIn("actions/setup-node", text)
        self.assertIn("rust-toolchain", text)
        self.assertIn("npm run build", text)
        self.assertIn("@tauri-apps/cli build", text)
        self.assertIn("upload-artifact", text)
        self.assertIn("Python Build Standalone", text)
        self.assertIn("python_spec.json", text)

    def test_05_e2e_live_terminal_lifecycle_emulation(self) -> None:
        """Emulates the exact React UI -> Tauri Host -> Core IPC sequence for Alpha terminal."""
        os.environ["COORDINATOR_PTY_BACKEND"] = "pipe"
        token = "test-alpha-token-12345"
        transport = InMemoryTransport()

        prof_mgr = DesktopProfileManager()
        prof_mgr.authorize_project(prof_mgr.get_active_profile().name, str(_repo_root))
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        authorizer._active_sessions[token] = prof_mgr.get_active_profile().name
        term_mgr = TerminalManager(authorizer=authorizer)

        server = SecureIPCServer(transport=transport, session_token=token, terminal_manager=term_mgr)

        # 1. Create Tab
        transport.inbox.append(json.dumps({
            "jsonrpc": "2.0",
            "id": "1",
            "method": "terminal.create_tab",
            "params": {"tab_id": "tab-main", "project_path": str(_repo_root)},
            "session_token": token
        }))
        self.assertTrue(server.process_one())
        res1 = json.loads(transport.outbox.pop(0))
        self.assertEqual(res1["result"]["tab_id"], "tab-main")

        # 2. Create Session (Human Interactive Terminal)
        cmd = [sys.executable, "-u", "-c", "import sys; print('HELLO_AI_COORDINATOR_ALPHA'); sys.stdout.flush(); sys.stdin.readline()"]
        transport.inbox.append(json.dumps({
            "jsonrpc": "2.0",
            "id": "2",
            "method": "terminal.create_session",
            "params": {
                "tab_id": "tab-main",
                "pane_id": 0,
                "kind": "human_terminal",
                "command": cmd,
                "cwd": str(_repo_root),
                "cols": 80,
                "rows": 24
            },
            "session_token": token
        }))
        self.assertTrue(server.process_one())
        res2 = json.loads(transport.outbox.pop(0))
        session_id = res2["result"]["session_id"]
        self.assertEqual(len(session_id), 36)

        # Allow child process to flush output
        time.sleep(0.4)

        # 3. Get Output
        transport.inbox.append(json.dumps({
            "jsonrpc": "2.0",
            "id": "3",
            "method": "terminal.get_output",
            "params": {"session_id": session_id},
            "session_token": token
        }))
        self.assertTrue(server.process_one())
        res3 = json.loads(transport.outbox.pop(0))
        output_lines = res3["result"]["lines"]
        combined = "".join(output_lines)
        self.assertIn("HELLO_AI_COORDINATOR_ALPHA", combined)

        # 4. Write input to unblock stdin
        transport.inbox.append(json.dumps({
            "jsonrpc": "2.0",
            "id": "4",
            "method": "terminal.write",
            "params": {"session_id": session_id, "data": "exit\n"},
            "session_token": token
        }))
        self.assertTrue(server.process_one())
        transport.outbox.pop(0)

        # 5. Close Session
        transport.inbox.append(json.dumps({
            "jsonrpc": "2.0",
            "id": "5",
            "method": "terminal.close_session",
            "params": {"session_id": session_id},
            "session_token": token
        }))
        self.assertTrue(server.process_one())
        res5 = json.loads(transport.outbox.pop(0))
        self.assertTrue(res5["result"]["closed"])

        # Teardown
        term_mgr.terminate_all()

    def test_06_ring_buffer_monotonic_polling_never_freezes_at_limit(self) -> None:
        """Verifies buffer polling with since_seq continues streaming after 1000-line limit (Problem 7)."""
        from desktop.terminal.buffer import BoundedTerminalBuffer

        buf = BoundedTerminalBuffer(max_lines=10, max_bytes=1024 * 1024)
        # 1. Fill buffer with 10 lines
        for i in range(10):
            buf.append(f"initial-line-{i}")

        lines, seq1 = buf.get_new_lines(0)
        self.assertEqual(len(lines), 10)
        self.assertEqual(seq1, 10)

        # 2. Add 5 more lines (evicting oldest 5)
        for i in range(10, 15):
            buf.append(f"overflow-line-{i}")

        # Total buffer still capped at 10
        self.assertEqual(buf.line_count, 10)

        # Polling with since_seq=seq1 (10) returns the 5 new lines, NOT empty!
        new_lines, seq2 = buf.get_new_lines(seq1)
        self.assertEqual(len(new_lines), 5)
        self.assertEqual(new_lines[0], "overflow-line-10")
        self.assertEqual(new_lines[-1], "overflow-line-14")
        self.assertEqual(seq2, 15)

    def test_07_tab_prerequisite_enforced_fail_closed(self) -> None:
        """Verifies session creation strictly fails if tab was not created first (Problem 2)."""
        token = "test-alpha-token-tab"
        transport = InMemoryTransport()
        prof_mgr = DesktopProfileManager()
        prof_mgr.authorize_project(prof_mgr.get_active_profile().name, str(_repo_root))
        authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
        authorizer._active_sessions[token] = prof_mgr.get_active_profile().name
        term_mgr = TerminalManager(authorizer=authorizer)
        server = SecureIPCServer(transport=transport, session_token=token, terminal_manager=term_mgr)

        # Attempt to create session without creating tab first
        transport.inbox.append(json.dumps({
            "jsonrpc": "2.0",
            "id": "1",
            "method": "terminal.create_session",
            "params": {
                "tab_id": "non-existent-tab",
                "pane_id": 0,
                "kind": "human_terminal",
                "cwd": str(_repo_root)
            },
            "session_token": token
        }))
        self.assertTrue(server.process_one())
        res = json.loads(transport.outbox.pop(0))
        self.assertIn("error", res)
        self.assertIn("does not exist", res["error"]["message"])


if __name__ == "__main__":
    unittest.main()
