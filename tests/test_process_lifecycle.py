from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ai_coordinator.process import (
    ProcessIdentity,
    ProcessSupervisor,
    SupervisedProcess,
    TerminationResult,
    WindowsProcessController,
    PosixProcessController,
    safe_process_alive,
    safe_terminate_tree,
)
from ai_coordinator.cli import Store


class ProcessLifecycleTests(unittest.TestCase):
    def test_identity_capture_and_serialization(self):
        ident = ProcessSupervisor.capture_identity(os.getpid())
        self.assertEqual(ident.pid, os.getpid())
        self.assertGreater(ident.create_time_ns, 0)
        self.assertTrue(ident.is_alive())

        data = ident.to_dict()
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["create_time_ns"], ident.create_time_ns)

        reconstructed = ProcessIdentity.from_dict(data)
        self.assertEqual(reconstructed.pid, ident.pid)
        self.assertEqual(reconstructed.create_time_ns, ident.create_time_ns)
        self.assertEqual(reconstructed.system, ident.system)
        self.assertTrue(reconstructed.is_alive())

    def test_pid_reuse_prevention_mismatched_creation_time(self):
        proc = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            role="worker",
        )
        self.assertTrue(proc.is_alive())
        pid = proc.pid
        real_time = proc.identity.create_time_ns
        fake_time = real_time + 77777777

        # Attempt terminate with forged creation time
        res = safe_terminate_tree(pid, expected_create_time_ns=fake_time)
        self.assertFalse(res.success)
        self.assertEqual(res.reason, "identity_mismatch")
        self.assertIn("recycled", res.detail.lower())

        # Target must remain alive and unharmed
        self.assertTrue(proc.is_alive())
        self.assertTrue(safe_process_alive(pid, expected_create_time_ns=real_time))

        # Terminate with real creation time
        real_res = proc.terminate(timeout_seconds=2.0)
        self.assertTrue(real_res.success)
        self.assertFalse(proc.is_alive())
        self.assertFalse(safe_process_alive(pid, expected_create_time_ns=real_time))

    def test_job_object_kill_on_close_fail_closed(self):
        if os.name != "nt":
            self.skipTest("Job Objects are specific to Windows NT")

        with tempfile.TemporaryDirectory() as temp_dir:
            pid_file = Path(temp_dir) / "worker.pid"
            sup_script = Path(temp_dir) / "supervisor_runner.py"

            sup_code = f"""import sys, time, pathlib
sys.path.insert(0, {json.dumps(str(Path(__file__).resolve().parents[1] / "src"))})
from ai_coordinator.process import ProcessSupervisor

proc = ProcessSupervisor.spawn([sys.executable, "-c", "import time; time.sleep(30)"], role="worker")
pathlib.Path({json.dumps(str(pid_file))}).write_text(str(proc.pid), encoding="ascii")
# Keep running for 1 second then exit abruptly without calling proc.terminate
time.sleep(1)
sys.exit(0)
"""
            sup_script.write_text(sup_code, encoding="utf-8")

            # Run supervisor as separate process
            sup_proc = subprocess.Popen([sys.executable, str(sup_script)])
            # Wait for supervisor to write worker pid
            for _ in range(30):
                if pid_file.exists():
                    break
                time.sleep(0.1)

            self.assertTrue(pid_file.exists(), "Supervisor did not write worker PID")
            worker_pid = int(pid_file.read_text(encoding="ascii").strip())

            # Worker should be alive while supervisor is running
            self.assertTrue(safe_process_alive(worker_pid))

            # Wait for supervisor to exit
            sup_proc.wait(timeout=5)
            self.assertEqual(sup_proc.returncode, 0)

            # Once supervisor dies and its job handle is closed by the OS kernel,
            # Windows KILL_ON_JOB_CLOSE must terminate the worker process automatically!
            time.sleep(0.5)
            self.assertFalse(
                safe_process_alive(worker_pid),
                f"Worker {worker_pid} survived supervisor death; fail-closed guarantee failed!",
            )

    def test_supervised_process_normal_lifecycle(self):
        proc = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import sys; sys.stdout.write('worker-ok\\n'); sys.exit(0)"],
            role="worker",
            stdout=subprocess.PIPE,
        )
        stdout, _ = proc.communicate(timeout=5)
        self.assertEqual(stdout.strip(), "worker-ok")
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(proc.is_alive())

    def test_supervised_process_context_manager(self):
        with ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            role="worker",
        ) as proc:
            pid = proc.pid
            ident_time = proc.identity.create_time_ns
            self.assertTrue(proc.is_alive())

        # Exiting context block must terminate the process automatically
        time.sleep(0.2)
        self.assertFalse(safe_process_alive(pid, expected_create_time_ns=ident_time))

    def test_nonexistent_and_invalid_pids(self):
        self.assertFalse(safe_process_alive(-1))
        self.assertFalse(safe_process_alive(0))
        self.assertFalse(safe_process_alive(99999999))

        res = safe_terminate_tree(-1)
        self.assertFalse(res.success)
        self.assertEqual(res.reason, "not_found")

        res2 = safe_terminate_tree(99999999)
        self.assertTrue(res2.success or res2.reason in ("not_found", "permission_denied"))

    def test_store_recovery_with_process_create_time(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "data")
            try:
                project = Path(temp_dir) / "proj"
                project.mkdir()
                task = store.new(project, "Test recovery")

                # Spawn a real worker process to simulate active run
                worker = ProcessSupervisor.spawn(
                    [sys.executable, "-c", "import time; time.sleep(15)"],
                    role="worker",
                )
                try:
                    run_id, folder, _ = store.reserve(task["id"], "code", "prompt", 5)
                    store.update_run(
                        run_id,
                        pid=worker.pid,
                        process_create_time=worker.identity.create_time_ns,
                    )

                    # 1. Attempting recover while both owner and worker are alive must be rejected
                    with self.assertRaises(ValueError) as ctx:
                        store.recover(task["id"], run_id)
                    self.assertIn("Processo ainda existe", str(ctx.exception))

                    # 2. Simulate that the owner coordinator crashed/died, but worker is still alive
                    store.db.execute("UPDATE runs SET owner_pid=99999999, owner_create_time=12345 WHERE id=?", (run_id,))
                    store.db.commit()

                    # Still rejected because worker is still running!
                    with self.assertRaises(ValueError) as ctx:
                        store.recover(task["id"], run_id)
                    self.assertIn("Processo ainda existe", str(ctx.exception))

                    # 3. Now terminate the worker as well
                    worker.terminate(timeout_seconds=2.0)
                    self.assertFalse(worker.is_alive())

                    # 4. Now recover must succeed and mark run as interrupted
                    recovered = store.recover(task["id"], run_id)
                    self.assertEqual(recovered["status"], "interrupted")
                finally:
                    if worker.is_alive():
                        worker.terminate(timeout_seconds=1.0)
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
