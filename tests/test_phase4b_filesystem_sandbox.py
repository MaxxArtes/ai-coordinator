"""Tests for Phase 4B.5.2 - Filesystem Sandbox, Access Control, and Operating System-Level Boundary Enforcement.

Verifies strict OS-level filesystem isolation for worker execution:
1. Worker cannot open supervisor.secret (denied by OS).
2. Worker cannot open coordination.sqlite (denied by OS).
3. Worker cannot read WAL/SHM files and backups (denied by OS).
4. Worker cannot write to sibling tasks (denied by OS).
5. Path traversal using ../ and absolute paths is strictly rejected (PathTraversalError).
6. Symlink and junction escape attempts are strictly rejected (ReparseEscapeError).
7. Worker cannot access supervisor credentials in user profile (denied by OS).
8. Supervisor safely imports authorized output from worker sandbox.
9. Execution fails closed if mandatory OS sandbox boundary cannot be established.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ai_coordinator.fs_sandbox import (
    ExecutionSandbox,
    FileSandboxConfig,
    FileSandboxSecurityError,
    PathTraversalError,
    ReparseEscapeError,
    WindowsAppContainerController,
    LinuxLandlockController,
    MacOsSeatbeltController,
    validate_contained_path,
    is_reparse_point_or_symlink,
)
from ai_coordinator.process import ProcessSupervisor, ResourceLimits


class Phase4B52FilesystemSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_root = Path(self.temp_dir.name).resolve()

        # 1. Privileged Coordinator Private Directory
        self.coordinator_dir = self.base_root / "coordinator_private"
        self.coordinator_dir.mkdir(parents=True, exist_ok=True)
        self.auth_dir = self.coordinator_dir / "auth"
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir = self.coordinator_dir / "backups"
        self.backups_dir.mkdir(parents=True, exist_ok=True)

        self.sqlite_file = self.coordinator_dir / "coordination.sqlite"
        self.sqlite_file.write_text("SQLITE_DATABASE_PRIVATE_HEADER", encoding="utf-8")
        self.wal_file = self.coordinator_dir / "coordination.sqlite-wal"
        self.wal_file.write_text("SQLITE_WAL_PRIVATE_DATA", encoding="utf-8")
        self.shm_file = self.coordinator_dir / "coordination.sqlite-shm"
        self.shm_file.write_text("SQLITE_SHM_PRIVATE_DATA", encoding="utf-8")
        self.secret_file = self.auth_dir / "supervisor.secret"
        self.secret_file.write_text("SUPERVISOR_SECRET_KEY_HEX_12345", encoding="utf-8")
        self.key_file = self.auth_dir / "supervisor.key"
        self.key_file.write_text("SUPERVISOR_PUBLIC_KEY_HEX_67890", encoding="utf-8")
        self.backup_file = self.backups_dir / "coordination_backup_001.bak"
        self.backup_file.write_text("COORDINATION_BACKUP_CONTENT", encoding="utf-8")

        # 2. Sibling Task Directory
        self.sibling_task_dir = self.base_root / "tasks" / "sibling_task_002"
        self.sibling_task_dir.mkdir(parents=True, exist_ok=True)
        self.sibling_file = self.sibling_task_dir / "sibling_state.json"
        self.sibling_file.write_text('{"task": "sibling", "state": "private"}', encoding="utf-8")

        # 3. Worker Execution Sandbox Directory
        self.sandbox_dir = self.base_root / "tasks" / "target_task_001" / "runs" / "run_001"
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)

        # 4. Allowed read roots (Python runtime)
        self.read_roots = [Path(sys.executable).parent]

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _run_worker_script(self, script_body: str, enforce_os_boundary: bool = False) -> tuple[int, str, str]:
        """Helper to run a real Python worker process inside ExecutionSandbox."""
        config = FileSandboxConfig(
            sandbox_id="test_run_001",
            sandbox_root=self.sandbox_dir,
            coordinator_private_dir=self.coordinator_dir,
            allowed_read_roots=self.read_roots,
            enforce_os_boundary=enforce_os_boundary,
            clean_on_exit=False,
        )
        sandbox = ExecutionSandbox(config)
        try:
            sandbox.prepare()
        except FileSandboxSecurityError:
            if enforce_os_boundary:
                raise
            # If not enforcing, proceed

        worker_script_path = self.sandbox_dir / "worker_test.py"
        worker_script_path.write_text(script_body, encoding="utf-8")

        cmd = [sys.executable, "-u", str(worker_script_path)]
        limits = ResourceLimits(max_memory_bytes=256 * 1024 * 1024, max_cpu_time_seconds=15.0)

        proc = ProcessSupervisor.spawn(
            cmd,
            cwd=self.sandbox_dir,
            role="worker",
            use_job_object=True,
            resource_limits=limits,
            fs_sandbox=sandbox if sandbox.boundary_status.startswith("enforced") else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            stdout, stderr = proc.communicate(timeout=10.0)
            return proc.returncode or 0, stdout, stderr
        finally:
            proc.close()
            sandbox.cleanup()

    def _is_os_boundary_enforced(self) -> bool:
        """Check if an OS-level mandatory boundary is active on the current host."""
        return (
            os.name == "nt"
            or (sys.platform.startswith("linux") and LinuxLandlockController.is_available())
            or (sys.platform == "darwin" and MacOsSeatbeltController.is_available())
        )

    def test_01_worker_cannot_open_supervisor_secret(self):
        """Worker running in sandbox must be denied access by OS when attempting to read supervisor.secret."""
        probe_file = self.sandbox_dir / "allowed_probe.txt"
        probe_file.write_text("PROBE_AUTHORIZED_PAYLOAD", encoding="utf-8")
        probe_status = self.sandbox_dir / "probe_status_01.txt"

        script = f"""import sys, pathlib
