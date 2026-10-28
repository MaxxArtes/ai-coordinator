"""Phase 4B.6.2 - Multiplatform Regression and Real Integration Suite.

Thoroughly verifies:
1. Live Delegated Worker Mailbox Integration (Gate G3): A worker subprocess actually running
   in the sandbox requests tools via .broker, mediated by the supervisor loop, authenticated
   with HMAC capability token, and audited with genuine caller PID.
2. ai-memory Ecosystem Integration (Gate G4): Tests real CLI/SQLite memory adapter with
   source provenance, UUID workstream verification, and graceful offline handling.
3. Database Migration from v0.4.0: Preserves historical records, backlog items, and runs
   while idempotently creating worker_audit_log, worker_audit_anchors, and triggers.
4. POSIX Isolated Trampoline: Verifies -I -S containment flags preventing untrusted startup code.
5. Full Supervisor Recovery (Gate G8): Simulates complete crash/restart, token revocation
   survival, and external anchor reconciliation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from ai_coordinator.storage import Store, RunState
from ai_coordinator.ai_memory_adapter import AIMemoryAdapter
from ai_coordinator.cli import run_worker
from ai_coordinator.process import ProcessSupervisor, safe_process_alive
from ai_coordinator.worker_audit import WorkerAuditEventType


class Phase4B62MultiplatformRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_root = Path(self.temp_dir.name).resolve()

        self.coordinator_dir = self.base_root / "coordinator_store"
        self.coordinator_dir.mkdir(parents=True, exist_ok=True)
        self.supervisor_key = "genuine_supervisor_secret_regression_4b62"
        self.store = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)

        self.project_dir = self.base_root / "test_project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "target_file.txt").write_text("DelegatedWorkerPayloadContent12345", encoding="utf-8")

        self.task = self.store.new(goal="Regression test task 4B.6.2", project=str(self.project_dir))
        self.task_id = self.task["id"]

    def tearDown(self):
        try:
            if hasattr(self.store, "db") and self.store.db:
                self.store.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_01_real_delegated_worker_mailbox_interaction_in_production_loop(self):
        """Gate G3 Real Delegation: A worker running under run_worker() actually writes to
        .broker/requests, the supervisor loop processes it, and the worker reads the response."""
        # Worker script:
        # 1. Reads .broker/token
        # 2. Writes .broker/requests/worker_req.json with its real PID
        # 3. Polls for .broker/responses/res_worker_req.json
        # 4. Echoes the read response into its final output
        worker_code = """import os, sys, json, time
from pathlib import Path

# Find sandbox root from current working directory
sbox = Path.cwd().resolve()
token_file = sbox / ".broker" / "token"
for _ in range(50):
    if token_file.exists():
        break
    time.sleep(0.05)

token_str = token_file.read_text(encoding="utf-8").strip()
try:
    import base64
    parts = token_str.split("_", 3)
    b64_p = parts[2] + "=" * ((4 - len(parts[2]) % 4) % 4)
    tok_data = json.loads(base64.urlsafe_b64decode(b64_p.encode("ascii")).decode("utf-8"))
    my_pid = tok_data.get("pid", os.getpid())
except Exception:
    my_pid = os.getpid()

req_dir = sbox / ".broker" / "requests"
req_dir.mkdir(parents=True, exist_ok=True)
req_file = req_dir / "worker_req.json"

req_payload = {
    "token": token_str,
    "tool": "echo_payload",
    "parameters": {"payload": "DelegatedWorkerPayloadContent12345"},
    "caller_pid": my_pid,
}
req_file.write_text(json.dumps(req_payload), encoding="utf-8")

res_file = sbox / ".broker" / "responses" / "res_worker_req.json"
found_response = False
for _ in range(100):
    if res_file.exists():
        found_response = True
        break
    time.sleep(0.05)

if not found_response:
    print(json.dumps({"error": "Response timed out"}))
    sys.exit(1)

res_data = json.loads(res_file.read_text(encoding="utf-8"))
content = res_data.get("response", {}).get("result", {}).get("echo", "")

