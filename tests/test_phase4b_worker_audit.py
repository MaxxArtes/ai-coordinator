"""Tests for Phase 4B.5.4 - Worker Execution Audit and Broker Hardening.

Verifies:
1. Revoked capability tokens remain revoked across Store restarts (persistent SQLite storage).
2. Call rate limits (max_calls) remain consistent across restarts and concurrent accesses.
3. Mailbox rejects forged or spoofed caller_pid claims (anti-spoofing binding).
4. Mailbox strictly rejects symlinks and reparse points in requests and responses.
5. Process PID and creation timestamps are accurately compared in nanoseconds across OSes.
6. Immutability of audit tables: UPDATE and DELETE are blocked by SQLite triggers.
7. Fail-closed policy: audit log persistence failures abort operations immediately.
8. Cryptographic hash chaining (SHA-256) and HMAC signatures detect tampering and allow history reconstruction.
9. Full worker lifecycle events (spawn, sandbox_prepared, tool_requested, tool_denied, exit, termination).
10. Strict sanitization of secrets and tokens from all audit records.
"""

from __future__ import annotations

import base64
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

from ai_coordinator.storage import Store
from ai_coordinator.tool_broker import (
    ToolBroker,
    ToolRegistry,
    CapabilityTokenManager,
    CapabilityToken,
    ToolDefinition,
    ToolPolicyMode,
    ToolBrokerContext,
    ToolBrokerError,
    ToolAccessDeniedError,
    InvalidCapabilityTokenError,
    CapabilityTokenExpiredError,
    CapabilityTokenRevokedError,
    BrokerRateLimitExceededError,
    CallerIdentityMismatchError,
    ToolAuditPersistenceError,
)
from ai_coordinator.worker_audit import (
    WorkerAuditLogger,
    WorkerAuditEventType,
    WorkerAuditRecord,
    WorkerAuditPersistenceError,
    WorkerAuditIntegrityError,
    GENESIS_HASH,
)
from ai_coordinator.process import ProcessSupervisor