# 1. Positive proof: worker operates legitimately within sandbox
try:
    probe = pathlib.Path("allowed_probe.txt").read_text(encoding="utf-8")
    if probe != "PROBE_AUTHORIZED_PAYLOAD":
        sys.exit(98)
    pathlib.Path("probe_status_01.txt").write_text("PROBE_CONFIRMED", encoding="utf-8")
except Exception:
    sys.exit(99)

# 2. Adversarial attempt: read coordinator secret
try:
    data = pathlib.Path(r"{self.secret_file}").read_text(encoding="utf-8")
    print(f"LEAKED: {{data}}")
    sys.exit(0)
except (PermissionError, OSError) as e:
    print(f"ACCESS_DENIED: {{type(e).__name__}}")
    sys.exit(42)
"""
        code, stdout, stderr = self._run_worker_script(script)
        # Check positive proof first: worker was alive and functioning within sandbox
        self.assertTrue(probe_status.exists(), f"Positive proof failed (code={code}, stdout={stdout}, stderr={stderr}): worker did not execute inside sandbox")
        self.assertEqual(probe_status.read_text(encoding="utf-8"), "PROBE_CONFIRMED")

        if self._is_os_boundary_enforced():
            self.assertIn(code, (42, -6))
            if code == 42:
                self.assertIn("ACCESS_DENIED", stdout)
            self.assertNotIn("LEAKED", stdout)

    def test_02_worker_cannot_open_coordination_sqlite(self):
        """Worker running in sandbox must be denied access by OS when opening coordination.sqlite."""
        probe_file = self.sandbox_dir / "allowed_probe.txt"
        probe_file.write_text("PROBE_AUTHORIZED_PAYLOAD", encoding="utf-8")
        probe_status = self.sandbox_dir / "probe_status_02.txt"

        script = f"""import sys, pathlib
# 1. Positive proof: worker operates legitimately within sandbox
try:
    probe = pathlib.Path("allowed_probe.txt").read_text(encoding="utf-8")
    if probe != "PROBE_AUTHORIZED_PAYLOAD":
        sys.exit(98)
    pathlib.Path("probe_status_02.txt").write_text("PROBE_CONFIRMED", encoding="utf-8")
except Exception:
    sys.exit(99)

# 2. Adversarial attempt: read SQLite file
try:
    data = pathlib.Path(r"{self.sqlite_file}").read_bytes()
    print("LEAKED_SQLITE")
    sys.exit(0)
except (PermissionError, OSError) as e:
    print(f"SQLITE_DENIED: {{type(e).__name__}}")
    sys.exit(42)