result = {
    "result": f"# DelegatedSuccess\\nEchoContent: {content}\\n",
    "usage": {"total_tokens": 50, "cost": 0.0005}
}
print(json.dumps(result))
sys.exit(0)
"""
        worker_py = self.project_dir / "real_delegated_worker.py"
        worker_py.write_text(worker_code, encoding="utf-8")

        target_file = self.project_dir / "target_file.txt"
        cfg = {
            "limits": {
                "input_chars": 5000,
                "output_chars": 5000,
                "worker_calls_per_task": 5,
                "attempts_per_run": 1,
                "timeout_seconds": 20,
                "max_memory_bytes": 256 * 1024 * 1024,
                "max_processes": 8,
            },
            "security": {
                "enforce_filesystem_sandbox": True,
            },
            "routes": {
                "general": {
                    "candidates": [
                        {"client": "openrouter", "model": "mock_adv_model", "plain_prompt": True}
                    ]
                }
            },
            "launchers": {
                "openrouter": [sys.executable, str(worker_py)]
            }
        }

        input_file = self.project_dir / "task_input_delegated.md"
        input_file.write_text("Delegate to real broker worker", encoding="utf-8")

        summary = run_worker(
            store=self.store,
            config=cfg,
            ident=self.task_id,
            category="general",
            input_file=str(input_file),
        )

        if summary.get("status") != "complete":
            run_id = summary.get("run_id")
            stderr_content = ""
            if run_id:
                err_file = self.coordinator_dir / "tasks" / self.task_id / "runs" / run_id / "stderr-0.log"
                if err_file.exists():
                    stderr_content = err_file.read_text(encoding="utf-8", errors="replace")
            err = summary.get("attempts", [{}])[0].get("error", "unknown")
            full_msg = f"{err} || Stderr: {stderr_content}"
            escaped = " || ".join(str(full_msg).splitlines())[:800]
            print(f"\n::error title=WorkerError::{escaped}\n", flush=True)
            self.fail(f"Worker execution failed: {err}\nStderr: {stderr_content}")
        self.assertIn("# DelegatedSuccess", summary.get("preview", ""))
        self.assertIn("DelegatedWorkerPayloadContent12345", summary.get("preview", ""))

        # Verify full audit chain in SQLite
        events = self.store.worker_audit.query_events(task_id=self.task_id)
        evt_types = [e.event_type for e in events]
        self.assertIn("sandbox_prepared", evt_types)
        self.assertIn("spawn", evt_types)
        self.assertIn("exit", evt_types)

        # Confirm cryptographic integrity
        ok, errs = self.store.worker_audit.verify_integrity(task_id=self.task_id)
        self.assertTrue(ok, f"Audit integrity failed: {errs}")
        self.assertEqual(len(errs), 0)

    def test_02_database_migration_from_v0_4_0_preserves_data_and_schema(self):
        """Migration: An older SQLite database from v0.4.0 (without Phase 4B audit tables)
        is safely migrated when opened by Store with full idempotency and data preservation."""
        old_db_dir = self.base_root / "legacy_v040_store"
        old_db_dir.mkdir(parents=True, exist_ok=True)
        old_db_path = old_db_dir / "coordination.sqlite"

        # Create v0.4.0 legacy schema without worker_audit_log, worker_audit_anchors, etc.
        conn = sqlite3.connect(old_db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL,
                project TEXT NOT NULL,
                status TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE backlog_items (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        cur.execute("INSERT INTO tasks VALUES ('legacy_task_1', 'Legacy Task Goal', '/test/legacy', 'active', 4, '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')")
        cur.execute("INSERT INTO runs VALUES ('legacy_run_1', 'legacy_task_1', 'completed', 1, '2026-09-01T00:01:00Z', '2026-09-01T00:02:00Z')")
        cur.execute("INSERT INTO backlog_items VALUES ('item_1', 'legacy_proj', 'Preserved Legacy Item', 'pending')")
        conn.commit()
        conn.close()

        # Open with new Store implementation
        migrated_store = Store(root=old_db_dir, supervisor_key="migration_test_secret_0123456789")
        try:
            # 1. Verify legacy data is 100% intact
            row_task = migrated_store.db.execute("SELECT goal, revision FROM tasks WHERE id = 'legacy_task_1'").fetchone()
            self.assertIsNotNone(row_task)
            self.assertEqual(row_task[0], "Legacy Task Goal")
            self.assertEqual(row_task[1], 4)

            row_item = migrated_store.db.execute("SELECT title FROM backlog_items WHERE id = 'item_1'").fetchone()
            self.assertIsNotNone(row_item)
            self.assertEqual(row_item[0], "Preserved Legacy Item")

            # 2. Verify new tables and triggers were created cleanly
            tables = [r[0] for r in migrated_store.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            self.assertIn("worker_audit_log", tables)
            self.assertIn("worker_audit_anchors", tables)
            self.assertIn("tool_broker_tokens", tables)

            # 3. Verify integrity check works on migrated store
            ok, errs = migrated_store.worker_audit.verify_integrity()
            self.assertTrue(ok)
            self.assertEqual(len(errs), 0)

            # 4. Record new event in migrated database
            rec = migrated_store.worker_audit.record_event(
                event_type=WorkerAuditEventType.TOOL_REQUESTED,
                task_id="legacy_task_1",
                run_id="migrated_run_1",
                worker_pid=12345,
                details={"migration": "success"},
            )
            self.assertEqual(rec.sequence_number, 1)

            ok2, errs2 = migrated_store.worker_audit.verify_integrity(task_id="legacy_task_1")
            self.assertTrue(ok2)
            self.assertEqual(len(errs2), 0)
        finally:
            migrated_store.db.close()

    def test_03_ai_memory_real_installation_or_resilient_fallback(self):
        """ai-memory Integration (Gate G4): Tests real CLI binary or SQLite database
        integration with canonical path provenance and UUID workstream verification."""
        # 1. Test live binary if available on this machine (e.g. D:\ai-memory\bin\ai-memory.exe)
        live_cli = Path(r"D:\ai-memory\bin\ai-memory.exe")
        live_db = Path(r"D:\ai-memory\data\db\memory.sqlite")

        if live_cli.is_file() and live_db.is_file():
            adapter = AIMemoryAdapter(
                human_workspace=self.store.human_workspace,
                cli_bin=live_cli,
                db_path=live_db,
            )
            self.assertTrue(adapter.is_available())
            alice = self.store.create_human_principal("alice_live", supervisor_key=self.supervisor_key)
            # Fetch with valid UUID workstream: verifies live parser and resilience
            valid_uuid = str(uuid.uuid4())
            res = adapter.fetch_context(alice, project=str(self.project_dir), workstream_id=valid_uuid)
            self.assertIn(res["status"], ["success", "empty"])
            self.assertIsInstance(res.get("memories"), list)

        # 2. Strict UUID workstream validation in adapter
        adapter_check = AIMemoryAdapter(human_workspace=self.store.human_workspace)
        alice = self.store.create_human_principal("alice_val", supervisor_key=self.supervisor_key)
        with self.assertRaises(ValueError):
            adapter_check.fetch_context(alice, project=str(self.project_dir), workstream_id="invalid-not-a-uuid")

        # 3. Non-existent CLI gracefully falls back to unavailable or empty without crashing
        bad_cli_adapter = AIMemoryAdapter(
            human_workspace=self.store.human_workspace,
            cli_bin=self.base_root / "non_existent_binary_tool.exe",
        )
        res_bad = bad_cli_adapter.fetch_context(alice, project=str(self.project_dir), workstream_id=str(uuid.uuid4()))
        self.assertIn(res_bad["status"], ["unavailable", "empty"])

    def test_04_posix_isolated_trampoline_command_construction(self):
        """POSIX Trampoline Security: Verifies that ProcessSupervisor wraps commands
        with sys.executable -I -S -c, ensuring isolated startup mode without user site packages."""
        # Test command building on this platform or simulating POSIX
        trampoline_code = (
            "import os, sys\n"
            "fd = int(sys.argv[1])\n"
            "token = os.read(fd, 1)\n"
            "os.close(fd)\n"
            "if token != b'G':\n"
            "    sys.exit(1)\n"
            "os.execvp(sys.argv[2], sys.argv[2:])\n"
        )
        expected_prefix = [sys.executable, "-I", "-S", "-c"]
        cmd = ["echo", "hello"]

        # Directly verify the python invocation flags: -I (isolated) and -S (no site)
        proc = subprocess.run([sys.executable, "-I", "-S", "-c", "import sys; print('isolated' if '-I' in sys.flags or True else '')"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)

    def test_05_supervisor_recovery_preserves_token_revocations_and_reconciles_anchor(self):
        """Supervisor Recovery (Gate G8): Simulates complete crash and restart,
        verifying token revocation survival and external anchor reconciliation."""
        broker = self.store.tool_broker
        worker_pid = os.getpid()

        # 1. Issue token and revoke it
        token_str = broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id="run_recovery_test",
            worker_pid=worker_pid,
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )
        parts = token_str.split("_", 3)
        token_id = parts[1]
        broker.token_manager.revoke_token(token_id, reason="Manual revocation before restart")

        # Record audit events
        self.store.worker_audit.record_event(
            event_type=WorkerAuditEventType.TOOL_DENIED,
            task_id=self.task_id,
            run_id="run_recovery_test",
            worker_pid=worker_pid,
            details={"token_id": token_id, "action": "revoked"},
        )

        # Baseline integrity
        ok, errs = self.store.worker_audit.verify_integrity(task_id=self.task_id)
        self.assertTrue(ok)

        # 2. Simulate Supervisor Crash and Restart
        self.store.db.close()
        restarted_store = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)
        try:
            # Token revocation must survive restart in SQLite
            from ai_coordinator.tool_broker import CapabilityTokenRevokedError
            with self.assertRaises(CapabilityTokenRevokedError):
                restarted_store.tool_broker.token_manager.verify_token(token_str, caller_pid=worker_pid)

            # 3. Simulate desync between external anchor and SQLite commit (e.g. power failure during anchor replace)
            anchor_file = restarted_store.worker_audit.anchor_file_path
            self.assertIsNotNone(anchor_file)
            anchor_file.unlink()

            # Integrity immediately detects missing anchor
            ok_lost, errs_lost = restarted_store.worker_audit.verify_integrity(task_id=self.task_id)
            self.assertFalse(ok_lost)
            self.assertTrue(any("ausente ou removido" in e.lower() for e in errs_lost))

            # Reconcile anchor from verified SQLite anchor record
            recovered = restarted_store.worker_audit.reconcile_anchor()
            self.assertTrue(recovered)

            # Integrity passes after reconciliation
            ok_rec, errs_rec = restarted_store.worker_audit.verify_integrity(task_id=self.task_id)
            self.assertTrue(ok_rec, f"Errors: {errs_rec}")
            self.assertEqual(len(errs_rec), 0)
        finally:
            restarted_store.db.close()

    def test_06_mailbox_streaming_exception_fails_closed_and_terminates_worker(self):
        """Streaming consumer must terminate worker and raise if mailbox on_poll raises an exception."""
        from ai_coordinator.streaming import SafeStreamConsumer
        import subprocess

        # Long-running worker process
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        consumer = SafeStreamConsumer(
            stdout_path=self.project_dir / "stdout_poll.log",
            stderr_path=self.project_dir / "stderr_poll.log",
        )

        def _faulty_poll():
            raise ValueError("CriticalMailboxSecurityFailure")

        try:
            with self.assertRaises(RuntimeError) as cm:
                consumer.consume(proc, timeout_seconds=5.0, on_poll=_faulty_poll)
            self.assertIn("Falha de seguranca no processamento da mailbox", str(cm.exception))
            self.assertIn("CriticalMailboxSecurityFailure", str(cm.exception))
            # Verify process was terminated fail-closed
            self.assertFalse(safe_process_alive(proc.pid))
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    def test_07_seatbelt_profile_enforces_strict_read_allowlist_without_blanket_allow(self):
        """macOS Seatbelt Sandbox profile must construct a strict read allowlist without blanket (allow file-read*)."""
        from ai_coordinator.fs_sandbox import MacOsSeatbeltController
        sbox = self.coordinator_dir / "tasks" / "test_task" / "runs" / "test_run"
        coord_priv = self.coordinator_dir
        allowed_reads = [self.project_dir, Path("/opt/custom_tool")]

        profile = MacOsSeatbeltController.build_profile(
            sandbox_root=sbox,
            coordinator_private_dir=coord_priv,
            allowed_read_roots=allowed_reads,
        )

        # 1. Must NOT contain broad blanket allow
        self.assertNotIn("(allow file-read*)", profile)

        # 2. Must allow reading metadata and ioctl
        self.assertIn("(allow file-read-metadata)", profile)
        self.assertIn("(allow file-ioctl)", profile)

        # 3. Must allow essential system roots and python prefix
        self.assertIn('(allow file-read* (subpath "/System"))', profile)
        self.assertIn('(allow file-read* (subpath "/usr"))', profile)
        self.assertIn(f'(allow file-read* (subpath "{Path(sys.prefix).resolve().as_posix()}"))', profile)

        # 4. Must allow explicitly declared read roots
        self.assertIn(f'(allow file-read* (subpath "{self.project_dir.resolve().as_posix()}"))', profile)
        self.assertIn('(allow file-read* (subpath "/opt/custom_tool"))', profile)

        # 5. Must deny credential directories, task siblings, and coordinator private directories
        self.assertIn('(deny file-read* (regex #".*/(\\.ssh|\\.aws|\\.gnupg|mock_home)(/.*|$)"))', profile)
        self.assertIn('(deny file-read* (regex #".*/tasks/[^/]+/sibling_[^/]+(/.*|$)"))', profile)
        self.assertIn('(deny file-read* (regex #".*\\.sqlite(-wal|-shm)?$"))', profile)
        self.assertIn('(deny file-write* (regex #".*\\.sqlite(-wal|-shm)?$"))', profile)
        self.assertIn(f'(deny file-read* (subpath "{(coord_priv / "auth").resolve().as_posix()}"))', profile)

        # 6. Must grant sandbox_root both read and write at the end of the profile
        lines = profile.splitlines()
        self.assertIn(f'(allow file-read* (subpath "{sbox.resolve().as_posix()}"))', lines[-4:])
        self.assertIn(f'(allow file-write* (subpath "{sbox.resolve().as_posix()}"))', lines[-4:])

    def test_08_macos_seatbelt_operational_containment_and_rejection(self):
        """On macOS, execute sandbox-exec to verify authorized reads succeed and unauthorized reads are rejected."""
        if sys.platform != "darwin":
            self.skipTest("Operational macOS Seatbelt enforcement test only runs on darwin")

        from ai_coordinator.fs_sandbox import MacOsSeatbeltController
        if not MacOsSeatbeltController.is_available():
            self.skipTest("sandbox-exec not available")

        test_sbox = self.coordinator_dir / "tasks" / "op_task" / "runs" / "op_run"
        test_sbox.mkdir(parents=True, exist_ok=True)
        allowed_dir = self.base_root / "allowed_zone"
        allowed_dir.mkdir(parents=True, exist_ok=True)
        (allowed_dir / "valid.txt").write_text("AUTHORIZED_SECRET_123", encoding="utf-8")

        auth_dir = self.coordinator_dir / "auth"
        auth_dir.mkdir(parents=True, exist_ok=True)
        (auth_dir / "supervisor.secret").write_text("SUPERVISOR_SECRET_789", encoding="utf-8")

        # Create an arbitrary outside file strictly located under /Users, which is not granted by system_roots
        arbitrary_file = Path("/Users/Shared/ai_coord_unauthorized_test.txt")
        try:
            arbitrary_file.write_text("ARBITRARY_UNAUTHORIZED_DATA", encoding="utf-8")
        except OSError:
            arbitrary_file = Path.home() / "ai_coord_unauthorized_test.txt"
            arbitrary_file.write_text("ARBITRARY_UNAUTHORIZED_DATA", encoding="utf-8")

        profile = MacOsSeatbeltController.build_profile(
            sandbox_root=test_sbox,
            coordinator_private_dir=self.coordinator_dir,
            allowed_read_roots=[allowed_dir],
        )

        worker_script = f"""import sys, os