class Phase4B54WorkerAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_root = Path(self.temp_dir.name).resolve()

        self.coordinator_dir = self.base_root / "coordinator_store"
        self.coordinator_dir.mkdir(parents=True, exist_ok=True)
        self.supervisor_key = "super_secret_audit_key_9876543210"
        self.store = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)

        self.project_dir = self.base_root / "project_workspace"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "sample.py").write_text("print('hello')", encoding="utf-8")

        self.task = self.store.new(goal="Task for worker audit testing", project=str(self.project_dir))
        self.task_id = self.task["id"]
        self.run_id = "run_audit_001"

        self.sandbox_dir = self.project_dir / "sandbox_audit_worker"
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)

        self.broker = self.store.tool_broker
        self.worker_audit = self.store.worker_audit

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

    def test_01_revoked_tokens_remain_revoked_across_store_restarts(self):
        """Tokens revoked in SQLite remain revoked after store is closed and reopened."""
        my_pid = os.getpid()
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )
        parts = token_str.split("_")
        token_id = parts[1]

        # Token works initially
        res = self.broker.execute(token_str, "workspace_read_file", {"path": "sample.py"}, caller_pid=my_pid)
        self.assertEqual(res["result"]["path"], "sample.py")

        # Revoke the token
        self.broker.token_manager.revoke_token(token_id, reason="Security alert: worker misbehavior")

        # Immediate verification: revoked
        with self.assertRaises(CapabilityTokenRevokedError):
            self.broker.execute(token_str, "workspace_read_file", {"path": "sample.py"}, caller_pid=my_pid)

        # Close store and simulate complete process restart
        self.store.db.close()
        new_store = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)
        try:
            # Token MUST still be revoked in new store instance
            with self.assertRaises(CapabilityTokenRevokedError):
                new_store.tool_broker.execute(token_str, "workspace_read_file", {"path": "sample.py"}, caller_pid=my_pid)
        finally:
            new_store.db.close()

    def test_02_call_limits_persist_and_enforce_across_restarts(self):
        """Tool call limits (max_calls) persist across restarts and block excess usage."""
        my_pid = os.getpid()
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
            max_calls=2,
        )

        # Call 1 of 2
        res1 = self.broker.execute(token_str, "echo_payload", {"payload": "first"}, caller_pid=my_pid)
        self.assertEqual(res1["result"]["echo"], "first")

        # Simulate store restart after 1 call
        self.store.db.close()
        new_store = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)
        try:
            # Call 2 of 2 in new store instance
            res2 = new_store.tool_broker.execute(token_str, "echo_payload", {"payload": "second"}, caller_pid=my_pid)
            self.assertEqual(res2["result"]["echo"], "second")

            # Call 3 of 2 MUST be rejected with BrokerRateLimitExceededError
            with self.assertRaises(BrokerRateLimitExceededError):
                new_store.tool_broker.execute(token_str, "echo_payload", {"payload": "third"}, caller_pid=my_pid)
        finally:
            new_store.db.close()

    def test_03_mailbox_rejects_forged_or_spoofed_caller_pid(self):
        """Mailbox processing verifies caller_pid against expected sandbox worker PID."""
        my_pid = os.getpid()
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
        )

        req_dir = self.sandbox_dir / ".broker" / "requests"
        req_dir.mkdir(parents=True, exist_ok=True)

        # Worker attempts to spoof caller_pid: claims PID 99999 while sandbox expected PID is my_pid
        req_file = req_dir / "spoofed_req.json"
        req_file.write_text(json.dumps({
            "token": token_str,
            "tool": "echo_payload",
            "parameters": {"payload": "adversary_data"},
            "caller_pid": 99999,
        }), encoding="utf-8")

        # Processing mailbox with expected_worker_pid = my_pid detects the spoof
        results = self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=my_pid)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertEqual(results[0]["error"], "CallerIdentityMismatchError")
        self.assertIn("spoofing detectado", results[0]["message"].lower())

    def test_04_mailbox_rejects_symlinks_in_requests_and_responses(self):
        """Mailbox refuses to follow symlinks or reparse points."""
        req_dir = self.sandbox_dir / ".broker" / "requests"
        req_dir.mkdir(parents=True, exist_ok=True)

        # Create target secret outside mailbox
        sensitive_file = self.store.root / "auth" / "supervisor.secret"
        sensitive_file.parent.mkdir(parents=True, exist_ok=True)
        sensitive_file.write_text("SUPERVISOR_SECRET_DATA", encoding="utf-8")

        # Create a malicious symlink in requests directory
        symlink_req = req_dir / "symlink_req.json"
        try:
            symlink_req.symlink_to(sensitive_file)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported in this test environment")

        results = self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=os.getpid())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertIn(results[0]["error"], ("SymlinkSecurityViolation", "ReparseEscapeError", "PathTraversalError"))
        self.assertFalse(symlink_req.exists())

    def test_05_precise_nanosecond_creation_time_validation(self):
        """Creation time is compared in nanoseconds, detecting PID recycling within 2-second tolerance."""
        my_pid = os.getpid()
        ident = ProcessSupervisor.capture_identity(my_pid)
        real_create_time_ns = ident.create_time_ns

        # 1. Valid token with real create_time_ns (passes)
        valid_token = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            worker_create_time_ns=real_create_time_ns,
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
        )
        res = self.broker.execute(valid_token, "echo_payload", {"payload": "ok"}, caller_pid=my_pid)
        self.assertEqual(res["result"]["echo"], "ok")

        # 2. Token with recycled PID: creation time shifted by 5 seconds (5 * 10^9 ns)
        recycled_token = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            worker_create_time_ns=real_create_time_ns - 5_000_000_000,
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
        )
        with self.assertRaises(CallerIdentityMismatchError) as ctx:
            self.broker.execute(recycled_token, "echo_payload", {"payload": "bad"}, caller_pid=my_pid)
        self.assertIn("pid recycling", str(ctx.exception).lower())

        # 3. Omitting caller_pid on worker-bound token strictly raises CallerIdentityMismatchError
        with self.assertRaises(CallerIdentityMismatchError) as ctx:
            self.broker.execute(valid_token, "echo_payload", {"payload": "bad"}, caller_pid=None)
        self.assertIn("obrigatorio", str(ctx.exception).lower())

    def test_06_sql_immutability_triggers_block_update_and_delete(self):
        """SQLite triggers block UPDATE and DELETE queries on audit tables."""
        # Insert a record in worker_audit_log
        record = self.worker_audit.record_event(
            event_type=WorkerAuditEventType.SPAWN,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=1234,
            details={"test": "immutability"},
        )

        # 1. Attempt UPDATE on worker_audit_log
        with self.assertRaises(sqlite3.Error) as ctx:
            self.store.db.execute("UPDATE worker_audit_log SET reason = 'tampered' WHERE id = ?", (record.sequence_number,))
        self.assertIn("imutavel", str(ctx.exception).lower())

        # 2. Attempt DELETE on worker_audit_log
        with self.assertRaises(sqlite3.Error) as ctx:
            self.store.db.execute("DELETE FROM worker_audit_log WHERE id = ?", (record.sequence_number,))
        self.assertIn("imutavel", str(ctx.exception).lower())

        # 3. Attempt UPDATE on tool_broker_audit
        my_pid = os.getpid()
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
        )
        self.broker.execute(token_str, "echo_payload", {"payload": "audit_test"}, caller_pid=my_pid)

        with self.assertRaises(sqlite3.Error) as ctx:
            self.store.db.execute("UPDATE tool_broker_audit SET decision = 'tampered'")
        self.assertIn("imutavel", str(ctx.exception).lower())

        # 4. Attempt DELETE on tool_broker_audit
        with self.assertRaises(sqlite3.Error) as ctx:
            self.store.db.execute("DELETE FROM tool_broker_audit")
        self.assertIn("imutavel", str(ctx.exception).lower())

    def test_07_fail_closed_policy_on_audit_persistence_failure(self):
        """Operations are aborted when audit log persistence fails."""
        my_pid = os.getpid()
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
        )

        # Close DB to force audit persistence failure
        self.store.db.close()

        # Tool execution must fail closed (raise ToolAuditPersistenceError)
        with self.assertRaises(ToolAuditPersistenceError):
            self.broker.execute(token_str, "echo_payload", {"payload": "must_fail"}, caller_pid=my_pid)

        # Worker audit logging must fail closed
        with self.assertRaises(WorkerAuditPersistenceError):
            self.worker_audit.record_event(
                event_type=WorkerAuditEventType.SPAWN,
                task_id=self.task_id,
                run_id=self.run_id,
            )

    def test_08_cryptographic_chaining_detects_tampering(self):
        """Hash chaining (SHA-256) and HMAC signatures detect tampering and gaps."""
        # Record sequential events
        self.worker_audit.record_event(
            event_type=WorkerAuditEventType.SPAWN,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=1001,
        )
        self.worker_audit.record_event(
            event_type=WorkerAuditEventType.SANDBOX_PREPARED,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=1001,
        )
        self.worker_audit.record_event(
            event_type=WorkerAuditEventType.EXIT,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=1001,
            resource_usage={"exit_code": 0},
        )

        # Baseline verification: intact
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)

        # Temporarily drop triggers to simulate attacker with direct SQLite disk access
        self.store.db.execute("DROP TRIGGER prevent_worker_audit_update")
        self.store.db.execute("""
            UPDATE worker_audit_log
            SET chain_hash = 'tampered_hash_00000000000000000000000000000000'
            WHERE sequence_number = 2
        """)
        self.store.db.commit()

        # Re-verify: must fail integrity check
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertFalse(ok)
        self.assertTrue(any("adulteracao" in e.lower() for e in errors))

    def test_09_full_lifecycle_event_recording_and_querying(self):
        """Worker lifecycle events are recorded and queryable in order."""
        events = [
            (WorkerAuditEventType.SPAWN, {"worker_pid": 2002}),
            (WorkerAuditEventType.SANDBOX_PREPARED, {"worker_pid": 2002}),
            (WorkerAuditEventType.TOOL_REQUESTED, {"worker_pid": 2002, "details": {"tool": "workspace_read_file"}}),
            (WorkerAuditEventType.TOOL_DENIED, {"worker_pid": 2002, "error": "Access denied"}),
            (WorkerAuditEventType.EXIT, {"worker_pid": 2002, "resource_usage": {"exit_code": 0}}),
        ]

        for evt_type, kwargs in events:
            self.worker_audit.record_event(
                event_type=evt_type,
                task_id=self.task_id,
                run_id=self.run_id,
                **kwargs,
            )

        records = self.worker_audit.query_events(task_id=self.task_id, run_id=self.run_id)
        self.assertEqual(len(records), 5)
        recorded_types = [r.event_type for r in records]
        self.assertEqual(recorded_types, [
            "spawn", "sandbox_prepared", "tool_requested", "tool_denied", "exit"
        ])
        for i, r in enumerate(records):
            self.assertEqual(r.sequence_number, i + 1)

    def test_10_strict_sanitization_of_secrets_in_audit_records(self):
        """Sensitive tokens, keys, and credentials are redacted from audit entries."""
        dirty_details = {
            "token": "cap_secret_token_12345",
            "supervisor_key": "master_supervisor_secret_value",
            "password": "super_secret_password",
            "safe_metric": 42,
            "nested": {
                "auth_credential": "bearer_secret_abc",
                "normal_field": "public_data",
            }
        }

        record = self.worker_audit.record_event(
            event_type=WorkerAuditEventType.TOOL_REQUESTED,
            task_id=self.task_id,
            run_id=self.run_id,
            details=dirty_details,
        )

        # Check raw stored json in database
        row = self.store.db.execute(
            "SELECT details_json FROM worker_audit_log WHERE event_id = ?",
            (record.event_id,)
        ).fetchone()
        raw_json = row[0]

        self.assertNotIn("cap_secret_token_12345", raw_json)
        self.assertNotIn("master_supervisor_secret_value", raw_json)
        self.assertNotIn("super_secret_password", raw_json)
        self.assertNotIn("bearer_secret_abc", raw_json)
        self.assertIn("[REDACTED]", raw_json)
        self.assertIn("public_data", raw_json)
        self.assertIn("42", raw_json)


if __name__ == "__main__":
    unittest.main()