"""
        code, stdout, stderr = self._run_worker_script(script)
        self.assertTrue(probe_status.exists(), f"Positive proof failed (code={code}, stdout={stdout}, stderr={stderr}): worker did not execute inside sandbox")
        self.assertEqual(probe_status.read_text(encoding="utf-8"), "PROBE_CONFIRMED")

        if self._is_os_boundary_enforced():
            self.assertIn(code, (42, -6))
            if code == 42:
                self.assertIn("SQLITE_DENIED", stdout)
            self.assertNotIn("LEAKED_SQLITE", stdout)

    def test_03_worker_cannot_read_wal_shm_and_backups(self):
        """Worker running in sandbox must be denied access to WAL, SHM, and coordinator backup files."""
        probe_file = self.sandbox_dir / "allowed_probe.txt"
        probe_file.write_text("PROBE_AUTHORIZED_PAYLOAD", encoding="utf-8")
        probe_status = self.sandbox_dir / "probe_status_03.txt"

        script = f"""import sys, pathlib
# 1. Positive proof: worker operates legitimately within sandbox
try:
    probe = pathlib.Path("allowed_probe.txt").read_text(encoding="utf-8")
    if probe != "PROBE_AUTHORIZED_PAYLOAD":
        sys.exit(98)
    pathlib.Path("probe_status_03.txt").write_text("PROBE_CONFIRMED", encoding="utf-8")
except Exception:
    sys.exit(99)

# 2. Adversarial attempt: read WAL, SHM, backups
denied_count = 0
for target in [r"{self.wal_file}", r"{self.shm_file}", r"{self.backup_file}"]:
    try:
        pathlib.Path(target).read_bytes()
    except (PermissionError, OSError):
        denied_count += 1
print(f"DENIED_COUNT: {{denied_count}}")
sys.exit(42 if denied_count == 3 else 0)
"""
        code, stdout, stderr = self._run_worker_script(script)
        self.assertTrue(probe_status.exists(), f"Positive proof failed (code={code}, stdout={stdout}, stderr={stderr}): worker did not execute inside sandbox")
        self.assertEqual(probe_status.read_text(encoding="utf-8"), "PROBE_CONFIRMED")

        if self._is_os_boundary_enforced():
            self.assertIn(code, (42, -6))
            if code == 42:
                self.assertIn("DENIED_COUNT: 3", stdout)

    def test_04_worker_cannot_write_to_sibling_tasks(self):
        """Worker running in sandbox must be denied write access to other sibling task directories."""
        target_sibling_write = self.sibling_task_dir / "pwned.txt"
        probe_file = self.sandbox_dir / "allowed_probe.txt"
        probe_file.write_text("PROBE_AUTHORIZED_PAYLOAD", encoding="utf-8")
        probe_status = self.sandbox_dir / "probe_status_04.txt"

        script = f"""import sys, pathlib
# 1. Positive proof: worker operates legitimately within sandbox
try:
    probe = pathlib.Path("allowed_probe.txt").read_text(encoding="utf-8")
    if probe != "PROBE_AUTHORIZED_PAYLOAD":
        sys.exit(98)
    pathlib.Path("probe_status_04.txt").write_text("PROBE_CONFIRMED", encoding="utf-8")
except Exception:
    sys.exit(99)

# 2. Adversarial attempt: write to sibling task
try:
    pathlib.Path(r"{target_sibling_write}").write_text("PWNED", encoding="utf-8")
    print("LEAKED_WRITE")
    sys.exit(0)
except (PermissionError, OSError) as e:
    print(f"WRITE_DENIED: {{type(e).__name__}}")
    sys.exit(42)
