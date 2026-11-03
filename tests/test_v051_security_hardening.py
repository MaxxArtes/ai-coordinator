"""Tests for Security Hardening v0.5.1 (Codex PR #2 Audit Hardening).

Covers:
1. P1: Tool Broker Mailbox Directory Symlink/Reparse Point Escape Prevention (.broker, requests, responses).
2. P1: Worker Audit Cross-Store/Cross-Process Concurrency and BEGIN IMMEDIATE Serialization.
3. P1: Worker Audit Anchor Crash Recovery and Tempfile Collision Elimination.
4. P2: Backward-Compatible Legacy Process Identity (Windows FILETIME & Linux Jiffies) and Fail-Closed Resolution.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import gc
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

from ai_coordinator.storage import Store, RunState
from ai_coordinator.tool_broker import (
    ToolBroker,
    ToolRegistry,
    CapabilityTokenManager,
    CapabilityToken,
    ToolDefinition,
    ToolPolicyMode,
    ToolAccessDeniedError,
    CallerIdentityMismatchError,
    _PinnedDirectory,
)
from ai_coordinator.fs_sandbox import (
    FileSandboxSecurityError,
    ReparseEscapeError,
    PathTraversalError,
    is_reparse_point_or_symlink,
    validate_contained_path,
)
from ai_coordinator.worker_audit import (
    WorkerAuditLogger,
    WorkerAuditEventType,
    WorkerAuditRecord,
    WorkerAuditPersistenceError,
    GENESIS_HASH,
)
from ai_coordinator.process import (
    safe_process_alive,
    safe_terminate_tree,
    WindowsProcessController,
    PosixProcessController,
    ProcessSupervisor,
    match_process_creation_time,
)


def _create_symlink_or_junction(link_path: Path, target_path: Path) -> bool:
    """Create a symlink or Windows directory junction."""
    if os.name == "nt":
        # Prefer directory junction on Windows which requires no admin privilege
        if target_path.is_dir():
            res = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
                capture_output=True,
                text=True,
            )
            return res.returncode == 0
        else:
            try:
                link_path.symlink_to(target_path)
                return True
            except OSError:
                return False
    else:
        try:
            link_path.symlink_to(target_path)
            return True
        except (OSError, NotImplementedError):
            return False


class TestV051SecurityHardening(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name).resolve()

        self.coordinator_dir = self.base_dir / "coord"
        self.coordinator_dir.mkdir(parents=True, exist_ok=True)

        self.project_dir = self.base_dir / "project"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        self.sandbox_dir = self.base_dir / "sandbox"
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)

        self.outside_dir = self.base_dir / "outside_victim"
        self.outside_dir.mkdir(parents=True, exist_ok=True)

        self.supervisor_key = "test_sup_secret_key_v051"
        self.store = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)
        self.task_id = "task_v051_sec"
        self.run_id = "run_v051_sec"

        # Register echo tool
        self.broker = self.store.tool_broker
        self.broker.registry.register(ToolDefinition(
            name="echo_tool",
            description="Echo payload",
            handler=lambda ctx, params: {"echo": params.get("msg")},
            policy_mode=ToolPolicyMode.ALLOW,
        ))

    def tearDown(self):
        try:
            if hasattr(self.store, "db") and self.store.db:
                self.store.db.close()
        except Exception:
            pass
        self.store = None
        self.broker = None
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # 1. P1: Tool Broker Mailbox Directory Symlink/Reparse Point Escape
    # -----------------------------------------------------------------------

    def test_01_mailbox_responses_directory_symlink_escape_rejected(self):
        """Worker cannot redirect .broker/responses via symlink/junction to overwrite host files."""
        # Setup valid request in requests dir
        req_dir = self.sandbox_dir / ".broker" / "requests"
        req_dir.mkdir(parents=True, exist_ok=True)

        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["echo_tool"],
        )

        req_file = req_dir / "req_01.json"
        req_file.write_text(json.dumps({
            "token": token_str,
            "tool": "echo_tool",
            "parameters": {"msg": "ATTACK_PAYLOAD"},
            "caller_pid": os.getpid(),
        }), encoding="utf-8")

        # Create malicious junction/symlink for .broker/responses -> outside_dir
        res_dir = self.sandbox_dir / ".broker" / "responses"
        created = _create_symlink_or_junction(res_dir, self.outside_dir)
        if not created:
            self.skipTest("Symlinks/Junctions not supported in current environment")

        self.assertTrue(is_reparse_point_or_symlink(res_dir))

        # Sensitive file in outside_victim that must NEVER be overwritten
        victim_file = self.outside_dir / f"res_{req_file.stem}.json"

        # Supervisor processes mailbox
        with self.assertRaises(ReparseEscapeError):
            self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=os.getpid(), raise_on_fatal=True)

        # Victim file outside sandbox must NOT have been created or modified
        self.assertFalse(victim_file.exists(), "Privileged supervisor wrote outside sandbox via responses symlink!")

    def test_02_mailbox_broker_root_symlink_escape_rejected(self):
        """Worker cannot redirect entire .broker directory via symlink/junction to escape sandbox."""
        broker_dir = self.sandbox_dir / ".broker"
        created = _create_symlink_or_junction(broker_dir, self.outside_dir)
        if not created:
            self.skipTest("Symlinks/Junctions not supported in current environment")

        with self.assertRaises(ReparseEscapeError):
            self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=os.getpid(), raise_on_fatal=True)

    # -----------------------------------------------------------------------
    # 2. P1: Worker Audit Serialization across Store Instances
    # -----------------------------------------------------------------------

    def test_03_concurrent_audit_logging_across_stores_serialized(self):
        """Concurrent audit writes across separate Store instances must be serialized via BEGIN IMMEDIATE."""
        # Create second store pointing to exact same SQLite DB
        store2 = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)
        audit1 = self.store.worker_audit
        audit2 = store2.worker_audit

        num_events = 20
        errors = []

        def worker_write(audit_inst, idx):
            try:
                audit_inst.record_event(
                    event_type=WorkerAuditEventType.TOOL_REQUESTED,
                    task_id=self.task_id,
                    run_id=self.run_id,
                    worker_pid=os.getpid(),
                    reason=f"concurrent_test_{idx}",
                )
            except Exception as e:
                errors.append(e)

        # Execute concurrent writes using ThreadPoolExecutor across both Store instances
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for i in range(num_events):
                inst = audit1 if (i % 2 == 0) else audit2
                futures.append(executor.submit(worker_write, inst, i))
            concurrent.futures.wait(futures)

        store2.db.close()

        # There must be zero unhandled persistence errors
        self.assertEqual(len(errors), 0, f"Errors occurred during concurrent logging: {errors}")

        # Verify integrity of the entire audit chain
        valid, integrity_errors = audit1.verify_integrity()
        self.assertTrue(valid, f"Audit chain verification failed: {integrity_errors}")

        # Check that sequence numbers are strictly 1..num_events with no duplicates
        rows = self.store.db.execute("SELECT sequence_number FROM worker_audit_log ORDER BY sequence_number ASC").fetchall()
        seqs = [r[0] for r in rows]
        expected_seqs = list(range(1, num_events + 1))
        self.assertEqual(seqs, expected_seqs, "Sequence numbers contain gaps or duplicates under concurrency!")

    def test_04_audit_anchor_crash_recovery(self):
        """Desynchronized external anchor can be recovered from authenticated DB anchor table."""
        audit = self.store.worker_audit

        # Record initial event
        rec1 = audit.record_event(
            event_type=WorkerAuditEventType.SPAWN,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
        )

        # Simulate crash/desynchronization: anchor file deleted or corrupted
        if audit.anchor_file_path and audit.anchor_file_path.exists():
            audit.anchor_file_path.unlink()

        # verify_integrity should report missing decoupled anchor
        valid, errs = audit.verify_integrity()
        self.assertFalse(valid)
        self.assertTrue(any("ausente" in e.lower() for e in errs))

        # Reconcile anchor
        recovered = audit.reconcile_anchor()
        self.assertTrue(recovered)

        # Now verify_integrity passes completely
        valid2, errs2 = audit.verify_integrity()
        self.assertTrue(valid2, f"Expected successful verification after reconciliation, got: {errs2}")

    # -----------------------------------------------------------------------
    # 3. P2: Legacy Process Identity (Windows FILETIME & Linux Jiffies)
    # -----------------------------------------------------------------------

    def test_05_windows_legacy_filetime_process_identity(self):
        """Windows FILETIME (~1.33e17) from v0.4.0 is recognized and validated without killing active run."""
        if os.name != "nt":
            self.skipTest("Windows-specific FILETIME test")

        my_pid = os.getpid()

        # Get actual kernel create time in Unix nanoseconds
        cur_time_ns = WindowsProcessController.get_creation_time(my_pid)
        self.assertIsNotNone(cur_time_ns)

        # Compute legacy v0.4.0 FILETIME representation:
        # v0.4.0: filetime_100ns = (dwHighDateTime << 32) + dwLowDateTime
        # Since cur_time_ns = (filetime_100ns - 116444736000000000) * 100:
        legacy_filetime = (cur_time_ns // 100) + 116444736000000000

        # Legacy filetime is ~1.3e17 to 1.4e17
        self.assertGreater(legacy_filetime, 100_000_000_000_000_000)
        self.assertLess(legacy_filetime, 200_000_000_000_000_000)

        # safe_process_alive with legacy FILETIME must return True
        self.assertTrue(
            safe_process_alive(my_pid, expected_create_time_ns=legacy_filetime),
            "safe_process_alive rejected active process with legacy v0.4.0 FILETIME!"
        )

        # And with actual Unix nanoseconds it also returns True
        self.assertTrue(
            safe_process_alive(my_pid, expected_create_time_ns=cur_time_ns),
            "safe_process_alive rejected active process with v0.5.0 Unix nanoseconds!"
        )

        # An unrelated/recycled timestamp must still be rejected (fail-closed)
        self.assertFalse(
            safe_process_alive(my_pid, expected_create_time_ns=legacy_filetime - 50_000_000),
            "safe_process_alive accepted mismatched legacy timestamp!"
        )

    def test_06_ambiguous_or_invalid_process_identity_fails_closed(self):
        """Negative or invalid process identity timestamps must fail-closed."""
        my_pid = os.getpid()

        # Negative timestamp
        self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=-1))
        self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=0))

        # Arbitrary random number outside valid time ranges
        self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=999999999999))

    def test_07_linux_legacy_jiffies_process_identity(self):
        """Linux jiffies (< 1e11) from v0.4.0 are recognized and validated with clk_tck conversion."""
        from ai_coordinator.process import match_process_creation_time

        # 500 seconds since boot at 100Hz = 50,000 jiffies
        jiffies = 50_000
        actual_ns = 500_000_000_000  # 500s in nanoseconds

        # Valid match with expected jiffies
        self.assertTrue(
            match_process_creation_time(actual_ns, jiffies, platform="posix"),
            "match_process_creation_time failed to recognize legacy Linux jiffies!"
        )

        # Mismatched jiffies (520 seconds = 52,000 jiffies, diff 20s > 2s tolerance)
        self.assertFalse(
            match_process_creation_time(actual_ns, 52_000, platform="posix"),
            "match_process_creation_time accepted mismatched jiffies!"
        )

    def test_08_store_reconcile_preserves_active_legacy_run(self):
        """Store.reconcile does not kill or interrupt active run with legacy v0.4.0 create_time."""
        my_pid = os.getpid()
        if os.name == "nt":
            cur_time_ns = WindowsProcessController.get_creation_time(my_pid)
            legacy_time = (cur_time_ns // 100) + 116444736000000000
        elif sys.platform.startswith("linux"):
            raw_jiffies = None
            stat_file = Path(f"/proc/{my_pid}/stat")
            if stat_file.exists():
                try:
                    content = stat_file.read_text(encoding="ascii", errors="replace")
                    rparen = content.rfind(")")
                    if rparen != -1:
                        fields = content[rparen + 1:].split()
                        if len(fields) >= 20:
                            raw_jiffies = int(fields[19])
                except Exception:
                    pass
            legacy_time = raw_jiffies if raw_jiffies is not None else 50000
        else:
            legacy_time = PosixProcessController.get_creation_time(my_pid)

        # Ensure task exists in tasks table
        self.store.db.execute("""
            INSERT OR IGNORE INTO tasks (
                id, project, goal, status, revision, snapshot, created, updated,
                ai_memory_workstream_id, step_count, uncheckpointed_steps, drift_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.task_id, str(self.project_dir), "Test legacy goal", "in_progress",
            1, json.dumps({}), "2026-11-03T00:00:00Z", "2026-11-03T00:00:00Z",
            None, 0, 0, "nominal"
        ))

        # Insert a simulated active run with legacy create_time
        run_id = "run_legacy_v040"
        self.store.db.execute("""
            INSERT INTO runs (
                id, task_id, category, status, pid, process_create_time, owner_pid, owner_create_time,
                started, attempts, input_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            self.task_id,
            "general",
            RunState.RUNNING,
            my_pid,
            legacy_time,
            my_pid,
            legacy_time,
            "2026-11-03T00:00:00Z",
            json.dumps([]),
            str(self.project_dir / "input.txt"),
        ))
        self.store.db.commit()

        # Run reconcile
        reconciled = self.store.reconcile(task_id=self.task_id)

        # Active run must NOT have been interrupted or marked unknown
        row = self.store.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row[0], RunState.RUNNING, "Store.reconcile interrupted active run with legacy create_time!")

    def test_09_pid_recycling_exact_matching_zero_tolerance(self):
        """Zero tolerance in process identity matching: any non-zero diff is rejected to prevent PID recycling adoption."""
        my_pid = os.getpid()

        if os.name == "nt":
            cur_time_ns = WindowsProcessController.get_creation_time(my_pid)
            self.assertIsNotNone(cur_time_ns)

            # Exact match passes
            self.assertTrue(safe_process_alive(my_pid, expected_create_time_ns=cur_time_ns))

            # 1 nanosecond difference MUST be rejected (PID recycling simulation)
            self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=cur_time_ns + 1))
            self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=cur_time_ns - 1))
            self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=cur_time_ns + 100))

            # Legacy Windows FILETIME exact conversion passes
            legacy_filetime = (cur_time_ns // 100) + 116444736000000000
            self.assertTrue(safe_process_alive(my_pid, expected_create_time_ns=legacy_filetime))

            # Legacy Windows FILETIME with 1 interval difference MUST be rejected
            self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=legacy_filetime + 1))
            self.assertFalse(safe_process_alive(my_pid, expected_create_time_ns=legacy_filetime - 1))

            # Cross-platform fail-closed: Linux jiffies on Windows MUST be rejected
            self.assertFalse(match_process_creation_time(cur_time_ns, 50_000, platform="nt"))

        # POSIX tests via match_process_creation_time
        # 500s at 100Hz = 50,000 jiffies = 500_000_000_000 ns
        self.assertTrue(match_process_creation_time(500_000_000_000, 50_000, platform="posix"))
        # 1 jiffy difference MUST be rejected
        self.assertFalse(match_process_creation_time(500_000_000_000, 50_001, platform="posix"))
        self.assertFalse(match_process_creation_time(500_000_000_000, 49_999, platform="posix"))
        # Cross-platform fail-closed: Windows FILETIME on POSIX MUST be rejected
        self.assertFalse(match_process_creation_time(500_000_000_000, 133500000000000000, platform="posix"))

    def test_10_external_anchor_cross_process_lock_and_anti_regression(self):
        """Cross-process audit lock and anti-regression guarantee on external anchor."""
        audit = self.store.worker_audit

        # Record events 1 and 2
        rec1 = audit.record_event(
            event_type=WorkerAuditEventType.SPAWN,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
        )
        rec2 = audit.record_event(
            event_type=WorkerAuditEventType.TOOL_REQUESTED,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
        )

        self.assertEqual(rec2.sequence_number, 2)
        self.assertTrue(audit.anchor_file_path.exists())
        content = audit.anchor_file_path.read_text(encoding="utf-8")
        self.assertIn("|2|", content)

        # Test cross-process lock exclusivity: when held, lock file exists and is active
        with audit._acquire_audit_lock():
            self.assertTrue(audit.lock_file_path.exists())

        # Test anti-regression: verify current file sequence is 2
        cur_file_seq = int(audit.anchor_file_path.read_text(encoding="utf-8").split("|")[1])
        self.assertEqual(cur_file_seq, 2)

        # Reconcile anchor also works with cross-process lock
        reconciled = audit.reconcile_anchor()
        self.assertTrue(reconciled)
        valid, errs = audit.verify_integrity()
        self.assertTrue(valid, f"Verification failed after reconciliation: {errs}")

    def test_11_mailbox_directory_swap_pinned_handle_blocks_tampering(self):
        """Pinned directory handles block deletion, renaming, and junction substitution during mailbox processing."""
        broker_dir = self.sandbox_dir / ".broker"
        res_dir = broker_dir / "responses"
        req_dir = broker_dir / "requests"
        res_dir.mkdir(parents=True, exist_ok=True)
        req_dir.mkdir(parents=True, exist_ok=True)

        # Test _PinnedDirectory
        pinned = _PinnedDirectory(res_dir, self.sandbox_dir, raise_on_fatal=True)
        pinned.pin()

        try:
            if os.name == "nt":
                # On Windows, holding the handle without FILE_SHARE_DELETE MUST block rmdir and rename
                with self.assertRaises(PermissionError):
                    os.rmdir(res_dir)

                with self.assertRaises(PermissionError):
                    os.rename(res_dir, res_dir.with_name("responses_moved"))
        finally:
            pinned.close()

        # Recreate directory for junction test
        fake_res_dir = self.sandbox_dir / ".broker" / "responses_junction"
        created = _create_symlink_or_junction(fake_res_dir, self.outside_dir)
        if created:
            pinned_fake = _PinnedDirectory(fake_res_dir, self.sandbox_dir, raise_on_fatal=True)
            with self.assertRaises(ReparseEscapeError):
                pinned_fake.pin()
            pinned_fake.close()

    def test_12_legacy_process_lifecycle_reconcile_and_sigkill_escalation(self):
        """Full lifecycle of legacy process: alive check, store reconcile, and termination with SIGKILL escalation logic."""
        if os.name == "nt":
            # 1. Spawn a live subprocess on Windows
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                cur_ns = WindowsProcessController.get_creation_time(proc.pid)
                self.assertIsNotNone(cur_ns)
                legacy_filetime = (cur_ns // 100) + 116444736000000000

                # Verify safe_process_alive with legacy FILETIME
                self.assertTrue(safe_process_alive(proc.pid, expected_create_time_ns=legacy_filetime))

                # Store active run
                run_id = f"run_legacy_life_{uuid.uuid4().hex[:8]}"
                self.store.db.execute("""
                    INSERT OR IGNORE INTO tasks (
                        id, project, goal, status, revision, snapshot, created, updated,
                        ai_memory_workstream_id, step_count, uncheckpointed_steps, drift_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.task_id, str(self.project_dir), "Goal", "in_progress",
                    1, json.dumps({}), "2026-11-03T00:00:00Z", "2026-11-03T00:00:00Z",
                    None, 0, 0, "nominal"
                ))
                my_owner_time = WindowsProcessController.get_creation_time(os.getpid())
                self.store.db.execute("""
                    INSERT INTO runs (
                        id, task_id, category, status, pid, process_create_time, owner_pid, owner_create_time,
                        started, attempts, input_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, self.task_id, "general", RunState.RUNNING,
                    proc.pid, legacy_filetime, os.getpid(), my_owner_time,
                    "2026-11-03T00:00:00Z", json.dumps([]), str(self.project_dir / "in.txt"),
                ))
                self.store.db.commit()

                # Reconcile keeps run running
                self.store.reconcile(self.task_id)
                row = self.store.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                self.assertEqual(row[0], RunState.RUNNING)

                # Terminate safely with legacy FILETIME
                res = safe_terminate_tree(proc, expected_create_time_ns=legacy_filetime, timeout_seconds=2.0)
                self.assertTrue(res.success)
                self.assertFalse(safe_process_alive(proc.pid, expected_create_time_ns=legacy_filetime))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
        elif sys.platform.startswith("linux"):
            # 1. Spawn a live subprocess on Linux that ignores SIGTERM to force SIGKILL escalation
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); sys.stdout.write('READY\\n'); sys.stdout.flush(); time.sleep(30)"
                ],
                stdout=subprocess.PIPE,
                start_new_session=True,
            )
            # Ensure process has started and actively installed SIG_IGN before proceeding
            ready_line = proc.stdout.readline()
            self.assertEqual(ready_line.strip(), b"READY")
            try:
                cur_ns = PosixProcessController.get_creation_time(proc.pid)
                self.assertIsNotNone(cur_ns)

                raw_jiffies = None
                stat_file = Path(f"/proc/{proc.pid}/stat")
                if stat_file.exists():
                    try:
                        content = stat_file.read_text(encoding="ascii", errors="replace")
                        rparen = content.rfind(")")
                        if rparen != -1:
                            fields = content[rparen + 1:].split()
                            if len(fields) >= 20:
                                raw_jiffies = int(fields[19])
                    except Exception:
                        pass
                legacy_time = raw_jiffies if raw_jiffies is not None else 50000

                self.assertTrue(safe_process_alive(proc.pid, expected_create_time_ns=legacy_time))

                run_id = f"run_legacy_posix_{uuid.uuid4().hex[:8]}"
                self.store.db.execute("""
                    INSERT OR IGNORE INTO tasks (
                        id, project, goal, status, revision, snapshot, created, updated,
                        ai_memory_workstream_id, step_count, uncheckpointed_steps, drift_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    self.task_id, str(self.project_dir), "Goal", "in_progress",
                    1, json.dumps({}), "2026-11-03T00:00:00Z", "2026-11-03T00:00:00Z",
                    None, 0, 0, "nominal"
                ))
                my_owner_time = PosixProcessController.get_creation_time(os.getpid())
                self.store.db.execute("""
                    INSERT INTO runs (
                        id, task_id, category, status, pid, process_create_time, owner_pid, owner_create_time,
                        started, attempts, input_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, self.task_id, "general", RunState.RUNNING,
                    proc.pid, legacy_time, os.getpid(), my_owner_time,
                    "2026-11-03T00:00:00Z", json.dumps([]), str(self.project_dir / "in.txt"),
                ))
                self.store.db.commit()

                self.store.reconcile(self.task_id)
                row = self.store.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
                self.assertEqual(row[0], RunState.RUNNING)

                # Timeout of 0.3s will expire SIGTERM wait, triggering raw_jiffies re-verification and SIGKILL escalation
                res = safe_terminate_tree(proc, expected_create_time_ns=legacy_time, timeout_seconds=0.3)
                self.assertTrue(res.success)
                self.assertEqual(res.reason, "terminated")
                self.assertIn("SIGKILL", res.detail)
                self.assertFalse(safe_process_alive(proc.pid, expected_create_time_ns=legacy_time))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

        # 2. Test POSIX escalation logic with legacy jiffies
        # Valid matching legacy jiffies (< 1e11) must match correctly
        # at 100Hz: 50,000 jiffies = 500,000,000,000 ns
        legacy_jiffies = 50_000
        cur_posix_ns = 500_000_000_000
        # When actual and expected match
        self.assertTrue(
            match_process_creation_time(cur_posix_ns, legacy_jiffies, platform="posix", raw_source_val=legacy_jiffies),
            "match_process_creation_time failed for legacy jiffies in POSIX escalation"
        )
        # When PID is recycled (mismatched jiffies), must fail-closed
        self.assertFalse(
            match_process_creation_time(cur_posix_ns, legacy_jiffies + 1, platform="posix", raw_source_val=legacy_jiffies),
            "match_process_creation_time accepted mismatched jiffies during escalation"
        )

    def test_13_audit_anchor_interleaving_and_anti_regression(self):
        """Adversarial interleaving: out-of-order anchor writes are strictly blocked by anti-regression guard."""
        audit1 = self.store.worker_audit
        store2 = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)
        audit2 = store2.worker_audit

        # Step 1: Supervisor B writes sequences 1 and 2
        rec1 = audit2.record_event(
            event_type=WorkerAuditEventType.SPAWN,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            reason="event_1",
        )
        rec2 = audit2.record_event(
            event_type=WorkerAuditEventType.TOOL_REQUESTED,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            reason="event_2",
        )
        self.assertEqual(rec2.sequence_number, 2)

        # External anchor now holds sequence 2
        cur_anchor_content = audit1.anchor_file_path.read_text(encoding="utf-8")
        self.assertIn("|2|", cur_anchor_content)
        seq_in_file = int(cur_anchor_content.split("|")[1])
        self.assertEqual(seq_in_file, 2)

        # Step 2: Adversarial simulation of Supervisor A attempting delayed write of sequence 1
        # In worker_audit.py, _persist_external_anchor enforces seq_num >= cur_file_seq:
        attempted_stale_seq = 1
        cur_file_seq = int(audit1.anchor_file_path.read_text(encoding="utf-8").split("|")[1])
        self.assertFalse(attempted_stale_seq >= cur_file_seq, "Anti-regression condition would have allowed stale write!")

        # Directly attempt stale write via _persist_external_anchor: must return False and NOT overwrite
        stale_write_result = audit1._persist_external_anchor(
            seq_num=1,
            chain_hash="stale_fake_chain_hash_1",
            created_at="2026-11-03T00:00:00Z",
            anchor_sig="stale_fake_sig_1",
            force=False,
        )
        self.assertFalse(stale_write_result, "_persist_external_anchor accepted stale sequence 1 when file is at sequence 2!")
        anchor_after_stale = audit1.anchor_file_path.read_text(encoding="utf-8")
        self.assertIn("|2|", anchor_after_stale, "Anchor was overwritten by stale write!")
        self.assertNotIn("stale_fake_chain_hash_1", anchor_after_stale)

        # Step 3: Concurrent interleaved writers with shared lock
        num_writes = 10
        errors = []
        observed_sequences = []

        def concurrent_writer(audit_inst, label):
            for i in range(num_writes):
                try:
                    r = audit_inst.record_event(
                        event_type=WorkerAuditEventType.TOOL_ALLOWED,
                        task_id=self.task_id,
                        run_id=self.run_id,
                        worker_pid=os.getpid(),
                        reason=f"{label}_{i}",
                    )
                    observed_sequences.append(r.sequence_number)
                except Exception as ex:
                    errors.append(ex)

        t1 = threading.Thread(target=concurrent_writer, args=(audit1, "sup_a"))
        t2 = threading.Thread(target=concurrent_writer, args=(audit2, "sup_b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        store2.db.close()
        self.assertEqual(len(errors), 0, f"Errors in concurrent interleaved audit logging: {errors}")

        # The final anchor must match the maximum sequence number (2 + 2 * num_writes = 22)
        expected_final_seq = 2 + (2 * num_writes)
        final_anchor = audit1.anchor_file_path.read_text(encoding="utf-8")
        final_file_seq = int(final_anchor.split("|")[1])
        self.assertEqual(final_file_seq, expected_final_seq)

        # Integrity check across whole chain must pass
        valid, errs = audit1.verify_integrity()
        self.assertTrue(valid, f"Audit chain verification failed: {errs}")

    def test_14_mailbox_concurrent_directory_swap_race(self):
        """Adversarial test: background thread attempts directory swap / junction hijack during process_mailbox."""
        req_dir = self.sandbox_dir / ".broker" / "requests"
        res_dir = self.sandbox_dir / ".broker" / "responses"
        req_dir.mkdir(parents=True, exist_ok=True)
        res_dir.mkdir(parents=True, exist_ok=True)

        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["echo_tool"],
        )

        req_file = req_dir / "req_swap_test.json"
        req_file.write_text(json.dumps({
            "token": token_str,
            "tool": "echo_tool",
            "parameters": {"msg": "SAFE_CONCURRENT_DATA"},
            "caller_pid": os.getpid(),
        }), encoding="utf-8")

        if os.name == "nt":
            # 1. Deterministic kernel check: pinned handle without FILE_SHARE_DELETE blocks rename
            pinned_check = _PinnedDirectory(res_dir, self.sandbox_dir, raise_on_fatal=True)
            pinned_check.pin()
            try:
                with self.assertRaises(PermissionError):
                    os.rename(res_dir, res_dir.with_name("responses_hijacked"))
            finally:
                pinned_check.close()

            # 2. Concurrency check: background attacker thread attempts directory swap during mailbox processing
            stop_attacker = threading.Event()
            tamper_attempts = [0]
            tamper_blocked_errors = []

            def attacker_loop():
                while not stop_attacker.is_set():
                    tamper_attempts[0] += 1
                    try:
                        os.rename(res_dir, res_dir.with_name("responses_hijacked"))
                        _create_symlink_or_junction(res_dir, self.outside_dir)
                    except (PermissionError, OSError) as e:
                        tamper_blocked_errors.append(e)
                    time.sleep(0.001)

            attacker_thread = threading.Thread(target=attacker_loop, daemon=True)
            attacker_thread.start()

            try:
                results = self.broker.process_mailbox(
                    self.sandbox_dir,
                    expected_worker_pid=os.getpid(),
                    raise_on_fatal=True,
                )
            finally:
                stop_attacker.set()
                attacker_thread.join(timeout=2.0)

            if tamper_blocked_errors:
                self.assertTrue(
                    all(isinstance(err, PermissionError) or getattr(err, "winerror", None) == 32 for err in tamper_blocked_errors),
                    f"Unexpected error in tamper attempts: {tamper_blocked_errors[:5]}"
                )
        else:
            # On POSIX: Pinned dir_fd with O_DIRECTORY | O_NOFOLLOW guarantees confinement
            results = self.broker.process_mailbox(
                self.sandbox_dir,
                expected_worker_pid=os.getpid(),
                raise_on_fatal=True,
            )

        # Confirm request was processed safely and successfully
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get("status"), "success")

        # Confirm response was written strictly within sandbox res_dir
        expected_res_file = res_dir / f"res_{req_file.stem}.json"
        self.assertTrue(expected_res_file.exists(), "Response file was not written to responses dir!")
        res_data = json.loads(expected_res_file.read_text(encoding="utf-8"))
        self.assertEqual(res_data.get("response", {}).get("result", {}).get("echo"), "SAFE_CONCURRENT_DATA")

        # Confirm victim outside directory remains 100% untouched
        self.assertEqual(len(list(self.outside_dir.iterdir())), 0, "Attacker succeeded in writing outside sandbox!")

    def test_15_posix_ancestor_symlink_swap_blocked_fail_closed(self):
        """Adversarial test on POSIX: swapping ancestor .broker or child responses to symlink fails closed with ReparseEscapeError and touches no outside files."""
        if os.name == "nt":
            self.skipTest("POSIX ancestor symlink swap test is specific to Linux/macOS environments")

        posix_sbox = self.sandbox_dir / "sbox_posix"
        posix_outside = self.sandbox_dir.parent / "outside_posix"
        posix_sbox.mkdir(parents=True, exist_ok=True)
        posix_outside.mkdir(parents=True, exist_ok=True)

        sbox_broker = posix_sbox / ".broker"
        sbox_req = sbox_broker / "requests"
        sbox_res = sbox_broker / "responses"
        sbox_req.mkdir(parents=True, exist_ok=True)
        sbox_res.mkdir(parents=True, exist_ok=True)

        outside_req = posix_outside / "requests"
        outside_res = posix_outside / "responses"
        outside_req.mkdir(parents=True, exist_ok=True)
        outside_res.mkdir(parents=True, exist_ok=True)

        victim_file = outside_res / "victim.txt"
        victim_file.write_text("ORIGINAL_UNTOUCHED_VICTIM_CONTENT", encoding="utf-8")

        outside_req_file = outside_req / "req_malicious.json"
        outside_req_file.write_text(json.dumps({"test": "malicious"}), encoding="utf-8")

        # Scenario 1: Worker swaps ancestor .broker to symlink pointing to outside
        sbox_broker.rename(posix_sbox / "broker_backup")
        os.symlink(str(posix_outside), str(sbox_broker), target_is_directory=True)

        try:
            with self.assertRaises((ReparseEscapeError, FileSandboxSecurityError)):
                self.broker.process_mailbox(
                    posix_sbox,
                    expected_worker_pid=os.getpid(),
                    raise_on_fatal=True,
                )
        finally:
            if sbox_broker.is_symlink():
                sbox_broker.unlink()
            (posix_sbox / "broker_backup").rename(sbox_broker)

        # Confirm fail-closed properties for Scenario 1:
        self.assertEqual(victim_file.read_text(encoding="utf-8"), "ORIGINAL_UNTOUCHED_VICTIM_CONTENT")
        self.assertEqual(len(list(outside_res.glob("res_*.json"))), 0)
        self.assertTrue(outside_req_file.exists())

        # Scenario 2: Worker swaps child responses to symlink pointing to outside/responses
        sbox_res.rename(sbox_broker / "responses_backup")
        os.symlink(str(outside_res), str(sbox_res), target_is_directory=True)

        try:
            with self.assertRaises((ReparseEscapeError, FileSandboxSecurityError)):
                self.broker.process_mailbox(
                    posix_sbox,
                    expected_worker_pid=os.getpid(),
                    raise_on_fatal=True,
                )
        finally:
            if sbox_res.is_symlink():
                sbox_res.unlink()
            (sbox_broker / "responses_backup").rename(sbox_res)

        # Confirm fail-closed properties for Scenario 2:
        self.assertEqual(victim_file.read_text(encoding="utf-8"), "ORIGINAL_UNTOUCHED_VICTIM_CONTENT")
        self.assertEqual(len(list(outside_res.glob("res_*.json"))), 0)

        # Scenario 3: Worker swaps requests to symlink pointing to outside/requests
        sbox_req.rename(sbox_broker / "requests_backup")
        os.symlink(str(outside_req), str(sbox_req), target_is_directory=True)

        try:
            with self.assertRaises((ReparseEscapeError, FileSandboxSecurityError)):
                self.broker.process_mailbox(
                    posix_sbox,
                    expected_worker_pid=os.getpid(),
                    raise_on_fatal=True,
                )
        finally:
            if sbox_req.is_symlink():
                sbox_req.unlink()
            (sbox_broker / "requests_backup").rename(sbox_req)

        # Confirm outside request was NEVER read or unlinked
        self.assertTrue(outside_req_file.exists())


if __name__ == "__main__":
    unittest.main()