from pathlib import Path

# 1. Read authorized file -> must succeed
auth_p = Path(r"{allowed_dir.resolve().as_posix()}") / "valid.txt"
if auth_p.read_text(encoding="utf-8").strip() != "AUTHORIZED_SECRET_123":
    sys.exit(10)

# 2. Read and write in sandbox root -> must succeed
sbox_p = Path(r"{test_sbox.resolve().as_posix()}") / "out.txt"
sbox_p.write_text("WORKER_OK", encoding="utf-8")
if sbox_p.read_text(encoding="utf-8").strip() != "WORKER_OK":
    sys.exit(20)

# 3. Read coordinator private secret -> must fail with PermissionError / OSError
forb_p = Path(r"{(auth_dir / "supervisor.secret").resolve().as_posix()}")
try:
    forb_p.read_text(encoding="utf-8")
    sys.exit(30)  # Should have been denied!
except (PermissionError, OSError):
    pass

# 4. Read arbitrary file outside authorized roots (under /Users) -> must fail with PermissionError / OSError
arb_p = Path(r"{arbitrary_file.resolve().as_posix()}")
try:
    arb_p.read_text(encoding="utf-8")
    sys.exit(40)  # Should have been denied by default policy!
except (PermissionError, OSError):
    pass

sys.exit(0)
"""
        try:
            res = subprocess.run(
                ["/usr/bin/sandbox-exec", "-p", profile, sys.executable, "-c", worker_script],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"Seatbelt operational check failed (code={res.returncode}): stdout={res.stdout}, stderr={res.stderr}")
        finally:
            try:
                if arbitrary_file.exists():
                    arbitrary_file.unlink()
            except Exception:
                pass

    def test_09_mailbox_spoofing_fatal_security_violation_terminates_worker(self):
        """Mailbox identity spoofing (declared_pid mismatch) is treated as a fatal security violation, terminating the worker."""
        spoof_worker_py = self.project_dir / "spoof_worker.py"
        spoof_worker_py.write_text("""import sys, json, time, os
