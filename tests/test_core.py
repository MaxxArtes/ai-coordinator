from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

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
        legacy = sqlite3.connect(self.root / "coordination.sqlite")
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


if __name__ == "__main__":
    unittest.main()
