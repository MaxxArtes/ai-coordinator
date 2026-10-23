"""Tests for Phase 4B.5.1 - Process Sandbox, Containment, and OS-Level Worker Governance."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_coordinator.process import (
    ProcessSupervisor,
    SupervisedProcess,
    WindowsProcessController,
    PosixProcessController,
    ResourceLimits,
    SENSITIVE_COORDINATOR_VARS,
)
from ai_coordinator.human_workspace import HumanWorkspaceAccessDenied
from ai_coordinator.runtime import worker_environment, worker_environment_policy


class ProcessSandboxContainmentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.sandbox_root = Path(self.temp_dir.name).resolve() / "project_root"
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = [self.sandbox_root]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_01_worker_env_strips_sensitive_supervisor_credentials(self):
        # Inject sensitive coordinator and supervisor secrets into parent environment
        secret_env = {
            "AI_COORDINATOR_SUPERVISOR_KEY": "super_secret_key_12345",
            "AI_COORDINATOR_SUPERVISOR_SECRET": "super_secret_hmac_67890",
            "AI_MEMORY_API_TOKEN": "aim_token_secret_abcdef",
            "CUSTOM_VAR": "legitimate_worker_value",
        }
        cmd = [
            sys.executable,
            "-c",
            "import os, json; print(json.dumps({k: v for k, v in os.environ.items()}))",
        ]

        with patch.dict(os.environ, secret_env):
            # Spawn worker process with ProcessSupervisor.spawn
            proc = ProcessSupervisor.spawn(
                cmd,
                cwd=self.sandbox_root,
                role="worker",
                stdout=subprocess.PIPE,
                allowed_roots=self.allowed_roots,
            )
            out, _ = proc.communicate(timeout=10)
            self.assertEqual(proc.poll(), 0)

            child_env = json.loads(out.strip())

            # Verify that sensitive variables were purged fail-closed
            self.assertNotIn("AI_COORDINATOR_SUPERVISOR_KEY", child_env)
            self.assertNotIn("AI_COORDINATOR_SUPERVISOR_SECRET", child_env)
            self.assertNotIn("AI_MEMORY_API_TOKEN", child_env)
            # Role must be enforced as worker
            self.assertEqual(child_env.get("AI_COORDINATOR_ROLE"), "worker")

    def test_02_windows_job_object_containment_before_execution(self):
        if os.name != "nt":
            self.skipTest("Teste aplicavel exclusivamente ao Windows NT")

        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
        proc = ProcessSupervisor.spawn(
            cmd,
            cwd=self.sandbox_root,
            role="worker",
            stdout=subprocess.PIPE,
            use_job_object=True,
            allowed_roots=self.allowed_roots,
        )
        self.assertIsNotNone(proc.job_handle)
        code = proc.wait(timeout=10)
        self.assertEqual(code, 0)

    def test_03_fail_closed_when_job_object_or_sandbox_fails(self):
        if os.name != "nt":
            self.skipTest("Teste aplicavel exclusivamente ao Windows NT")

        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
        # Simulate Job Object creation failure
        with patch.object(WindowsProcessController, "create_job_object", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                ProcessSupervisor.spawn(
                    cmd,
                    cwd=self.sandbox_root,
                    role="worker",
                    use_job_object=True,
                    allowed_roots=self.allowed_roots,
                )
            self.assertIn("falha na ativacao da sandbox", str(ctx.exception).lower())

    def test_04_descendant_processes_contained_and_killed_on_close(self):
        # Worker spawns a child process (grandchild of supervisor)
        grandchild_script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        cmd = [sys.executable, "-u", "-c", grandchild_script]

        proc = ProcessSupervisor.spawn(
            cmd,
            cwd=self.sandbox_root,
            role="worker",
            stdout=subprocess.PIPE,
            use_job_object=True,
            allowed_roots=self.allowed_roots,
        )

        grandchild_pid_line = proc.stdout.readline()
        self.assertTrue(bool(grandchild_pid_line.strip()))
        grandchild_pid = int(grandchild_pid_line.strip())

        # Terminate supervised worker
        res = proc.terminate(timeout_seconds=5.0)
        self.assertTrue(res.success)

        # Allow OS kernel to reap descendant process tree
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            is_alive = (
                WindowsProcessController.is_running(grandchild_pid)
                if os.name == "nt"
                else PosixProcessController.is_running(grandchild_pid)
            )
            if not is_alive:
                break
            time.sleep(0.05)

        # Confirm grandchild is dead
        if os.name == "nt":
            self.assertFalse(WindowsProcessController.is_running(grandchild_pid))
        else:
            self.assertFalse(PosixProcessController.is_running(grandchild_pid))

    def test_05_working_directory_canonicalization_and_restriction(self):
        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]

        # 1. Non-existent working directory raises ValueError
        invalid_dir = self.sandbox_root / "does_not_exist_subfolder"
        with self.assertRaises(ValueError):
            ProcessSupervisor.spawn(cmd, cwd=invalid_dir, role="worker")

        # 2. Directory outside allowed roots raises HumanWorkspaceAccessDenied
        outside_dir = Path(self.temp_dir.name).resolve() / "outside_unauthorized"
        outside_dir.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx:
            ProcessSupervisor.spawn(
                cmd,
                cwd=outside_dir,
                role="worker",
                allowed_roots=self.allowed_roots,
            )
        self.assertIn("fora das raizes autorizadas", str(ctx.exception).lower())

        # 3. Authorized directory runs cleanly
        proc = ProcessSupervisor.spawn(
            cmd,
            cwd=self.sandbox_root,
            role="worker",
            allowed_roots=self.allowed_roots,
        )
        code = proc.wait(timeout=5)
        self.assertEqual(code, 0)

    def test_06_resource_limits_configuration(self):
        limits = ResourceLimits(
            max_memory_bytes=256 * 1024 * 1024,
            max_cpu_time_seconds=15.0,
            max_processes=8,
        )
        cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
        proc = ProcessSupervisor.spawn(
            cmd,
            cwd=self.sandbox_root,
            role="worker",
            resource_limits=limits,
            allowed_roots=self.allowed_roots,
        )
        code = proc.wait(timeout=10)
        self.assertEqual(code, 0)

    def test_07_runtime_worker_environment_stripping(self):
        # Verify that runtime.worker_environment strips sensitive secrets even under mode='inherit'
        config_inherit = {
            "worker_environment": {"mode": "inherit"},
            "availability": {"opencode_cli_major": 2},
        }
        candidate = {"client": "opencode", "model": "small"}

        secret_env = {
            "AI_COORDINATOR_SUPERVISOR_KEY": "leak_key_test",
            "AI_COORDINATOR_SUPERVISOR_SECRET": "leak_secret_test",
            "AI_MEMORY_API_TOKEN": "leak_aim_token",
            "SAFE_HOST_VAR": "host_value",
        }
        with patch.dict(os.environ, secret_env):
            env, mode = worker_environment(config_inherit, candidate, task_id="task_test_123")
            self.assertEqual(mode, "inherit")
            self.assertNotIn("AI_COORDINATOR_SUPERVISOR_KEY", env)
            self.assertNotIn("AI_COORDINATOR_SUPERVISOR_SECRET", env)
            self.assertNotIn("AI_MEMORY_API_TOKEN", env)
            self.assertEqual(env["AI_COORDINATOR_ROLE"], "worker")
            self.assertEqual(env["AI_COORDINATOR_TASK_ID"], "task_test_123")


if __name__ == "__main__":
    unittest.main()