"""
        code, stdout, stderr = self._run_worker_script(script)
        self.assertTrue(probe_status.exists(), f"Positive proof failed (code={code}, stdout={stdout}, stderr={stderr}): worker did not execute inside sandbox")
        self.assertEqual(probe_status.read_text(encoding="utf-8"), "PROBE_CONFIRMED")
        self.assertFalse(target_sibling_write.exists())
        if self._is_os_boundary_enforced():
            self.assertIn(code, (42, -6))
            if code == 42:
                self.assertIn("WRITE_DENIED", stdout)

    def test_05_path_traversal_with_dotdot_and_absolute_paths_rejected(self):
        """Strict path canonicalization rejects ../ traversal and paths outside sandbox root."""
        # 1. Traversal using ..
        with self.assertRaises(PathTraversalError):
            validate_contained_path("../../../coordination.sqlite", self.sandbox_dir)

        with self.assertRaises(PathTraversalError):
            validate_contained_path("subdir/../../auth/supervisor.secret", self.sandbox_dir)

        # 2. Absolute path outside sandbox root
        with self.assertRaises(PathTraversalError):
            validate_contained_path(self.secret_file, self.sandbox_dir)

        # 3. Valid contained path passes
        valid = validate_contained_path("sub/output.md", self.sandbox_dir)
        self.assertEqual(valid, self.sandbox_dir / "sub" / "output.md")

    def test_06_symlink_and_junction_escape_rejected(self):
        """Symlinks and Windows junctions targeting files outside sandbox root are detected and rejected."""
        # Create a symlink pointing outside
        symlink_target = self.sandbox_dir / "symlink_escape_link"
        try:
            os.symlink(self.secret_file, symlink_target)
            has_symlink = True
        except (OSError, NotImplementedError):
            has_symlink = False

        if has_symlink:
            self.assertTrue(is_reparse_point_or_symlink(symlink_target))
            with self.assertRaises(ReparseEscapeError):
                validate_contained_path("symlink_escape_link", self.sandbox_dir)

    def test_07_worker_cannot_access_user_home_credentials(self):
        """Worker running in sandbox must be denied access to supervisor home profile credentials."""
        # Create a mock credential file in user profile or mock home
        mock_ssh_key = self.base_root / "mock_home" / ".ssh" / "id_rsa"
        mock_ssh_key.parent.mkdir(parents=True, exist_ok=True)
        mock_ssh_key.write_text("MOCK_PRIVATE_RSA_KEY", encoding="utf-8")

        probe_file = self.sandbox_dir / "allowed_probe.txt"
        probe_file.write_text("PROBE_AUTHORIZED_PAYLOAD", encoding="utf-8")
        probe_status = self.sandbox_dir / "probe_status_07.txt"

        script = f"""import sys, pathlib
# 1. Positive proof: worker operates legitimately within sandbox
try:
    probe = pathlib.Path("allowed_probe.txt").read_text(encoding="utf-8")
    if probe != "PROBE_AUTHORIZED_PAYLOAD":
        sys.exit(98)
    pathlib.Path("probe_status_07.txt").write_text("PROBE_CONFIRMED", encoding="utf-8")
except Exception:
    sys.exit(99)

# 2. Adversarial attempt: read home credentials
try:
    data = pathlib.Path(r"{mock_ssh_key}").read_text(encoding="utf-8")
    print("LEAKED_HOME")
    sys.exit(0)
except (PermissionError, OSError) as e:
    print(f"HOME_DENIED: {{type(e).__name__}}")
    sys.exit(42)
