from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ai_coordinator.streaming import (
    StreamingScrubber,
    SafeStreamConsumer,
    StreamConsumeResult,
)
from ai_coordinator.contracts import (
    HeadlessContract,
    get_client_contract,
    validate_headless_arguments,
    CLIENT_CONTRACTS,
)
from ai_coordinator.process import ProcessSupervisor, safe_process_alive


class StreamingAndContractsTests(unittest.TestCase):
    # -----------------------------------------------------------------------
    # Scenario 1: Segredo dividido entre blocos
    # -----------------------------------------------------------------------
    def test_secret_split_across_chunks(self):
        scrubber = StreamingScrubber(overlap_size=32)
        # Split sk- token across two chunks
        chunk1 = "Log prefix: api token is sk-1234567890"
        chunk2 = "abcdef1234567890abcdef and more logs\n"

        out1 = scrubber.feed(chunk1)
        out2 = scrubber.feed(chunk2)
        out3 = scrubber.flush()

        combined = out1 + out2 + out3
        self.assertIn("[REDACTED]", combined)
        self.assertNotIn("sk-1234567890", combined)
        self.assertNotIn("abcdef1234567890abcdef", combined)

    # -----------------------------------------------------------------------
    # Scenario 2: Segredo conhecido entre linhas
    # -----------------------------------------------------------------------
    def test_known_secret_between_lines(self):
        known = {"secret-top-token-1234"}
        scrubber = StreamingScrubber(overlap_size=32, exact_secrets=known)
        
        chunk1 = "Header: Bearer secret-top-"
        chunk2 = "token-1234\nLine 2 info\n"

        out1 = scrubber.feed(chunk1)
        out2 = scrubber.feed(chunk2)
        out3 = scrubber.flush()

        combined = out1 + out2 + out3
        self.assertIn("[REDACTED]", combined)
        self.assertNotIn("secret-top-token-1234", combined)

    # -----------------------------------------------------------------------
    # Scenario 3: Saída sem quebra de linha com memória limitada
    # -----------------------------------------------------------------------
    def test_stream_without_newlines_bounded_memory(self):
        scrubber = StreamingScrubber(overlap_size=32, max_word_size=256)
        # Feed 10,000 characters without any newlines
        long_line = "A" * 10000
        emitted_chunks = []
        for i in range(0, len(long_line), 500):
            out = scrubber.feed(long_line[i:i+500])
            if out:
                emitted_chunks.append(out)
        emitted_chunks.append(scrubber.flush())

        total_emitted = "".join(emitted_chunks)
        self.assertEqual(len(total_emitted), 10000)
        # Buffer inside scrubber remained strictly bounded
        self.assertEqual(len(scrubber.buffer), 0)

    # -----------------------------------------------------------------------
    # Scenario 4: Linha excessivamente longa com descarte/emissão segura
    # -----------------------------------------------------------------------
    def test_excessively_long_token_safe_truncation(self):
        scrubber = StreamingScrubber(overlap_size=64, max_word_size=256)
        # Single continuous token of 2000 characters
        token = "X" * 2000
        out1 = scrubber.feed(token)
        out2 = scrubber.flush()
        self.assertEqual(len(out1 + out2), 2000)
        self.assertEqual(len(scrubber.buffer), 0)

    # -----------------------------------------------------------------------
    # Scenario 5: Saída contínua de grande volume sem deadlock
    # -----------------------------------------------------------------------
    def test_continuous_high_volume_stream_no_deadlock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "stdout.log"
            err_file = Path(temp_dir) / "stderr.log"

            code = "import sys; sys.stdout.write('data\\n' * 5000); sys.stderr.write('err\\n' * 5000)"
            proc = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            consumer = SafeStreamConsumer(
                stdout_path=out_file,
                stderr_path=err_file,
                max_log_bytes=500_000,
            )
            res = consumer.consume(proc, timeout_seconds=10)
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(res.returncode, 0)
            self.assertTrue(out_file.exists())
            self.assertTrue(err_file.exists())
            self.assertGreater(out_file.stat().st_size, 10000)
            self.assertGreater(err_file.stat().st_size, 10000)

    # -----------------------------------------------------------------------
    # Scenario 6: stdout e stderr simultâneos
    # -----------------------------------------------------------------------
    def test_simultaneous_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "stdout.log"
            err_file = Path(temp_dir) / "stderr.log"

            code = """import sys
sys.stdout.write("out1\\n")
sys.stderr.write("err1\\n")
sys.stdout.write("out2\\n")
sys.stderr.write("err2\\n")
"""
            proc = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )

            consumer = SafeStreamConsumer(stdout_path=out_file, stderr_path=err_file)
            res = consumer.consume(proc, timeout_seconds=5)
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(res.returncode, 0)
            self.assertIn("out1", res.stdout)
            self.assertIn("out2", res.stdout)
            self.assertIn("err1", res.stderr)
            self.assertIn("err2", res.stderr)

    # -----------------------------------------------------------------------
    # Scenario 7: Processo encerrado por timeout integrado com ProcessSupervisor
    # -----------------------------------------------------------------------
    def test_process_timeout_integration_with_supervisor(self):
        proc = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            role="worker",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        pid = proc.pid
        consumer = SafeStreamConsumer()
        with self.assertRaises(TimeoutError):
            consumer.consume(proc, timeout_seconds=0.5)

        proc.stdout.close()
        proc.stderr.close()
        time.sleep(0.2)
        self.assertFalse(safe_process_alive(pid))

    # -----------------------------------------------------------------------
    # Scenario 8: Queda durante a execução não deixa log bruto
    # -----------------------------------------------------------------------
    def test_process_crash_preserves_scrubbed_logs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out_file = Path(temp_dir) / "stdout.log"
            code = """import sys
sys.stdout.write("Leaked key: sk-abcdef123456789012345678\\n")
sys.stdout.flush()
raise SystemExit(1)
"""
            proc = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            consumer = SafeStreamConsumer(stdout_path=out_file)
            res = consumer.consume(proc, timeout_seconds=5)
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(res.returncode, 1)

            # Log on disk must already be sanitized
            disk_text = out_file.read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", disk_text)
            self.assertNotIn("sk-abcdef123456789012345678", disk_text)

    # -----------------------------------------------------------------------
    # Scenario 9: Credenciais nos erros sanitizadas
    # -----------------------------------------------------------------------
    def test_credentials_in_error_messages_sanitized(self):
        scrubber = StreamingScrubber()
        error_json = '{"error": {"message": "Invalid auth token: sk-999999999999999999999999"}}'
        scrubbed = scrubber.scrub(error_json)
        self.assertIn("[REDACTED]", scrubbed)
        self.assertNotIn("sk-999999999999999999999999", scrubbed)

    # -----------------------------------------------------------------------
    # Scenario 10: Worker tenta usar ferramenta proibida ou flag irrestrita
    # -----------------------------------------------------------------------
    def test_worker_prohibited_flags_blocked(self):
        # Worker codex using forbidden --dangerously-skip-permissions
        with self.assertRaises(ValueError) as ctx:
            validate_headless_arguments("codex", "worker", ["codex", "exec", "--dangerously-skip-permissions"])
        self.assertIn("Flag proibida", str(ctx.exception))

        # Worker claude using forbidden --allow-all
        with self.assertRaises(ValueError) as ctx:
            validate_headless_arguments("claude", "worker", ["claude", "--allow-all"])
        self.assertIn("Flag proibida", str(ctx.exception))

    # -----------------------------------------------------------------------
    # Scenario 11: Coordenador encontra bloqueio de permissão (needs_input)
    # -----------------------------------------------------------------------
    def test_coordinator_permission_mode_needs_input(self):
        contract = get_client_contract("agy", role="coordinator")
        self.assertIsNotNone(contract)
        self.assertTrue(contract.supports_coordinator_headless)
        self.assertEqual(contract.permission_mode, "needs_input")

        # Worker role must be disallowed for agy
        self.assertIsNone(get_client_contract("agy", role="worker"))

    # -----------------------------------------------------------------------
    # Scenario 12: Cliente sem headless seguro recusado explicitamente
    # -----------------------------------------------------------------------
    def test_client_without_safe_headless_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            validate_headless_arguments("non_existent_client", "worker", ["non_existent_client", "run"])
        self.assertIn("Cliente desconhecido", str(ctx.exception))

        # agy cannot be executed as worker
        with self.assertRaises(ValueError) as ctx:
            validate_headless_arguments("agy", "worker", ["agy", "run"])
        self.assertIn("nao suporta", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
