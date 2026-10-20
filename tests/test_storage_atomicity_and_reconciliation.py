from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_coordinator.process import ProcessSupervisor
from ai_coordinator.storage import (
    Store,
    RunState,
    ACTIVE_RUN_STATES,
    TERMINAL_RUN_STATES,
)


class StorageAtomicityAndReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "data"
        self.project = Path(self.temp_dir.name) / "project"
        self.project.mkdir()
        self.store = Store(self.root)
        self.task = self.store.new(self.project, "Tarefa de teste Etapa 1C")

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Duas reservas simultâneas para a mesma execução
    # -------------------------------------------------------------------------
    def test_duplicate_and_concurrent_reservations_rejected(self):
        run_id, folder, remaining = self.store.reserve(self.task["id"], "code", "prompt 1", 5)
        self.assertTrue(run_id)
        self.assertTrue(folder.exists())

        # Second sequential reservation attempt on the same task must be rejected
        with self.assertRaises(ValueError) as ctx:
            self.store.reserve(self.task["id"], "code", "prompt 2", 5)
        self.assertIn("Worker em execucao", str(ctx.exception))

        # Test true multithreaded concurrency on a new task
        task2 = self.store.new(self.project, "Tarefa concorrente")
        results = []
        errors = []

        def attempt_reserve(p):
            local_store = Store(self.root)
            try:
                res = local_store.reserve(task2["id"], "code", f"prompt {p}", 5)
                results.append(res)
            except ValueError as err:
                errors.append(str(err))
            finally:
                local_store.db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(attempt_reserve, i) for i in range(4)]
            concurrent.futures.wait(futures)

        # Exactly 1 reservation must succeed, other 3 rejected with worker in execution
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 3)
        for err in errors:
            self.assertIn("Worker em execucao", err)

    # -------------------------------------------------------------------------
    # 2. Atualização com revisão obsoleta rejeitada (Fencing)
    # -------------------------------------------------------------------------
    def test_update_and_transition_with_stale_revision_rejected(self):
        run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
        row = self.store.db.execute("SELECT revision, status FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["revision"], 1)
        self.assertEqual(row["status"], RunState.RESERVED)

        # Successful update with matching revision (rev 1 -> rev 2)
        updated = self.store.update_run(run_id, expected_revision=1, client="opencode")
        self.assertEqual(updated["revision"], 2)

        # Stale update attempting revision 1 must be rejected
        with self.assertRaises(ValueError) as ctx:
            self.store.update_run(run_id, expected_revision=1, model="test-model")
        self.assertIn("Revisao obsoleta", str(ctx.exception))

        # Stale transition attempting revision 1 must also be rejected
        with self.assertRaises(ValueError) as ctx:
            self.store.transition_run(run_id, to_status=RunState.STARTING, expected_revision=1)
        self.assertIn("Revisao obsoleta", str(ctx.exception))

        # Successful transition with current revision 2 -> rev 3
        trans = self.store.transition_run(run_id, to_status=RunState.STARTING, expected_revision=2)
        self.assertEqual(trans["revision"], 3)
        self.assertEqual(trans["status"], RunState.STARTING)

    # -------------------------------------------------------------------------
    # 3. Queda entre reserva e inicialização
    # -------------------------------------------------------------------------
    def test_crash_between_reservation_and_startup(self):
        run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
        # Simulate supervisor crashed before spawning process: dead owner, no child process
        self.store.db.execute(
            "UPDATE runs SET owner_pid=99999998, owner_create_time=111111 WHERE id=?",
            (run_id,)
        )
        self.store.db.commit()

        reconciled = self.store.reconcile(self.task["id"])
        self.assertEqual(len(reconciled), 1)
        rec = reconciled[0]
        self.assertEqual(rec["status"], RunState.INTERRUPTED)
        # Guarantees: exit_code and ended must remain None/NULL, observed_at is set
        self.assertIsNone(rec["exit_code"])
        self.assertIsNone(rec["ended"])
        self.assertIsNotNone(rec["observed_at"])
        self.assertIn("Queda antes da inicializacao", rec["error"])

    # -------------------------------------------------------------------------
    # 4. Supervisor morto com cliente ainda ativo
    # -------------------------------------------------------------------------
    def test_supervisor_dead_with_worker_still_running_becomes_unsupervised(self):
        worker = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            role="worker",
        )
        try:
            run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
            self.store.transition_run(
                run_id,
                to_status=RunState.RUNNING,
                expected_status=RunState.RESERVED,
                pid=worker.pid,
                process_create_time=worker.identity.create_time_ns,
            )

            # Simulate supervisor coordinator died, but worker process is still alive
            self.store.db.execute(
                "UPDATE runs SET owner_pid=99999997, owner_create_time=222222 WHERE id=?",
                (run_id,)
            )
            self.store.db.commit()

            reconciled = self.store.reconcile(self.task["id"])
            self.assertEqual(len(reconciled), 1)
            rec = reconciled[0]
            # State must transition to UNSUPERVISED
            self.assertEqual(rec["status"], RunState.UNSUPERVISED)
            # Worker process must NOT be terminated
            self.assertTrue(worker.is_alive())
            # Must not be marked complete or failed
            self.assertIsNone(rec["exit_code"])
            self.assertIsNone(rec["ended"])

            # Attempting manual recover while worker process is alive must be rejected
            with self.assertRaises(ValueError) as ctx:
                self.store.recover(self.task["id"], run_id)
            self.assertIn("Processo ainda existe", str(ctx.exception))
        finally:
            if worker.is_alive():
                worker.terminate(timeout_seconds=2.0)

    # -------------------------------------------------------------------------
    # 5. Supervisor e cliente ausentes sem registro final
    # -------------------------------------------------------------------------
    def test_supervisor_and_client_absent_without_final_record(self):
        run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
        self.store.transition_run(
            run_id,
            to_status=RunState.RUNNING,
            expected_status=RunState.RESERVED,
            pid=99999996,
            process_create_time=333333,
        )
        # Both owner and process are dead, no final exit recorded
        self.store.db.execute(
            "UPDATE runs SET owner_pid=99999995, owner_create_time=444444 WHERE id=?",
            (run_id,)
        )
        self.store.db.commit()

        reconciled = self.store.reconcile(self.task["id"])
        self.assertEqual(len(reconciled), 1)
        rec = reconciled[0]
        self.assertEqual(rec["status"], RunState.INTERRUPTED)
        self.assertIsNone(rec["exit_code"])
        self.assertIsNone(rec["ended"])
        self.assertIsNotNone(rec["observed_at"])
        self.assertIn("Supervisor e cliente ausentes", rec["error"])

    # -------------------------------------------------------------------------
    # 6. Identidade do processo impossível de consultar
    # -------------------------------------------------------------------------
    def test_process_identity_query_failure_transitions_to_identity_unknown(self):
        run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
        self.store.transition_run(
            run_id,
            to_status=RunState.RUNNING,
            expected_status=RunState.RESERVED,
            pid=1234,
            process_create_time=555555,
        )

        # Simulate OS permission/query error preventing identity determination
        with patch("ai_coordinator.storage.safe_process_alive", side_effect=PermissionError("Access denied querying times")):
            reconciled = self.store.reconcile(self.task["id"])
            self.assertEqual(len(reconciled), 1)
            rec = reconciled[0]
            self.assertEqual(rec["status"], RunState.IDENTITY_UNKNOWN)
            self.assertIsNotNone(rec["observed_at"])
            self.assertIsNone(rec["ended"])
            self.assertIn("impossivel de consultar", rec["error"])

    # -------------------------------------------------------------------------
    # 7. Cancelamento simultâneo ao encerramento normal
    # -------------------------------------------------------------------------
    def test_cancellation_concurrent_with_normal_exit(self):
        run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
        self.store.transition_run(run_id, to_status=RunState.RUNNING, expected_status=RunState.RESERVED)

        # 1. Operator requests cancellation
        cancel_res = self.store.request_cancel(run_id)
        self.assertEqual(cancel_res["status"], RunState.CANCEL_REQUESTED)
        self.assertIsNotNone(cancel_res["cancel_requested_at"])

        # 2. Worker process finished normally with exit_code 0 concurrently
        exit_res = self.store.confirm_exit(run_id, exit_code=0, output_path="/path/to/result.md")
        self.assertEqual(exit_res["status"], RunState.COMPLETE)
        self.assertEqual(exit_res["exit_code"], 0)
        self.assertIsNotNone(exit_res["ended"])

        # Both events must be in audit log
        events = self.store.events(run_id)
        event_types = [e["event_type"] for e in events]
        self.assertIn("cancel_requested", event_types)
        self.assertIn("confirm_exit_after_cancel", event_types)

    # -------------------------------------------------------------------------
    # 8. Dois comandos resume concorrentes
    # -------------------------------------------------------------------------
    def test_concurrent_resume_commands_leave_consistent_state(self):
        # Set up an interrupted run
        run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
        self.store.db.execute("UPDATE runs SET owner_pid=99999999 WHERE id=?", (run_id,))
        self.store.db.commit()

        results = []

        def execute_resume():
            local_store = Store(self.root)
            try:
                res = local_store.resume(self.task["id"])
                results.append(res)
            finally:
                local_store.db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(execute_resume) for _ in range(2)]
            concurrent.futures.wait(futures)

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["task"]["id"], self.task["id"])
            self.assertEqual(r["recent_runs"][0]["status"], RunState.INTERRUPTED)

    # -------------------------------------------------------------------------
    # 9. Heartbeat expirado com processo ainda vivo
    # -------------------------------------------------------------------------
    def test_expired_heartbeat_with_process_alive_preserves_running_state(self):
        worker = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            role="worker",
        )
        try:
            run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
            self.store.transition_run(
                run_id,
                to_status=RunState.RUNNING,
                expected_status=RunState.RESERVED,
                pid=worker.pid,
                process_create_time=worker.identity.create_time_ns,
            )

            # Set heartbeat to 10 minutes ago
            stale_stamp = "2026-10-20T10:00:00.000000Z"
            self.store.db.execute("UPDATE runs SET last_heartbeat=? WHERE id=?", (stale_stamp, run_id))
            self.store.db.commit()

            # Reconcile with 10s silence threshold
            reconciled = self.store.reconcile(self.task["id"], silence_threshold_seconds=10.0)
            # Must NOT fail, terminate, or interrupt the run!
            self.assertEqual(len(reconciled), 0)

            # Run remains in RUNNING
            curr = self.store.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            self.assertEqual(curr["status"], RunState.RUNNING)
            self.assertTrue(worker.is_alive())
        finally:
            if worker.is_alive():
                worker.terminate(timeout_seconds=2.0)

    # -------------------------------------------------------------------------
    # 10. Persistência do código de saída observado
    # -------------------------------------------------------------------------
    def test_observed_exit_code_persistence(self):
        # Case A: exit code 0
        run_id_0, _, _ = self.store.reserve(self.task["id"], "code", "prompt 0", 5)
        self.store.transition_run(run_id_0, to_status=RunState.RUNNING, expected_status=RunState.RESERVED)
        res_0 = self.store.confirm_exit(run_id_0, exit_code=0)
        self.assertEqual(res_0["status"], RunState.COMPLETE)
        self.assertEqual(res_0["exit_code"], 0)
        self.assertEqual(
            self.store.db.execute("SELECT exit_code FROM runs WHERE id=?", (run_id_0,)).fetchone()[0],
            0
        )

        # Complete task to allow new run or use different task
        task_b = self.store.new(self.project, "Task B")
        run_id_1, _, _ = self.store.reserve(task_b["id"], "code", "prompt 137", 5)
        self.store.transition_run(run_id_1, to_status=RunState.RUNNING, expected_status=RunState.RESERVED)
        res_1 = self.store.confirm_exit(run_id_1, exit_code=137)
        self.assertEqual(res_1["status"], RunState.FAILED)
        self.assertEqual(res_1["exit_code"], 137)
        self.assertEqual(
            self.store.db.execute("SELECT exit_code FROM runs WHERE id=?", (run_id_1,)).fetchone()[0],
            137
        )

    # -------------------------------------------------------------------------
    # 11. Recuperação após interrupção durante gravação
    # -------------------------------------------------------------------------
    def test_recovery_after_mid_write_interruption(self):
        run_id, _, _ = self.store.reserve(self.task["id"], "code", "prompt", 5)
        rev_before = self.store.db.execute("SELECT revision FROM runs WHERE id=?", (run_id,)).fetchone()[0]

        # Simulate a crash/exception inside an atomic transaction
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.db.execute("UPDATE runs SET client='corrupt_uncommitted' WHERE id=?", (run_id,))
                raise RuntimeError("Crash during write!")

        # Verify transaction rolled back cleanly
        row_after = self.store.db.execute("SELECT client, revision FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertIsNone(row_after["client"])
        self.assertEqual(row_after["revision"], rev_before)

        # Subsequent writes operate normally without corrupt state
        updated = self.store.update_run(run_id, client="clean_write")
        self.assertEqual(updated["client"], "clean_write")
        self.assertEqual(updated["revision"], rev_before + 1)


if __name__ == "__main__":
    unittest.main()