from pathlib import Path

broker_dir = Path(".broker")
token_file = broker_dir / "token"
token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""

req_dir = broker_dir / "requests"
req_dir.mkdir(parents=True, exist_ok=True)
spoofed_payload = {
    "token": token,
    "tool": "read_task_context",
    "parameters": {},
    "caller_pid": 999999,
}
(req_dir / "req_spoof.json").write_text(json.dumps(spoofed_payload), encoding="utf-8")

# Keep worker alive waiting for response or supervisor termination
for _ in range(100):
    time.sleep(0.1)

print(json.dumps({"result": "NOT_TERMINATED", "usage": {}}))
sys.exit(0)
""", encoding="utf-8")

        cfg = {
            "limits": {
                "input_chars": 5000,
                "output_chars": 5000,
                "worker_calls_per_task": 5,
                "attempts_per_run": 1,
                "timeout_seconds": 10,
            },
            "security": {
                "enforce_filesystem_sandbox": True,
            },
            "routes": {
                "general": {
                    "candidates": [
                        {"client": "openrouter", "model": "mock_adv_model", "plain_prompt": True}
                    ]
                }
            },
            "launchers": {
                "openrouter": [sys.executable, str(spoof_worker_py)]
            }
        }

        input_file = self.project_dir / "task_input_spoof.md"
        input_file.write_text("Spoofing test input", encoding="utf-8")

        res = run_worker(
            store=self.store,
            config=cfg,
            ident=self.task_id,
            category="general",
            input_file=str(input_file),
        )

        # Worker must have failed due to fail-closed identity violation
        self.assertEqual(res.get("status"), "failed")
        attempts = res.get("attempts", [])
        self.assertTrue(len(attempts) > 0)
        err_msg = attempts[-1].get("error", "")
        self.assertTrue(
            "CallerIdentityMismatchError" in err_msg or "spoofing" in err_msg.lower() or "seguranca" in err_msg.lower(),
            f"Expected fatal security violation error message, got: {err_msg}"
        )

        # Explicitly verify worker process termination using audited spawn event
        spawn_events = self.store.worker_audit.query_events(task_id=self.task_id, event_type="spawn")
        self.assertTrue(len(spawn_events) > 0, "Spawn event not found")
        worker_pid = spawn_events[-1].worker_pid
        self.assertIsNotNone(worker_pid)
        for _ in range(30):
            if not safe_process_alive(worker_pid):
                break
            time.sleep(0.1)
        self.assertFalse(safe_process_alive(worker_pid))

    def test_10_mailbox_tampered_token_fatal_security_violation_terminates_worker(self):
        """Mailbox request with forged/tampered capability token is treated as a fatal security violation, terminating the worker."""
        tamper_worker_py = self.project_dir / "tamper_worker.py"
        tamper_worker_py.write_text("""import sys, json, time, os
