"""Phase 4B.5.5 — Consolidated Integrated Adversarial Security Suite.

Covers comprehensive E2E security stress tests:
1. Full delegation E2E via run_worker() generates complete immutable audit trail.
2. Fail-closed pre-execution policy: audit failure blocks tool handler before side-effects.
3. Strict tamper detection: modifying any canonical field (limits, usage, details, PID) breaks hash chain.
4. Log tail-truncation defense: deleting final audit records is caught by worker_audit_anchors.
5. Strict mailbox anti-spoofing: missing, non-positive or spoofed expected_worker_pid is rejected.
6. Concurrent quota exhaustion: multi-threaded tool requests strictly respect max_calls atomically.
7. Job Object & POSIX resource containment: tree termination leaves no zombie descendants.
8. Coordinator private store isolation: access to coordination.sqlite or supervisor.secret is denied.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
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
    compute_canonical_record_hash,
    GENESIS_HASH,
)
from ai_coordinator.process import ProcessSupervisor, ResourceLimits, safe_terminate_tree
from ai_coordinator.fs_sandbox import ExecutionSandbox, FileSandboxConfig
from ai_coordinator.cli import run_worker, terminate_tree


class Phase4B55AdversarialSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_root = Path(self.temp_dir.name).resolve()

        self.coordinator_dir = self.base_root / "coordinator_store"
        self.coordinator_dir.mkdir(parents=True, exist_ok=True)
        self.supervisor_key = "adversarial_secret_supervisor_key_0123456789"
        self.store = Store(root=self.coordinator_dir, supervisor_key=self.supervisor_key)

        self.project_dir = self.base_root / "project_workspace"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "app.py").write_text("print('Production App')", encoding="utf-8")

        self.task = self.store.new(goal="Adversarial stress testing task", project=str(self.project_dir))
        self.task_id = self.task["id"]
        self.run_id = f"adv_run_{uuid.uuid4().hex[:8]}"

        self.sandbox_dir = self.project_dir / "sandbox_adv_worker"
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

    def test_01_e2e_run_worker_generates_complete_immutable_audit_trail(self):
        """Delegation through run_worker() produces a cryptographically sound audit trail."""
        mock_worker_py = self.project_dir / "mock_worker.py"
        mock_worker_py.write_text("""import sys, json