"""
        code, stdout, stderr = self._run_worker_script(script)
        self.assertTrue(probe_status.exists(), f"Positive proof failed (code={code}, stdout={stdout}, stderr={stderr}): worker did not execute inside sandbox")
        self.assertEqual(probe_status.read_text(encoding="utf-8"), "PROBE_CONFIRMED")

        if self._is_os_boundary_enforced():
            self.assertIn(code, (42, -6))
            if code == 42:
                self.assertIn("HOME_DENIED", stdout)
            self.assertNotIn("LEAKED_HOME", stdout)

    def test_08_supervisor_safely_imports_authorized_output(self):
        """Supervisor safely imports authorized output from worker sandbox and verifies integrity."""
        config = FileSandboxConfig(
            sandbox_id="test_import_001",
            sandbox_root=self.sandbox_dir,
            coordinator_private_dir=self.coordinator_dir,
            allowed_read_roots=self.read_roots,
            clean_on_exit=False,
        )
        sandbox = ExecutionSandbox(config)
        sandbox.prepare(input_files={"task_input.txt": "AUTHORIZED_INPUT_PAYLOAD"})

        # Verify input was written into sandbox
        input_in_sandbox = self.sandbox_dir / "task_input.txt"
        self.assertTrue(input_in_sandbox.exists())
        self.assertEqual(input_in_sandbox.read_text(encoding="utf-8"), "AUTHORIZED_INPUT_PAYLOAD")

        # Worker generates legitimate output
        output_in_sandbox = self.sandbox_dir / "final-0.md"
        output_in_sandbox.write_text("# Result\nWorker completed successfully.", encoding="utf-8")

        # Supervisor safely imports output text and bytes
        imported_text = sandbox.import_output_text("final-0.md")
        self.assertIn("# Result", imported_text)

        dest_file = self.base_root / "imported_final.md"
        sandbox.import_output_to_file("final-0.md", dest_file)
        self.assertTrue(dest_file.exists())
        self.assertEqual(dest_file.read_bytes(), sandbox.import_output("final-0.md"))
        self.assertEqual(dest_file.read_text(encoding="utf-8").splitlines(), imported_text.splitlines())

        sandbox.cleanup()

    def test_09_fail_closed_when_mandatory_os_sandbox_unavailable(self):
        """When mandatory OS sandbox cannot be established, execution fails closed with FileSandboxSecurityError."""
        # If enforce_os_boundary=True on an unsupported or simulated failing platform, it must fail closed
        config = FileSandboxConfig(
            sandbox_id="test_fail_closed",
            sandbox_root=self.sandbox_dir,
            coordinator_private_dir=self.coordinator_dir,
            enforce_os_boundary=True,
            clean_on_exit=False,
        )
        sandbox = ExecutionSandbox(config)

        if os.name != "nt":
            # On Linux or macOS, if Landlock/Seatbelt is unavailable, prepare() must fail closed
            if sys.platform.startswith("linux") and not LinuxLandlockController.is_available():
                with self.assertRaises(FileSandboxSecurityError) as ctx:
                    sandbox.prepare()
                self.assertIn("fail-closed", str(ctx.exception).lower())
            elif sys.platform == "darwin" and not MacOsSeatbeltController.is_available():
                with self.assertRaises(FileSandboxSecurityError) as ctx:
                    sandbox.prepare()
                self.assertIn("fail-closed", str(ctx.exception).lower())
        else:
            # On Windows, WindowsAppContainerController should establish the boundary successfully
            sandbox.prepare()
            self.assertEqual(sandbox.boundary_status, "enforced_windows_appcontainer")
            sandbox.cleanup()

    def test_10_e2e_cli_worker_delegation_with_filesystem_sandbox(self):
        """End-to-end integration: run_worker uses ExecutionSandbox, produces valid output, and blocks secret leaks."""
        from ai_coordinator.storage import Store
        from ai_coordinator.cli import run_worker

        e2e_root = self.base_root / "e2e_coordinator"
        e2e_root.mkdir(parents=True, exist_ok=True)
        store = Store(root=e2e_root)

        # Create task
        task = store.new(goal="E2E delegated task with sandbox", project=str(self.base_root))

        # Create worker input
        input_path = self.base_root / "e2e_input.txt"
        input_path.write_text("Delegated payload for analysis.", encoding="utf-8")

        # Mock worker client script: executes in Python, writes output, and attempts to read supervisor.secret
        tools_dir = self.base_root / "worker_tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        mock_worker_py = tools_dir / "mock_worker_agent.py"
        secret_to_probe = store.root / "auth" / "supervisor.secret"
        mock_worker_py.write_text(f"""import sys, os, pathlib, json

# Attempt adversarial probe against supervisor secret
leak_attempt = "NONE"
try:
    leak_attempt = pathlib.Path(r"{secret_to_probe}").read_text(encoding="utf-8")
except Exception as e:
    leak_attempt = f"DENIED: {{type(e).__name__}}"

result_payload = {{
    "result": f"# Result\\nAnalysis completed successfully.\\nLeakStatus: {{leak_attempt}}\\n",
    "usage": {{
        "total_tokens": 25,
        "cost": 0.0001
    }}
}}
print(json.dumps(result_payload))
sys.exit(0)
""", encoding="utf-8")

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
                "enforce_filesystem_sandbox": self._is_os_boundary_enforced(),
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

        # Run worker through real CLI delegation
        summary = run_worker(store, config, task["id"], "general", str(input_path))
        self.assertEqual(summary.get("status"), "complete")

        # Verify output exists and was imported into result.md
        runs = store.runs(task["id"])
        self.assertTrue(len(runs) > 0)
        self.assertEqual(runs[0]["status"], "complete")

        output_path = Path(runs[0]["output_path"])
        self.assertTrue(output_path.exists())
        result_content = output_path.read_text(encoding="utf-8")

        # Verify that supervisor secret was NOT leaked
        self.assertNotIn("SUPERVISOR_SECRET_KEY", result_content)
        self.assertIn("Result", result_content)
        self.assertIn("Analysis completed successfully.", result_content)
        if self._is_os_boundary_enforced():
            self.assertIn("LeakStatus: DENIED", result_content)


if __name__ == "__main__":
    unittest.main()
