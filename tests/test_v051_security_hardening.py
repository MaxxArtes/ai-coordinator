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
        else:
            cur_time_ns = PosixProcessController.get_creation_time(my_pid)
            legacy_time = cur_time_ns

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


if __name__ == "__main__":
    unittest.main()
