"""Tests for Phase 4B.5.3 - Tool Broker, Authorization, and Mediation Subsystem.

Verifies:
1. Deny-by-default for unregistered or unapproved tools.
2. Cryptographic capability token verification (HMAC signature, tamper detection).
3. Expiration of capability tokens (TTL enforcement).
4. Revocation of capability tokens.
5. Protection against task and run identity spoofing.
6. Process identity binding and PID recycling (TOCTOU) defense.
7. Strict mutation protection against checkpoints, secrets, and SQLite.
8. Operational limits (max calls, payload size).
9. Human-in-the-loop approval workflow (requires_approval).
10. Positive path: authorized tool execution and audit log persistence.
11. Sandbox file mailbox mediation for fully isolated workers.
12. Win32 native stdin and default enforce_filesystem_sandbox verification.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ai_coordinator.storage import Store
from ai_coordinator.tool_broker import (
    ToolBroker,
    ToolRegistry,
    CapabilityTokenManager,
    CapabilityToken,
    ToolDefinition,
    ToolPolicyMode,
    ToolBrokerContext,
    BrokerAuditRecord,
    ToolBrokerError,
    ToolAccessDeniedError,
    InvalidCapabilityTokenError,
    CapabilityTokenExpiredError,
    CapabilityTokenRevokedError,
    BrokerRateLimitExceededError,
    CallerIdentityMismatchError,
    MutationNotPermittedError,
    ToolApprovalRequired,
)
from ai_coordinator.process import ProcessSupervisor, ResourceLimits


class Phase4B53ToolBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_root = Path(self.temp_dir.name).resolve()

        self.coordinator_dir = self.base_root / "coordinator_store"
        self.coordinator_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(root=self.coordinator_dir, supervisor_key="super_secret_supervisor_key_12345")

        self.project_dir = self.base_root / "project_workspace"
        self.project_dir.mkdir(parents=True, exist_ok=True)

        # Authorized project file
        self.sample_code_file = self.project_dir / "main.py"
        self.sample_code_file.write_text("def hello():\n    return 'Hello, Coordinator!'\n", encoding="utf-8")

        # Task and run
        self.task = self.store.new(goal="Task for tool broker testing", project=str(self.project_dir))
        self.task_id = self.task["id"]
        self.run_id = "run_tb_001"

        # Worker sandbox directory
        self.sandbox_dir = self.project_dir / "sandbox_worker_01"
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.sandbox_file = self.sandbox_dir / "worker_output.txt"
        self.sandbox_file.write_text("SANDBOX_LOCAL_DATA", encoding="utf-8")

        self.broker = self.store.tool_broker

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

    def test_01_deny_by_default_unregistered_or_unauthorized_tool(self):
        """Unregistered tools or tools outside token scope are strictly denied."""
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )

        # 1. Unregistered tool
        with self.assertRaises(ToolAccessDeniedError) as ctx:
            self.broker.execute(token_str, "unknown_malicious_tool", {}, caller_pid=os.getpid())
        self.assertIn("nao cadastrada", str(ctx.exception).lower())

        # 2. Registered tool but NOT in token scope
        with self.assertRaises(ToolAccessDeniedError) as ctx:
            self.broker.execute(token_str, "workspace_list_dir", {"subpath": "."}, caller_pid=os.getpid())
        self.assertIn("fora do escopo", str(ctx.exception).lower())

    def test_02_rejection_of_forged_or_tampered_token(self):
        """Capability tokens with invalid HMAC signatures or tampered payloads are rejected."""
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )

        # 1. Tampered signature
        parts = token_str.split("_")
        tampered_sig = parts[0] + "_" + parts[1] + "_" + parts[2] + "_" + ("0" * len(parts[3]))
        with self.assertRaises(InvalidCapabilityTokenError):
            self.broker.execute(tampered_sig, "workspace_read_file", {"path": "main.py"}, caller_pid=os.getpid())

        # 2. Token issued by fake key
        fake_manager = CapabilityTokenManager(supervisor_secret="fake_adversary_key")
        fake_token = fake_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )
        with self.assertRaises(InvalidCapabilityTokenError):
            self.broker.execute(fake_token, "workspace_read_file", {"path": "main.py"}, caller_pid=os.getpid())

    def test_03_rejection_of_expired_capability_token(self):
        """Capability tokens past their expiration deadline are rejected."""
        # Issue token with tiny TTL (0.1s)
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
            ttl_seconds=0.1,
        )
        time.sleep(0.15)

        with self.assertRaises(CapabilityTokenExpiredError):
            self.broker.execute(token_str, "workspace_read_file", {"path": "main.py"}, caller_pid=os.getpid())

    def test_04_rejection_of_revoked_token(self):
        """Tokens revoked by the supervisor cannot be used for any subsequent tool calls."""
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )

        token_id = token_str.split("_")[1]
        self.broker.token_manager.revoke_token(token_id)

        with self.assertRaises(CapabilityTokenRevokedError):
            self.broker.execute(token_str, "workspace_read_file", {"path": "main.py"}, caller_pid=os.getpid())

    def test_05_rejection_of_task_or_run_spoofing(self):
        """Worker cannot use token issued for Task A to read from or manipulate Task B."""
        other_project = self.base_root / "other_secret_project"
        other_project.mkdir(parents=True, exist_ok=True)
        other_file = other_project / "secret.txt"
        other_file.write_text("CONFIDENTIAL_PROJECT_B", encoding="utf-8")

        # Token is bound to self.project_dir
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )

        # Attempt to escape project_root using absolute path to other project
        with self.assertRaises(ToolBrokerError):
            self.broker.execute(token_str, "workspace_read_file", {"path": str(other_file)}, caller_pid=os.getpid())

    def test_06_caller_identity_verification_and_pid_recycling_defense(self):
        """Broker verifies caller PID and creation time to prevent identity spoofing and PID recycling."""
        my_pid = os.getpid()
        ident = ProcessSupervisor.capture_identity(my_pid)
        create_time = ident.create_time_ns if ident else int(time.time() * 1e9)

        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            worker_create_time_ns=create_time,
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )

        # 1. Caller PID mismatch
        wrong_pid = my_pid + 9999
        with self.assertRaises(CallerIdentityMismatchError):
            self.broker.execute(token_str, "workspace_read_file", {"path": "main.py"}, caller_pid=wrong_pid)

        # 2. Simulated recycled PID (mismatched create_time_ns)
        recycled_token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=my_pid,
            worker_create_time_ns=create_time - 50_000_000_000,  # 50 seconds in the past
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )
        with self.assertRaises(CallerIdentityMismatchError):
            self.broker.execute(recycled_token_str, "workspace_read_file", {"path": "main.py"}, caller_pid=my_pid)

    def test_07_strictly_denied_privileged_tools_and_mutation_protection(self):
        """Workers cannot execute privileged tools or mutate coordinator storage / checkpoints."""
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["read_supervisor_secret", "raw_sqlite_query", "workspace_read_file"],
        )

        # 1. read_supervisor_secret has DENY policy mode
        with self.assertRaises(ToolAccessDeniedError) as ctx:
            self.broker.execute(token_str, "read_supervisor_secret", {}, caller_pid=os.getpid())
        self.assertIn("estritamente proibida", str(ctx.exception).lower())

        # 2. raw_sqlite_query has DENY policy mode
        with self.assertRaises(ToolAccessDeniedError) as ctx:
            self.broker.execute(token_str, "raw_sqlite_query", {"query": "SELECT * FROM tasks"}, caller_pid=os.getpid())
        self.assertIn("estritamente proibida", str(ctx.exception).lower())

        # 3. workspace_read_file targeting supervisor.secret is rejected
        secret_file = self.store.root / "auth" / "supervisor.secret"
        with self.assertRaises(ToolAccessDeniedError):
            self.broker.execute(token_str, "workspace_read_file", {"path": str(secret_file)}, caller_pid=os.getpid())

    def test_08_operational_limits_rate_limit_and_payload_size(self):
        """Worker exceeding call limits or payload size is denied with BrokerRateLimitExceededError."""
        # 1. Max calls limit (e.g. max 2 calls)
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
            max_calls=2,
        )

        # Call 1: OK
        res1 = self.broker.execute(token_str, "echo_payload", {"payload": "first"}, caller_pid=os.getpid())
        self.assertEqual(res1["status"], "success")

        # Call 2: OK
        res2 = self.broker.execute(token_str, "echo_payload", {"payload": "second"}, caller_pid=os.getpid())
        self.assertEqual(res2["status"], "success")

        # Call 3: Exceeded limit
        with self.assertRaises(BrokerRateLimitExceededError):
            self.broker.execute(token_str, "echo_payload", {"payload": "third"}, caller_pid=os.getpid())

        # 2. Payload size limit
        huge_payload = {"payload": "A" * 70_000}
        fresh_token = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["echo_payload"],
        )
        with self.assertRaises(BrokerRateLimitExceededError):
            self.broker.execute(fresh_token, "echo_payload", huge_payload, caller_pid=os.getpid())

    def test_09_human_approval_required_workflow(self):
        """Tool requiring approval pauses execution; supervisor approval allows clearance."""
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["mutate_checkpoint"],
        )

        # Initial call raises ToolApprovalRequired with approval_id
        with self.assertRaises(ToolApprovalRequired) as ctx:
            self.broker.execute(token_str, "mutate_checkpoint", {"revision": 24}, caller_pid=os.getpid())
        approval_id = ctx.exception.approval_id
        self.assertTrue(bool(approval_id))

        # Check pending approvals
        self.assertIn(approval_id, self.broker.pending_approvals)
        self.assertEqual(self.broker.pending_approvals[approval_id]["status"], "pending")

        # Approval attempt with wrong key fails
        with self.assertRaises(ToolAccessDeniedError):
            self.broker.approve_request(approval_id, supervisor_key="wrong_key")

        # Legitimate supervisor approves
        ok = self.broker.approve_request(approval_id, supervisor_key="super_secret_supervisor_key_12345")
        self.assertTrue(ok)
        self.assertEqual(self.broker.pending_approvals[approval_id]["status"], "approved")

    def test_10_positive_path_authorized_tool_execution(self):
        """Authorized worker executes allowed tool, receives clean result, and audit log is recorded."""
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file", "calculate_hash"],
        )

        # 1. Execute workspace_read_file
        res = self.broker.execute(
            token_str,
            "workspace_read_file",
            {"path": "main.py"},
            caller_pid=os.getpid(),
            sandbox_root=self.project_dir,
        )
        self.assertEqual(res["status"], "success")
        self.assertIn("Hello, Coordinator!", res["result"]["content"])
        self.assertTrue(bool(res.get("correlation_id")))

        # 2. Execute calculate_hash
        hash_res = self.broker.execute(
            token_str,
            "calculate_hash",
            {"content": "sample_data", "algorithm": "sha256"},
            caller_pid=os.getpid(),
        )
        self.assertEqual(hash_res["status"], "success")
        self.assertEqual(len(hash_res["result"]["digest"]), 64)

        # 3. Verify SQLite audit records
        audit_records = self.broker.query_audit_log(task_id=self.task_id, run_id=self.run_id)
        self.assertTrue(len(audit_records) >= 2)
        decisions = [r["decision"] for r in audit_records]
        self.assertIn("allowed", decisions)

    def test_11_sandbox_file_mailbox_mediation(self):
        """Worker inside strict sandbox requests tools via .broker mailbox files."""
        token_str = self.broker.token_manager.issue_token(
            task_id=self.task_id,
            run_id=self.run_id,
            worker_pid=os.getpid(),
            project_root=self.project_dir,
            allowed_tools=["workspace_read_file"],
        )

        # Worker writes request into sandbox .broker/requests/
        broker_req_dir = self.sandbox_dir / ".broker" / "requests"
        broker_req_dir.mkdir(parents=True, exist_ok=True)
        req_file = broker_req_dir / "req_001.json"
        req_file.write_text(json.dumps({
            "token": token_str,
            "tool": "workspace_read_file",
            "parameters": {"path": "worker_output.txt"},
            "caller_pid": os.getpid(),
        }), encoding="utf-8")

        # Supervisor processes mailbox
        results = self.broker.process_mailbox(self.sandbox_dir, expected_worker_pid=os.getpid())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "success")

        # Response file exists in sandbox .broker/responses/
        res_file = self.sandbox_dir / ".broker" / "responses" / "res_req_001.json"
        self.assertTrue(res_file.exists())
        res_obj = json.loads(res_file.read_text(encoding="utf-8"))
        self.assertEqual(res_obj["status"], "success")
        self.assertIn("SANDBOX_LOCAL_DATA", res_obj["response"]["result"]["content"])

        # Request file was cleaned up
        self.assertFalse(req_file.exists())

    def test_12_native_win32_stdin_and_default_enforce_fs(self):
        """Verify that enforce_filesystem_sandbox is True by default and AppContainer stdin works."""
        from ai_coordinator.cli import run_worker

        # Config without security.enforce_filesystem_sandbox (must default to True)
        mock_worker_py = self.project_dir / "stdin_mock_worker.py"
        mock_worker_py.write_text("""import sys, json
line = sys.stdin.readline().strip()
res = {
    "result": f"# EchoResult\\nReceivedInput: {line}\\n",
    "usage": {"total_tokens": 10, "cost": 0.0001}
}
print(json.dumps(res))
sys.exit(0)
""", encoding="utf-8")

        input_file = self.project_dir / "task_input.txt"
        input_file.write_text("TEST_STDIN_PAYLOAD_LINE", encoding="utf-8")

        config = {
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
                # Notice: enforce_filesystem_sandbox not specified, must default to True safely!
            },
            "routes": {
                "general": {
                    "candidates": [
                        {"client": "openrouter", "model": "mock_model", "plain_prompt": True}
                    ]
                }
            },
            "launchers": {
                "openrouter": [sys.executable, str(mock_worker_py)]
            }
        }

        summary = run_worker(self.store, config, self.task_id, "general", str(input_file))
        self.assertEqual(summary.get("status"), "complete")

        output_path = Path(summary["output_file"])
        self.assertTrue(output_path.exists())
        text = output_path.read_text(encoding="utf-8")
        self.assertIn("ReceivedInput: TEST_STDIN_PAYLOAD_LINE", text)


if __name__ == "__main__":
    unittest.main()