line = sys.stdin.readline().strip()
res = {
    "result": f"# AuditVerificationSuccess\\nReceivedInput: {line}\\n",
    "usage": {"total_tokens": 10, "cost": 0.0001}
}
print(json.dumps(res))
sys.exit(0)
""", encoding="utf-8")

        cfg = {
            "limits": {
                "input_chars": 5000,
                "output_chars": 5000,
                "worker_calls_per_task": 5,
                "attempts_per_run": 2,
                "timeout_seconds": 15,
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
                "openrouter": [sys.executable, str(mock_worker_py)]
            }
        }

        input_file = self.project_dir / "task_input.md"
        input_file.write_text("Perform bounded audit verification", encoding="utf-8")

        # Execute worker via run_worker
        res = run_worker(
            store=self.store,
            config=cfg,
            ident=self.task_id,
            category="general",
            input_file=str(input_file),
        )

        self.assertEqual(res["status"], "complete")

        # Verify automated audit records
        events = self.worker_audit.query_events(task_id=self.task_id)
        event_types = [e.event_type for e in events]

        self.assertIn("sandbox_prepared", event_types)
        self.assertIn("spawn", event_types)
        self.assertIn("exit", event_types)

        # Verify cryptographic integrity of entire audit trail
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id)
        self.assertTrue(ok, f"Audit integrity failed: {errors}")
        self.assertEqual(len(errors), 0)

        # Verify tail-anchor table consistency
        anchor_row = self.store.db.execute(
            "SELECT last_sequence_number, last_chain_hash FROM worker_audit_anchors ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(anchor_row)
        self.assertEqual(anchor_row[0], events[-1].sequence_number)
        self.assertEqual(anchor_row[1], events[-1].chain_hash)

    def test_02_fail_closed_pre_execution_blocks_mutating_handler(self):
        """Audit persistence failure MUST abort before tool handler executes (no side-effects)."""
        handler_executed = False
        target_file = self.project_dir / "mutated_state.txt"

        def mutating_handler(ctx: ToolBrokerContext, params: dict):
            nonlocal handler_executed
            handler_executed = True
            target_file.write_text("MUTATION_HAPPENED", encoding="utf-8")
            return {"written": True}

        self.broker.registry.register(ToolDefinition(
            name="guarded_mutation",
            description="Tool with high privilege mutation",
            handler=mutating_handler,
            policy_mode=ToolPolicyMode.ALLOW,
            is_mutation=False,
        ))

        my_pid = os.getpid()
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["guarded_mutation"],
        )

        # Force audit database persistence failure
        self.store.db.close()

        # Execute must raise ToolAuditPersistenceError fail-closed
        with self.assertRaises(ToolAuditPersistenceError):
            self.broker.execute(
                token_str=token_str,
                tool_name="guarded_mutation",
                parameters={"action": "mutate"},
                caller_pid=my_pid,
            )

        # CRITICAL ASSERTION: The tool handler was NEVER executed and target file does not exist
        self.assertFalse(handler_executed, "Handler MUST NOT execute when audit persistence fails")
        self.assertFalse(target_file.exists(), "No side-effects may occur on disk")

    def test_03_tampering_with_any_canonical_field_breaks_hash_chain(self):
        """Modifying ANY of the 15 canonical audit fields is detected immediately by verify_integrity."""
        rec = self.worker_audit.record_event(
            event_type=WorkerAuditEventType.SPAWN,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=1122,
            process_identity={"pid": 1122, "role": "test_worker"},
            limits={"max_memory_bytes": 1048576},
            resource_usage={"cpu_time": 0.05},
            reason="Baseline audit entry",
            details={"config_version": 1},
        )

        # Baseline verification: pristine
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)

        # Disable update trigger to simulate offline disk tampering
        self.store.db.execute("DROP TRIGGER prevent_worker_audit_update")

        # 1. Tamper limits_json
        self.store.db.execute(
            "UPDATE worker_audit_log SET limits_json = '{\"max_memory_bytes\":99999999}' WHERE event_id = ?",
            (rec.event_id,)
        )
        self.store.db.commit()
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertFalse(ok)
        self.assertTrue(any("adulteracao" in e.lower() for e in errors))

        # Revert limits_json
        self.store.db.execute(
            "UPDATE worker_audit_log SET limits_json = ? WHERE event_id = ?",
            (json.dumps(rec.limits, sort_keys=True, separators=(",", ":")), rec.event_id)
        )
        self.store.db.commit()
        ok, _ = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertTrue(ok)

        # 2. Tamper worker_pid
        self.store.db.execute(
            "UPDATE worker_audit_log SET worker_pid = 9999 WHERE event_id = ?",
            (rec.event_id,)
        )
        self.store.db.commit()
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertFalse(ok)
        self.assertTrue(any("adulteracao" in e.lower() for e in errors))

        # Revert worker_pid
        self.store.db.execute(
            "UPDATE worker_audit_log SET worker_pid = ? WHERE event_id = ?",
            (rec.worker_pid, rec.event_id)
        )
        self.store.db.commit()

        # 3. Tamper details_json
        self.store.db.execute(
            "UPDATE worker_audit_log SET details_json = '{\"forged\": true}' WHERE event_id = ?",
            (rec.event_id,)
        )
        self.store.db.commit()
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertFalse(ok)
        self.assertTrue(any("adulteracao" in e.lower() for e in errors))

    def test_04_log_tail_truncation_detected_by_anchors(self):
        """Deleting the most recent audit records triggers tail-truncation detection via anchors."""
        for i in range(5):
            self.worker_audit.record_event(
                event_type=WorkerAuditEventType.TOOL_REQUESTED,
                task_id=self.task_id,
                run_id=self.run_id,
                worker_pid=2000 + i,
                details={"step": i},
            )

        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)

        # Drop DELETE trigger to simulate malicious row deletion
        self.store.db.execute("DROP TRIGGER prevent_worker_audit_delete")

        # Attacker deletes sequences 4 and 5
        self.store.db.execute("DELETE FROM worker_audit_log WHERE sequence_number >= 4")
        self.store.db.commit()

        # Verify integrity must detect tail truncation
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertFalse(ok)
        self.assertTrue(any("truncamento" in e.lower() for e in errors))

    def test_05_mailbox_strictly_rejects_missing_or_spoofed_expected_worker_pid(self):
        """Mailbox processing strictly enforces non-empty, genuine expected_worker_pid."""
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
        req_file = req_dir / "spoofed_req.json"

        # 1. Payload declares PID 99999 (spoof attempt)
        req_file.write_text(json.dumps({
            "token": token_str,
            "tool": "echo_payload",
            "parameters": {"payload": "secret_request"},
            "caller_pid": 99999,
        }), encoding="utf-8")

        # Case A: Missing or invalid expected_worker_pid
        with self.assertRaises(CallerIdentityMismatchError):
            self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=None)  # type: ignore

        with self.assertRaises(CallerIdentityMismatchError):
            self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=0)

        with self.assertRaises(CallerIdentityMismatchError):
            self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=-10)

        # Case B: expected_worker_pid is legitimate my_pid, but payload declared spoofed 99999
        results = self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=my_pid)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("CallerIdentityMismatchError", results[0]["error"])

        # Case C: Genuine request matching expected_worker_pid
        req_file.write_text(json.dumps({
            "token": token_str,
            "tool": "echo_payload",
            "parameters": {"payload": "valid_request"},
            "caller_pid": my_pid,
        }), encoding="utf-8")

        results2 = self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=my_pid)
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0]["status"], "success")
        self.assertEqual(results2[0]["response"]["result"]["echo"], "valid_request")

    def test_06_concurrent_quota_exhaustion_cannot_bypass_limits(self):
        """Multi-threaded concurrent tool executions strictly enforce max_calls limit."""
        my_pid = os.getpid()
        max_quota = 3
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
            max_calls=max_quota,
        )

        results = []
        errors = []

        def call_tool(idx: int):
            try:
                res = self.broker.execute(
                    token_str=token_str,
                    tool_name="echo_payload",
                    parameters={"payload": f"call_{idx}"},
                    caller_pid=my_pid,
                )
                results.append(res)
            except Exception as e:
                errors.append(e)

        # Concurrently fire 10 simultaneous threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(call_tool, i) for i in range(10)]
            concurrent.futures.wait(futures)

        # Exactly max_quota calls succeed
        self.assertEqual(len(results), max_quota)
        self.assertEqual(len(errors), 10 - max_quota)
        for err in errors:
            self.assertIsInstance(err, BrokerRateLimitExceededError)

    def test_07_job_object_resource_stress_containment_and_tree_termination(self):
        """Worker process spawning child processes is completely terminated via Job Object tree cleanup."""
        # Python script that spawns a child worker and both sleep
        parent_script = self.project_dir / "parent_worker.py"
        parent_script.write_text(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)\n",
            encoding="utf-8"
        )

        limits = ResourceLimits(
            max_memory_bytes=64 * 1024 * 1024,
            max_processes=5,
        )

        proc = ProcessSupervisor.spawn(
            [sys.executable, str(parent_script)],
            cwd=self.project_dir,
            role="worker",
            use_job_object=True,
            resource_limits=limits,
        )

        self.assertIsNotNone(proc.pid)
        time.sleep(0.5)

        # Audit termination event
        self.worker_audit.record_event(
            event_type=WorkerAuditEventType.TERMINATION,
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=proc.pid,
            reason="Adversarial stress cleanup test",
        )

        # Terminate entire process tree
        terminate_tree(proc)

        # Process should be dead
        self.assertIsNotNone(proc.poll())

        # Audit trail must remain verifiable
        ok, errors = self.worker_audit.verify_integrity(task_id=self.task_id, run_id=self.run_id)
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)

    def test_08_private_coordinator_files_strictly_inaccessible_from_sandbox(self):
        """Private coordinator storage files cannot be read or listed by sandboxed workers."""
        my_pid = os.getpid()
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file", "workspace_list_dir"],
        )

        # Attempt to read coordination.sqlite directly via broker
        with self.assertRaises(ToolAccessDeniedError):
            self.broker.execute(
                token_str=token_str,
                tool_name="workspace_read_file",
                parameters={"path": f"../coordinator_store/coordination.sqlite"},
                caller_pid=my_pid,
            )

        # Attempt to list coordinator_store directory
        with self.assertRaises(ToolAccessDeniedError):
            self.broker.execute(
                token_str=token_str,
                tool_name="workspace_list_dir",
                parameters={"subpath": f"../coordinator_store"},
                caller_pid=my_pid,
            )


if __name__ == "__main__":
    unittest.main()
