from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ai_coordinator import availability, runtime
from ai_coordinator.api import Coordinator
from ai_coordinator.cli import Store, benchmark_report
from ai_coordinator.runtime import worker_environment


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "data"
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.store = Store(self.root)

    def tearDown(self):
        self.store.db.close()
        self.temporary.cleanup()

    def test_task_links_current_ai_memory_workstream(self):
        workstream = "019c6e27-e55b-73d1-87d8-4e01f1f75043"
        with patch.dict(os.environ, {"AI_MEMORY_WORKSTREAM_ID": workstream}, clear=False):
            task = self.store.new(self.project, "Revisar implementacao")
        self.assertEqual(task["ai_memory_workstream_id"], workstream)
        self.assertEqual(self.store.context(self.project)[0]["ai_memory_workstream_id"], workstream)

    def test_explicit_workstream_overrides_environment(self):
        environment_id = "019c6e27-e55b-73d1-87d8-4e01f1f75043"
        explicit_id = "019c6e27-e55b-73d1-87d8-4e01f1f75044"
        with patch.dict(os.environ, {"AI_MEMORY_WORKSTREAM_ID": environment_id}, clear=False):
            task = self.store.new(self.project, "Tarefa vinculada", explicit_id)
        self.assertEqual(task["ai_memory_workstream_id"], explicit_id)

    def test_rejects_invalid_workstream_id(self):
        with self.assertRaisesRegex(ValueError, "UUID"):
            self.store.new(self.project, "Tarefa", "nao-e-uuid")

    def test_migrates_existing_task_database(self):
        self.store.db.close()
        self.root.mkdir(parents=True, exist_ok=True)
        database = self.root / "coordination.sqlite"
        for suffix in ("-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)
        database.unlink(missing_ok=True)
        legacy = sqlite3.connect(database)
        legacy.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, project TEXT NOT NULL, goal TEXT NOT NULL, "
            "status TEXT NOT NULL, revision INTEGER NOT NULL, snapshot TEXT NOT NULL, "
            "created TEXT NOT NULL, updated TEXT NOT NULL)"
        )
        legacy.commit()
        legacy.close()
        self.store = Store(self.root)
        columns = {row[1] for row in self.store.db.execute("PRAGMA table_info(tasks)")}
        self.assertIn("ai_memory_workstream_id", columns)

    def test_checkpoint_keeps_revision_guard(self):
        task = self.store.new(self.project, "Revisar implementacao")
        saved = self.store.checkpoint(task["id"], task["revision"], {"summary": "Feito"})
        self.assertEqual(saved["revision"], 2)
        with self.assertRaisesRegex(ValueError, "Revisao mudou"):
            self.store.checkpoint(task["id"], task["revision"], {"summary": "Antigo"})


class WorkerEnvironmentTests(unittest.TestCase):
    def test_allowlist_hides_ungranted_secrets_and_grants_per_client(self):
        source = {
            "PATH": "/bin",
            "HOME": "/home/test",
            "OPENROUTER_API_KEY": "secret-value",
            "OTHER_PRIVATE_TOKEN": "another-secret",
            "UNRELATED_SETTING": "not-forwarded",
        }
        config = {
            "worker_environment": {
                "mode": "allowlist",
                "allow": [],
                "allow_by_client": {"opencode": ["OPENROUTER_API_KEY"]},
            },
            "availability": {"opencode_cli_major": 2},
        }
        with patch.dict(os.environ, source, clear=True):
            opencode_env, policy = worker_environment(config, {"client": "opencode"}, "task-1")
            claude_env, _ = worker_environment(config, {"client": "claude"}, "task-1")
        self.assertEqual(policy, "allowlist")
        self.assertEqual(opencode_env["OPENROUTER_API_KEY"], "secret-value")
        self.assertNotIn("OTHER_PRIVATE_TOKEN", opencode_env)
        self.assertNotIn("UNRELATED_SETTING", opencode_env)
        self.assertNotIn("OPENROUTER_API_KEY", claude_env)
        self.assertEqual(opencode_env["AI_COORDINATOR_ROLE"], "worker")
        self.assertEqual(opencode_env["OPENCODE_DISABLE_DEFAULT_PLUGINS"], "true")

    def test_inherit_requires_explicit_mode(self):
        config = {"worker_environment": {"mode": "inherit"}}
        with patch.dict(os.environ, {"CUSTOM_WORKER_VALUE": "visible"}, clear=True):
            env, policy = worker_environment(config, {"client": "claude"}, "task-2")
        self.assertEqual(policy, "inherit")
        self.assertEqual(env["CUSTOM_WORKER_VALUE"], "visible")


class BenchmarkReportTests(unittest.TestCase):
    def test_summary_uses_only_manual_quality_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "results.json"
            target = Path(directory) / "summary.md"
            source.write_text(json.dumps({
                "schema_version": 1,
                "benchmark_id": "demo",
                "results": [
                    {"candidate": "codex/model-a", "quality_score": 4, "elapsed_seconds": 10,
                     "usage": {"total_tokens": 100, "cost": 0.02}, "case_id": "case-a"},
                    {"candidate": "claude/model-b", "quality_score": None, "elapsed_seconds": 8,
                     "usage": {}, "case_id": "case-a"},
                ],
            }), encoding="utf-8")
            result = benchmark_report(source, target)
            markdown = target.read_text(encoding="utf-8")
        self.assertEqual(result["scored_runs"], 1)
        self.assertIn("4.00", markdown)
        self.assertIn("sem nota", markdown)


class AvailabilityAndRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.health = availability.Health(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_403_and_429_block_only_model_while_quota_blocks_client_or_provider(self):
        c_403 = availability.classify("codex", "gpt-4o", "HTTP 403: Forbidden - access denied")
        self.assertEqual((c_403["kind"], c_403["scope"]), ("permission", "model"))

        c_429 = availability.classify("codex", "gpt-4o", "HTTP 429: Too Many Requests (rate limit)")
        self.assertEqual((c_429["kind"], c_429["scope"]), ("rate_limit", "model"))

        c_quota = availability.classify("codex", "gpt-4o", "quota exceeded for account")
        self.assertEqual((c_quota["kind"], c_quota["scope"]), ("quota", "client"))

        c_credits = availability.classify("opencode", "zen/m1", "insufficient credits on balance")
        self.assertEqual((c_credits["kind"], c_credits["scope"]), ("quota", "provider"))

        self.health.record("opencode", "zen/m1", success=False, message="insufficient credits on balance", at=100.0)
        eligible_zen, _ = self.health.eligible({"client": "opencode", "model": "zen/m2"}, at=105.0)
        self.assertFalse(eligible_zen)
        eligible_other, _ = self.health.eligible({"client": "opencode", "model": "openrouter/m3"}, at=105.0)
        self.assertTrue(eligible_other)

    def test_generic_access_word_in_404_or_timeout_does_not_trigger_403_permission(self):
        res_404 = availability.classify("opencode", "zen/m1", "HTTP 404: model access not found")
        self.assertEqual(res_404["kind"], "not_found")

        res_timeout = availability.classify("opencode", "zen/m1", "network access timed out")
        self.assertEqual(res_timeout["kind"], "timeout")

    def test_catalog_fresh_excludes_missing_model_and_stale_or_empty_preserves_eligibility(self):
        candidate_missing = {"client": "opencode", "model": "zen/unknown"}
        candidate_present = {"client": "opencode", "model": "zen/m1"}

        eligible_empty, _ = self.health.eligible(candidate_missing, at=500.0)
        self.assertTrue(eligible_empty)

        self.health.catalog("opencode", "zen", ["zen/m1", "zen/m2"], at=1000.0, ttl=3600)
        eligible_fresh_missing, _ = self.health.eligible(candidate_missing, at=1000.0)
        self.assertFalse(eligible_fresh_missing)
        eligible_fresh_present, _ = self.health.eligible(candidate_present, at=1000.0)
        self.assertTrue(eligible_fresh_present)

        eligible_stale_missing, _ = self.health.eligible(candidate_missing, at=4601.0)
        self.assertTrue(eligible_stale_missing)

    def test_validate_output_json_python_and_truncation(self):
        with self.assertRaises(ValueError):
            runtime.validate_output('{"key": "val"}', {"response_format": "json"}, {"finish_reason": "length"})

        valid_json = runtime.validate_output(
            '{"answer": 42}',
            {"response_format": "json", "required_keys": ["answer"]},
            {},
        )
        self.assertEqual(json.loads(valid_json), {"answer": 42})

        with self.assertRaises(ValueError):
            runtime.validate_output(
                '{"other": 42}',
                {"response_format": "json", "required_keys": ["answer"]},
                {},
            )

        fenced_code = "```python\ndef run():\n    return 1\n```"
        stripped = runtime.validate_output(fenced_code, {"response_format": "python"}, {})
        self.assertEqual(stripped, "def run():\n    return 1")

    def test_aggregate_usage_distinguishes_complete_partial_and_unavailable(self):
        self.assertEqual(runtime.aggregate_usage([])["accounting_status"], "unavailable")
        self.assertEqual(
            runtime.aggregate_usage([{"usage": {"total_tokens": 150}}])["accounting_status"],
            "partial",
        )
        complete = runtime.aggregate_usage([
            {"usage": {"total_tokens": 100, "cost": 0.002}},
            {"usage": {"total_tokens": 50, "cost": 0.001}},
        ])
        self.assertEqual(complete["accounting_status"], "complete")
        self.assertEqual(complete["total_tokens"], 150)
        self.assertAlmostEqual(complete["cost"], 0.003)


class McpAndApiParityTests(unittest.TestCase):
    def test_coordinator_delegate_passes_exclude_clients(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir) / "data"
            project = Path(tmp_dir) / "project"
            project.mkdir()
            with Coordinator(root) as coord:
                task = coord.new_task(project, "Test task")
                with patch("ai_coordinator.api._run_worker", return_value={"status": "complete"}) as mock_run:
                    coord.delegate(
                        task["id"],
                        "code",
                        "print('hello')",
                        exclude_clients=["claude"],
                    )
                self.assertTrue(mock_run.called)
                passed_config = mock_run.call_args.args[1]
                self.assertIn("claude", passed_config.get("disabled_worker_clients", []))


class CliVersionTests(unittest.TestCase):
    def test_cli_parser_version_flag(self):
        import io
        from contextlib import redirect_stdout
        from ai_coordinator.cli import parser
        p = parser()
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            p.parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)
        output = out.getvalue().strip()
        self.assertIn("ai-coordinator", output)
        self.assertIn("0.4.0", output)

    def test_cli_subprocess_version_flag(self):
        import subprocess, sys
        res = subprocess.run(
            [sys.executable, "-c", "from ai_coordinator.cli import parser; parser().parse_args(['--version'])"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("ai-coordinator 0.4.0", res.stdout.strip())


if __name__ == "__main__":
    unittest.main()
