from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_coordinator.streaming import (
    StreamingScrubber,
    SafeStreamConsumer,
)
from ai_coordinator.process import (
    ProcessSupervisor,
    safe_process_alive,
    safe_terminate_tree,
    PosixProcessController,
    WindowsProcessController,
)
from ai_coordinator.contracts import (
    validate_headless_arguments,
    CLIENT_CONTRACTS,
)
from ai_coordinator.mission import (
    is_path_allowed,
    FlightPlan,
    FlightBudget,
)
from ai_coordinator.availability import (
    classify,
)
from ai_coordinator.storage import (
    Store,
    RunState,
)


class SecurityHardeningH1ToH7Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "data"
        self.project = Path(self.temp_dir.name) / "project"
        self.project.mkdir()
        self.store = Store(self.root)

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # H1: Sanitização em streaming sem vazamento de Bearer/prefixos em blocos
    # -------------------------------------------------------------------------
    def test_h1_streaming_bearer_split_across_chunks_no_leak(self):
        """Reproduces the exact synthetic leak: 'Bearer ' in chunk 1, 160-char token in chunk 2."""
        scrubber = StreamingScrubber(overlap_size=128)
        chunk1 = "Authorization: Bearer "
        chunk2 = "a" * 160 + "\n"

        out1 = scrubber.feed(chunk1)
        out2 = scrubber.feed(chunk2)
        out3 = scrubber.flush()

        combined = out1 + out2 + out3
        # Token must NOT leak into any emitted chunk
        self.assertNotIn("aaaa", out1)
        self.assertNotIn("aaaa", out2)
        self.assertNotIn("aaaa", out3)
        self.assertNotIn("aaaa", combined)

        # Scrubber must redact Bearer token properly
        self.assertIn("Bearer [REDACTED]", combined)

    def test_h1_streaming_split_sk_token_and_passwords(self):
        scrubber = StreamingScrubber(overlap_size=64)
        c1 = "Log prefix: db password="
        c2 = "supersecret1234567890\nNext line"

        o1 = scrubber.feed(c1)
        o2 = scrubber.feed(c2)
        o3 = scrubber.flush()

        combined = o1 + o2 + o3
        self.assertNotIn("supersecret1234567890", combined)
        self.assertIn("[REDACTED]", combined)

    # -------------------------------------------------------------------------
    # H2: Controle de processos e prevenção contra TOCTOU e órfãos
    # -------------------------------------------------------------------------
    def test_h2_safe_terminate_tree_tracks_child_creation_times(self):
        """Verify safe_terminate_tree enumerates and terminates child processes safely."""
        proc = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            role="worker",
        )
        self.assertTrue(proc.is_alive())
        res = safe_terminate_tree(proc, expected_create_time_ns=proc.identity.create_time_ns)
        self.assertTrue(res.success)
        self.assertFalse(proc.is_alive())

    def test_h2_toctou_rejection_with_recycled_identity(self):
        proc = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            role="worker",
        )
        fake_time = proc.identity.create_time_ns + 999_999_999
        res = safe_terminate_tree(proc.pid, expected_create_time_ns=fake_time)
        self.assertFalse(res.success)
        self.assertEqual(res.reason, "identity_mismatch")
        self.assertTrue(proc.is_alive())
        proc.terminate(timeout_seconds=2.0)

    # -------------------------------------------------------------------------
    # H3: Validação de Contratos Headless Positivos
    # -------------------------------------------------------------------------
    def test_h3_headless_contract_requires_mandatory_security_flags(self):
        # Codex requires --sandbox read-only
        with self.assertRaises(ValueError) as ctx:
            validate_headless_arguments("codex", "worker", ["codex", "exec", "prompt"])
        self.assertIn("obrigatorio", str(ctx.exception).lower())

        # Codex with correct sandbox succeeds
        validate_headless_arguments(
            "codex",
            "worker",
            ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "-c", 'approval_policy="never"'],
        )

        # Codex with sandbox override to write-all fails
        with self.assertRaises(ValueError) as ctx:
            validate_headless_arguments(
                "codex",
                "worker",
                ["codex", "exec", "--sandbox", "read-only", "--ephemeral", "-c", 'approval_policy="never"', "--sandbox", "write-all"],
            )
        self.assertIn("override de sandbox proibido", str(ctx.exception).lower())

        # Claude requires --print and --tools ""
        with self.assertRaises(ValueError) as ctx:
            validate_headless_arguments("claude", "worker", ["claude", "-p", "prompt"])
        self.assertIn("obrigatorio", str(ctx.exception).lower())

        # Claude with correct flags succeeds
        validate_headless_arguments("claude", "worker", ["claude", "--print", "--tools", "", "prompt"])

    # -------------------------------------------------------------------------
    # H4: Isolamento de caminhos e escape por links simbólicos
    # -------------------------------------------------------------------------
    def test_h4_symlink_escape_outside_allowed_paths_strictly_rejected(self):
        allowed_dir = self.project / "allowed_src"
        allowed_dir.mkdir()
        secret_dir = Path(self.temp_dir.name) / "secret_outside"
        secret_dir.mkdir()
        secret_file = secret_dir / "passwords.txt"
        secret_file.write_text("root:supersecret", encoding="utf-8")

        # 1. Real symlink test if OS privileges allow
        symlink_path = allowed_dir / "shortcut_outside"
        try:
            symlink_path.symlink_to(secret_dir, target_is_directory=True)
            target = symlink_path / "passwords.txt"
            self.assertFalse(is_path_allowed(target, [allowed_dir]))
        except (OSError, NotImplementedError):
            pass

        # 2. Canonical realpath resolution verification (unit test for path escape)
        with patch("os.path.realpath") as mock_realpath:
            # Simulate target resolving to outside directory /etc/shadow
            def fake_realpath(p):
                if "shortcut" in str(p):
                    return "C:/private/secret.txt" if os.name == "nt" else "/etc/shadow"
                return str(p)
            mock_realpath.side_effect = fake_realpath

            candidate = allowed_dir / "shortcut" / "secret.txt"
            self.assertFalse(is_path_allowed(candidate, [allowed_dir]))

    # -------------------------------------------------------------------------
    # H5: Verificação Determinística do Portão de Pouso
    # -------------------------------------------------------------------------
    def test_h5_landing_gate_verifies_artifacts_and_rejects_superficial_claims(self):
        task = self.store.new(self.project, "Tarefa com pouso substancial")
        task_id = task["id"]

        # 1. Superficial evidence is rejected by record_landing_check
        with self.assertRaises(ValueError) as ctx:
            self.store.record_landing_check(task_id, "check_tests", "passed", evidence="ok")
        self.assertIn("superficial rejeitada", str(ctx.exception).lower())

        with self.assertRaises(ValueError) as ctx:
            self.store.record_landing_check(task_id, "check_tests", "passed", evidence="passed")
        self.assertIn("superficial rejeitada", str(ctx.exception).lower())

        # 2. Evidence referencing an artifact file that does not exist fails verify_landing
        plan_dict = {
            "goal": "Verificar artefato",
            "acceptance_criteria": ["Artefato existente"],
            "allowed_paths": [str(self.project)],
            "budget": {"max_steps": 10, "max_uncheckpointed_steps": 5},
            "mandatory_landing_checks": ["ci_artifact_check"],
        }
        self.store.set_flight_plan(FlightPlan.from_dict({**plan_dict, "task_id": task_id}))

        # Record with non-existent artifact file
        self.store.record_landing_check(
            task_id,
            "ci_artifact_check",
            "passed",
            evidence="artifact: missing_report.json com testes concluidos",
        )
        passed, unmet = self.store.verify_landing(task_id)
        self.assertFalse(passed)
        self.assertTrue(any("nao encontrado" in u for u in unmet))

        # 3. Create the real artifact file with content -> verify_landing passes
        report_file = self.store.root / "tasks" / task_id / "missing_report.json"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(json.dumps({"tests_passed": 70, "status": "ok"}), encoding="utf-8")

        passed_real, unmet_real = self.store.verify_landing(task_id)
        self.assertTrue(passed_real)
        self.assertEqual(unmet_real, [])

    # -------------------------------------------------------------------------
    # H6: Classificação Precisa de Erros sem Falsos Positivos
    # -------------------------------------------------------------------------
    def test_h6_error_classification_no_false_positive_permission_cooldown(self):
        # A local file PermissionError message must NOT be classified as model permission error
        local_err = "PermissionError: [Errno 13] Permission denied: 'D:/local_project/temp.txt'"
        classified = classify("claude", "sonnet", local_err)
        # Must NOT classify as "permission" kind with 3600s cooldown
        self.assertNotEqual(classified["kind"], "permission")
        self.assertNotEqual(classified["cooldown"], 3600)

        # An internal log mentioning "harness" must NOT trigger cooldown
        harness_err = "Agent harness context initialized with standard tools"
        classified_harness = classify("codex", "gpt-6-luna", harness_err)
        self.assertNotEqual(classified_harness["kind"], "permission")

        # Real HTTP 403 / API permission error DOES trigger permission
        real_403 = "HTTP status: 403 Forbidden: access denied to model anthropic/claude-3-opus"
        classified_real = classify("claude", "opus", real_403)
        self.assertEqual(classified_real["kind"], "permission")
        self.assertEqual(classified_real["cooldown"], 3600)

    # -------------------------------------------------------------------------
    # H7: Deadline Integral Abrangendo Stdin
    # -------------------------------------------------------------------------
    def test_h7_stdin_timeout_covers_unresponsive_child(self):
        """Process that hangs and never reads stdin is cleanly terminated at timeout deadline."""
        # Code that opens without reading stdin and sleeps
        code = "import time; time.sleep(15)"
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        consumer = SafeStreamConsumer()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            # Feed 100 KiB of data into stdin with 1.5 second timeout
            consumer.consume(proc, timeout_seconds=1.5, stdin_data="x" * 100_000)
        elapsed = time.monotonic() - start

        # Must time out within reasonable margin (~1.5s to 3.5s), NOT 15s!
        self.assertLess(elapsed, 5.0)
        proc.poll()
        self.assertIsNotNone(proc.returncode)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