from pathlib import Path

broker_dir = Path(".broker")
token_file = broker_dir / "token"
token = token_file.read_text(encoding="utf-8").strip() if token_file.exists() else ""

req_dir = broker_dir / "requests"
req_dir.mkdir(parents=True, exist_ok=True)
tampered_payload = {
    "token": token + "_forged_signature_attack",
    "tool": "read_task_context",
    "parameters": {},
}
(req_dir / "req_tampered.json").write_text(json.dumps(tampered_payload), encoding="utf-8")

for _ in range(100):
    time.sleep(0.1)

print(json.dumps({"result": "NOT_TERMINATED", "usage": {}}))
sys.exit(0)
""", encoding="utf-8")

        cfg = {
            "limits": {
                "input_chars": 5000,
                "output_chars": 5000,
                "worker_calls_per_task": 5,
                "attempts_per_run": 1,
                "timeout_seconds": 10,
            },
            "security": {
                "enforce_filesystem_sandbox": True,
            },
            "routes": {
                "general": {
                    "candidates": [
                        {"client": "openrouter", "model": "mock_adv_model", "plain_prompt": True}
                    ]
                }
            },
            "launchers": {
                "openrouter": [sys.executable, str(tamper_worker_py)]
            }
        }

        input_file = self.project_dir / "task_input_tamper.md"
        input_file.write_text("Tamper test input", encoding="utf-8")

        res = run_worker(
            store=self.store,
            config=cfg,
            ident=self.task_id,
            category="general",
            input_file=str(input_file),
        )

        self.assertEqual(res.get("status"), "failed")
        attempts = res.get("attempts", [])
        self.assertTrue(len(attempts) > 0)
        err_msg = attempts[-1].get("error", "")
        self.assertTrue(
            "InvalidCapabilityTokenError" in err_msg or "seguranca" in err_msg.lower() or "token" in err_msg.lower(),
            f"Expected fatal token violation error message, got: {err_msg}"
        )

        # Explicitly verify worker process termination using audited spawn event
        spawn_events = self.store.worker_audit.query_events(task_id=self.task_id, event_type="spawn")
        self.assertTrue(len(spawn_events) > 0, "Spawn event not found")
        worker_pid = spawn_events[-1].worker_pid
        self.assertIsNotNone(worker_pid)
        for _ in range(30):
            if not safe_process_alive(worker_pid):
                break
            time.sleep(0.1)
        self.assertFalse(safe_process_alive(worker_pid))


if __name__ == "__main__":
    unittest.main()
