"""Tests for Phase 4B.1 - Human Workspace (Human Inbox, Privacy, Lifecycle, Audit Log, and Worker Isolation)."""

import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_coordinator.storage import Store
from ai_coordinator.human_workspace import (
    HumanWorkspace,
    HumanPrincipal,
    HumanInboxState,
    HumanInboxReadState,
    HumanInboxItemKind,
    HumanWorkspaceError,
    HumanWorkspaceAccessDenied,
    HumanWorkspaceConflict,
    HumanWorkspaceNotFoundError,
)


class HumanWorkspaceInboxTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = str(Path(self.temp_dir.name).resolve())
        self.store = Store(self.root)
        self.ws = self.store.human_workspace

        self.alice = self.store.create_human_principal("alice", supervisor_key=self.store.supervisor_key)
        self.bob = self.store.create_human_principal("bob", supervisor_key=self.store.supervisor_key)

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # --- 1. Schema and Idempotent Migrations ---
    def test_01_schema_idempotency_and_table_existence(self):
        # Multiple init_schema calls should be completely safe and idempotent
        self.ws._init_schema()
        self.ws._init_schema()

        # Check tables in sqlite_master
        tables = {
            r[0]
            for r in self.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("human_inbox", tables)
        self.assertIn("human_inbox_events", tables)

        # Check triggers
        triggers = {
            r[0]
            for r in self.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        self.assertIn("trg_prevent_update_human_inbox_events", triggers)
        self.assertIn("trg_prevent_delete_human_inbox_events", triggers)

    # --- 2. Capture and Initial State ---
    def test_02_capture_creates_record_with_expected_defaults(self):
        item = self.ws.capture(
            self.alice,
            "Pensamento inicial sobre a arquitetura do workspace",
            item_kind=HumanInboxItemKind.THOUGHT,
            project_id="project-phoenix",
            tags=["arquitetura", "fase4b"],
            context={"ui": "terminal", "mode": "focus"},
        )

        self.assertTrue(item["id"].startswith("inbox_"))
        self.assertEqual(item["owner_id"], "alice")
        self.assertEqual(item["project_id"], "project-phoenix")
        self.assertEqual(item["kind"], HumanInboxItemKind.THOUGHT)
        self.assertEqual(item["content"], "Pensamento inicial sobre a arquitetura do workspace")
        self.assertEqual(item["tags"], ["arquitetura", "fase4b"])
        self.assertEqual(item["context"], {"ui": "terminal", "mode": "focus"})
        self.assertEqual(item["state"], HumanInboxState.CAPTURED)
        self.assertEqual(item["read_state"], HumanInboxReadState.UNREAD)
        self.assertEqual(item["revision"], 1)
        self.assertIsNotNone(item["created_at"])
        self.assertIsNotNone(item["updated_at"])

    def test_03_capture_validation_and_secret_scrubbing(self):
        # Empty content should be rejected
        with self.assertRaises(ValueError):
            self.ws.capture(self.alice, "   ")

        # Invalid kind should be rejected
        with self.assertRaises(ValueError):
            self.ws.capture(self.alice, "Texto valido", item_kind="unsupported_kind")

        # Content with secrets should be scrubbed
        item = self.ws.capture(
            self.alice,
            "Chave de teste: sk-1234567890abcdef1234567890abcdef no inbox",
        )
        self.assertNotIn("sk-1234567890abcdef1234567890abcdef", item["content"])
        self.assertIn("[REDACTED]", item["content"])

    # --- 3. Privacy and Isolation between Owners ---
    def test_04_strict_owner_privacy_on_get(self):
        alice_item = self.ws.capture(self.alice, "Ideia super secreta da Alice")

        # Alice can read her own item
        retrieved = self.ws.get(self.alice, alice_item["id"])
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["id"], alice_item["id"])

        # Bob CANNOT read Alice's item -> HumanWorkspaceAccessDenied
        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.get(self.bob, alice_item["id"])

        # Non-existent item returns None for owner
        self.assertIsNone(self.ws.get(self.alice, "inbox_nonexistent_id"))

    def test_05_project_id_does_not_expand_visibility(self):
        # Alice captures an item associated with shared project 'project-shared'
        alice_shared = self.ws.capture(
            self.alice,
            "Nota da Alice no projeto compartilhado",
            project_id="project-shared",
        )

        # Bob captures an item in the same project
        bob_shared = self.ws.capture(
            self.bob,
            "Nota do Bob no mesmo projeto",
            project_id="project-shared",
        )

        # Querying by project_id strictly filters by owner_id
        alice_list = self.ws.list_inbox(self.alice, project_id="project-shared")
        self.assertEqual(len(alice_list), 1)
        self.assertEqual(alice_list[0]["id"], alice_shared["id"])

        bob_list = self.ws.list_inbox(self.bob, project_id="project-shared")
        self.assertEqual(len(bob_list), 1)
        self.assertEqual(bob_list[0]["id"], bob_shared["id"])

        # Bob cannot see Alice's item even specifying project_id
        bob_ids = {item["id"] for item in bob_list}
        self.assertNotIn(alice_shared["id"], bob_ids)

    # --- 4. Separation of Read State and Lifecycle ---
    def test_06_read_state_transitions_independently(self):
        item = self.ws.capture(self.alice, "Item para teste de leitura")
        self.assertEqual(item["read_state"], HumanInboxReadState.UNREAD)
        self.assertEqual(item["revision"], 1)

        # Mark as read
        read_item = self.ws.mark_read_state(
            self.alice, item["id"], read_state=HumanInboxReadState.READ, expected_revision=1
        )
        self.assertEqual(read_item["read_state"], HumanInboxReadState.READ)
        self.assertEqual(read_item["state"], HumanInboxState.CAPTURED)  # Lifecycle unchanged!
        self.assertEqual(read_item["revision"], 2)

        # Mark back as unread
        unread_item = self.ws.mark_read_state(
            self.alice, item["id"], read_state=HumanInboxReadState.UNREAD, expected_revision=2
        )
        self.assertEqual(unread_item["read_state"], HumanInboxReadState.UNREAD)
        self.assertEqual(unread_item["revision"], 3)

    def test_07_lifecycle_transitions_with_optimistic_fencing(self):
        item = self.ws.capture(self.alice, "Item de ciclo de vida")

        # 1. captured -> triaged (valid)
        triaged = self.ws.transition_state(
            self.alice, item["id"], new_state=HumanInboxState.TRIAGED, expected_revision=1,
            reason="Item avaliado pelo usuario",
        )
        self.assertEqual(triaged["state"], HumanInboxState.TRIAGED)
        self.assertEqual(triaged["revision"], 2)

        # 2. Concurrency conflict on stale revision
        with self.assertRaises(HumanWorkspaceConflict):
            self.ws.transition_state(
                self.alice, item["id"], new_state=HumanInboxState.DISCARDED, expected_revision=1
            )

        # 3. Direct transition to converted is strictly forbidden (must use convert_to_task)
        with self.assertRaises(ValueError) as ctx_conv:
            self.ws.transition_state(
                self.alice, item["id"], new_state=HumanInboxState.CONVERTED, expected_revision=2
            )
        self.assertIn("utilize convert_to_task()", str(ctx_conv.exception).lower())

        # 4. Use convert_to_task to legally convert the item to a backlog task
        conv_res = self.store.convert_inbox_item_to_task(
            self.alice, item["id"], project=self.root, expected_revision=2
        )
        converted = conv_res["inbox_item"]
        self.assertEqual(converted["state"], HumanInboxState.CONVERTED)
        self.assertEqual(converted["revision"], 3)
        self.assertIsNotNone(converted["converted_task_id"])

        # 5. converted -> captured is illegal!
        with self.assertRaises(ValueError):
            self.ws.transition_state(
                self.alice, item["id"], new_state=HumanInboxState.CAPTURED, expected_revision=3
            )

        # 6. converted -> discarded (valid)
        discarded = self.ws.transition_state(
            self.alice, item["id"], new_state=HumanInboxState.DISCARDED, expected_revision=3,
            reason="Descartado apos analise",
        )
        self.assertEqual(discarded["state"], HumanInboxState.DISCARDED)
        self.assertEqual(discarded["revision"], 4)

        # 7. discarded -> captured (re-opening is valid)
        reopened = self.ws.transition_state(
            self.alice, item["id"], new_state=HumanInboxState.CAPTURED, expected_revision=4,
            reason="Restaurado para captura ativa",
        )
        self.assertEqual(reopened["state"], HumanInboxState.CAPTURED)
        self.assertEqual(reopened["revision"], 5)

    # --- 5. Content and Metadata Updates ---
    def test_08_update_content_and_metadata(self):
        item = self.ws.capture(self.alice, "Texto rascunho", tags=["draft"])

        # Update content
        updated_content = self.ws.update_content(
            self.alice, item["id"], content="Texto revisado e polido", expected_revision=1
        )
        self.assertEqual(updated_content["content"], "Texto revisado e polido")
        self.assertEqual(updated_content["revision"], 2)

        # Update metadata (project and tags)
        updated_meta = self.ws.update_metadata(
            self.alice, item["id"], project_id="new-project", tags=["draft", "final"], expected_revision=2
        )
        self.assertEqual(updated_meta["project_id"], "new-project")
        self.assertEqual(updated_meta["tags"], ["draft", "final"])
        self.assertEqual(updated_meta["revision"], 3)

        # Bob cannot update Alice's item
        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.update_content(
                self.bob, item["id"], content="Ataque do Bob", expected_revision=3
            )

    # --- 6. Append-Only Audit Logging and Immutability Triggers ---
    def test_09_audit_trail_and_trigger_immutability(self):
        item = self.ws.capture(self.alice, "Registro de auditoria")
        self.ws.mark_read_state(self.alice, item["id"], read_state=HumanInboxReadState.READ, expected_revision=1)
        self.ws.transition_state(self.alice, item["id"], new_state=HumanInboxState.TRIAGED, expected_revision=2)

        events = self.ws.events(self.alice, item["id"])
        self.assertEqual(len(events), 3)

        actions = [e["action"] for e in events]
        self.assertEqual(actions, ["capture", "mark_read", "transition"])

        # Bob cannot read Alice's audit events
        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.events(self.bob, item["id"])

        first_event_id = events[0]["id"]
        # Direct SQL UPDATE attempt on human_inbox_events MUST BE ABORTED by trigger
        with self.assertRaises(sqlite3.DatabaseError) as ctx_update:
            self.store.db.execute("UPDATE human_inbox_events SET action='tampered' WHERE id=?", (first_event_id,))
        self.assertIn("append-only", str(ctx_update.exception))
        self.store.db.rollback()

        # Direct SQL DELETE attempt on human_inbox_events MUST BE ABORTED by trigger
        with self.assertRaises(sqlite3.DatabaseError) as ctx_delete:
            self.store.db.execute("DELETE FROM human_inbox_events WHERE id=?", (first_event_id,))
        self.assertIn("append-only", str(ctx_delete.exception))
        self.store.db.rollback()

    # --- 7. Security: Worker Lockout and Cryptographic Session Verification ---
    def test_10_worker_lockout_by_role_and_environment(self):
        # 1. Principal with role='worker' is rejected
        worker_principal = HumanPrincipal("worker_bot", role="worker")
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_role:
            self.ws.capture(worker_principal, "Tentativa de escrita por worker")
        self.assertIn("worker", str(ctx_role.exception).lower())

        # 2. Process with AI_COORDINATOR_ROLE='worker' is rejected even with human principal
        with patch.dict(os.environ, {"AI_COORDINATOR_ROLE": "worker"}):
            with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_env:
                self.ws.capture(self.alice, "Tentativa em processo de worker")
            self.assertIn("workers subordinados", str(ctx_env.exception).lower())

            with self.assertRaises(HumanWorkspaceAccessDenied):
                self.ws.list_inbox(self.alice)

    def test_11_cryptographic_session_token_validation(self):
        # Valid token generated by coordinator Store
        valid_token = self.store.issue_human_session_token("alice", supervisor_key=self.store.supervisor_key)
        authenticated_alice = HumanPrincipal("alice", role="human", session_token=valid_token)
        self.ws.verify_principal(authenticated_alice, require_session_token=True)

        # Forged token is rejected
        forged_alice = HumanPrincipal("alice", role="human", session_token="hsess_alice_forged_digest_12345678")
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_forge:
            self.ws.verify_principal(forged_alice, require_session_token=True)
        self.assertIn("invalido ou falsificado", str(ctx_forge.exception))

        # Mismatched owner token (Bob's token used for Alice) is rejected
        bob_token = self.store.issue_human_session_token("bob", supervisor_key=self.store.supervisor_key)
        alice_with_bob_token = HumanPrincipal("alice", role="human", session_token=bob_token)
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_mismatch:
            self.ws.verify_principal(alice_with_bob_token, require_session_token=True)
        self.assertIn("proprietario divergente", str(ctx_mismatch.exception))


class HumanWorkspaceSecurity4B1STests(unittest.TestCase):
    """Hardened security tests for Phase 4B.1-S (mandatory tokens, anti-impersonation, supervisor key fencing)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = self.temp_dir.name
        self.store = Store(self.root)
        self.ws = self.store.human_workspace
        self.alice = self.store.create_human_principal("alice", supervisor_key=self.store.supervisor_key)
        self.bob = self.store.create_human_principal("bob", supervisor_key=self.store.supervisor_key)

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_s01_naked_principal_without_token_strictly_rejected(self):
        # HumanPrincipal without session token (session_token=None)
        naked_alice = HumanPrincipal("alice", role="human", session_token=None)

        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_cap:
            self.ws.capture(naked_alice, "Tentativa sem token")
        self.assertIn("token de sessao autenticado e obrigatorio", str(ctx_cap.exception).lower())

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.get(naked_alice, "inbox_12345")

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.list_inbox(naked_alice)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.transition_state(naked_alice, "inbox_12345", new_state=HumanInboxState.TRIAGED, expected_revision=1)

    def test_s02_empty_session_token_strictly_rejected(self):
        empty_alice = HumanPrincipal("alice", role="human", session_token="")
        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.capture(empty_alice, "Tentativa com token vazio")

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.list_inbox(empty_alice)

    def test_s03_token_issuance_requires_valid_supervisor_key(self):
        # Empty supervisor key rejected
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_empty:
            self.store.issue_human_session_token("alice", supervisor_key="")
        self.assertIn("chave de supervisor ausente ou invalida", str(ctx_empty.exception).lower())

        # Forged supervisor key rejected
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_wrong:
            self.store.issue_human_session_token("alice", supervisor_key="untrusted_key_12345")
        self.assertIn("chave de supervisor ausente ou invalida", str(ctx_wrong.exception).lower())

        # Calling create_human_principal without supervisor key rejected
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_create:
            self.store.create_human_principal("alice")
        self.assertIn("chave de supervisor obrigatoria", str(ctx_create.exception).lower())

    def test_s04_worker_cannot_issue_session_token_even_with_key(self):
        with patch.dict(os.environ, {"AI_COORDINATOR_ROLE": "worker"}):
            with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_worker:
                self.store.issue_human_session_token("alice", supervisor_key=self.store.supervisor_key)
            self.assertIn("workers subordinados estao proibidos", str(ctx_worker.exception).lower())

    def test_s05_impersonation_attack_across_all_inbox_operations(self):
        # Capture a real item as Alice
        alice_item = self.ws.capture(self.alice, "Item autentico da Alice")

        # Attacker tries to use Bob's valid token under Alice's owner_id
        bob_token = self.store.issue_human_session_token("bob", supervisor_key=self.store.supervisor_key)
        impostor_alice = HumanPrincipal("alice", role="human", session_token=bob_token)

        # Every single operation must detect owner divergence and abort
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_cap:
            self.ws.capture(impostor_alice, "Ataque de escrita com token de outro usuario")
        self.assertIn("proprietario divergente", str(ctx_cap.exception).lower())

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.get(impostor_alice, alice_item["id"])

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.list_inbox(impostor_alice)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.mark_read_state(impostor_alice, alice_item["id"], read_state=HumanInboxReadState.READ, expected_revision=1)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.transition_state(impostor_alice, alice_item["id"], new_state=HumanInboxState.TRIAGED, expected_revision=1)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.update_content(impostor_alice, alice_item["id"], content="Conteudo adulterado", expected_revision=1)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.update_metadata(impostor_alice, alice_item["id"], project_id="malicious-proj", expected_revision=1)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.events(impostor_alice, alice_item["id"])

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.convert_to_task(impostor_alice, alice_item["id"], store=self.store, project=self.root)

    def test_s06_forged_hmac_token_across_all_inbox_operations(self):
        alice_item = self.ws.capture(self.alice, "Item autentico para teste de token forjado")
        forged_alice = HumanPrincipal("alice", role="human", session_token=f"hsess_alice_{'0'*64}")

        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_cap:
            self.ws.capture(forged_alice, "Tentativa de escrita com token falso")
        self.assertIn("invalido ou falsificado", str(ctx_cap.exception).lower())

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.get(forged_alice, alice_item["id"])

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.list_inbox(forged_alice)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.mark_read_state(forged_alice, alice_item["id"], read_state=HumanInboxReadState.READ, expected_revision=1)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.transition_state(forged_alice, alice_item["id"], new_state=HumanInboxState.TRIAGED, expected_revision=1)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.update_content(forged_alice, alice_item["id"], content="Conteudo adulterado", expected_revision=1)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.events(forged_alice, alice_item["id"])

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.ws.convert_to_task(forged_alice, alice_item["id"], store=self.store, project=self.root)


class HumanWorkspaceConversion4B2Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = str(Path(self.temp_dir.name).resolve())
        self.store = Store(self.root)
        self.ws = self.store.human_workspace

        self.alice = self.store.create_human_principal("alice", supervisor_key=self.store.supervisor_key)
        self.bob = self.store.create_human_principal("bob", supervisor_key=self.store.supervisor_key)

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_01_successful_conversion_to_backlog_task(self):
        item = self.ws.capture(
            self.alice,
            "Implementar telemetria de performance no worker",
            item_kind=HumanInboxItemKind.TODO,
            project_id=self.root,
            tags=["telemetria", "performance"],
        )
        self.assertEqual(item["state"], HumanInboxState.CAPTURED)
        self.assertEqual(item["revision"], 1)
        self.assertIsNone(item["converted_task_id"])

        result = self.store.convert_inbox_item_to_task(
            self.alice,
            item["id"],
            expected_revision=1,
        )

        self.assertTrue(result["created"])
        task = result["task"]
        inbox_item = result["inbox_item"]

        # Verify task attributes
        self.assertTrue(task["id"])
        self.assertEqual(task["status"], "backlog")
        self.assertEqual(task["goal"], "Implementar telemetria de performance no worker")
        self.assertEqual(task["project"], self.root)

        # Backlog tasks must NOT appear in Store.context(project) as active tasks
        active_contexts = self.store.context(self.root)
        self.assertEqual(len(active_contexts), 0)

        # Verify inbox item updated state
        self.assertEqual(inbox_item["state"], HumanInboxState.CONVERTED)
        self.assertEqual(inbox_item["converted_task_id"], task["id"])
        self.assertEqual(inbox_item["revision"], 2)

        # Verify audit events
        events = self.ws.events(self.alice, item["id"])
        self.assertEqual(len(events), 2)  # capture + convert_to_task
        conv_event = events[1]
        self.assertEqual(conv_event["action"], "convert_to_task")
        self.assertEqual(conv_event["actor_id"], "alice")
        self.assertEqual(conv_event["old_state"], HumanInboxState.CAPTURED)
        self.assertEqual(conv_event["new_state"], HumanInboxState.CONVERTED)
        self.assertEqual(conv_event["details"]["task_id"], task["id"])
        self.assertEqual(conv_event["details"]["status"], "backlog")

    def test_02_conversion_custom_goal_and_project_override(self):
        item = self.ws.capture(
            self.alice,
            "Ideia preliminar sem projeto associado",
            item_kind=HumanInboxItemKind.IDEA,
        )
        self.assertIsNone(item["project_id"])

        result = self.store.convert_inbox_item_to_task(
            self.alice,
            item["id"],
            project=self.root,
            goal="Refinamento formal: arquitetura de fallback",
            expected_revision=1,
        )

        self.assertTrue(result["created"])
        self.assertEqual(result["task"]["goal"], "Refinamento formal: arquitetura de fallback")
        self.assertEqual(result["task"]["project"], self.root)
        self.assertEqual(result["inbox_item"]["state"], HumanInboxState.CONVERTED)

    def test_03_strict_idempotency_second_conversion_returns_existing(self):
        item = self.ws.capture(
            self.alice,
            "Tarefa de infraestrutura idempotente",
            project_id=self.root,
        )

        # First conversion
        res1 = self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=1)
        self.assertTrue(res1["created"])
        task_id = res1["task"]["id"]

        # Retry with revision before conversion (network retry pattern)
        res2 = self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=1)
        self.assertFalse(res2["created"])
        self.assertEqual(res2["task"]["id"], task_id)

        # Retry with current revision
        res3 = self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=2)
        self.assertFalse(res3["created"])
        self.assertEqual(res3["task"]["id"], task_id)

        # Call without expected_revision
        res4 = self.store.convert_inbox_item_to_task(self.alice, item["id"])
        self.assertFalse(res4["created"])
        self.assertEqual(res4["task"]["id"], task_id)

        # Ensure no duplicate tasks were created in SQLite
        task_count = self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        self.assertEqual(task_count, 1)

        # Ensure no duplicate events were logged
        events = self.ws.events(self.alice, item["id"])
        conv_events = [e for e in events if e["action"] == "convert_to_task"]
        self.assertEqual(len(conv_events), 1)

    def test_04_optimistic_concurrency_conflict(self):
        item = self.ws.capture(self.alice, "Item concorrente", project_id=self.root)

        with self.assertRaises(HumanWorkspaceConflict):
            self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=99)

        # Item must remain untouched in captured state
        fresh = self.ws.get(self.alice, item["id"])
        self.assertEqual(fresh["state"], HumanInboxState.CAPTURED)
        self.assertEqual(fresh["revision"], 1)
        self.assertIsNone(fresh["converted_task_id"])

        task_count = self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        self.assertEqual(task_count, 0)

    def test_05_atomic_rollback_on_failure(self):
        item = self.ws.capture(self.alice, "Item com rollback atomico", project_id=self.root)

        # Monkeypatch _record_event to raise an exception after task creation
        original_record_event = self.ws._record_event

        def faulty_record_event(*args, **kwargs):
            if kwargs.get("action") == "convert_to_task":
                raise RuntimeError("Falha simulada no registro de auditoria")
            return original_record_event(*args, **kwargs)

        self.ws._record_event = faulty_record_event
        try:
            with self.assertRaises(RuntimeError):
                self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=1)
        finally:
            self.ws._record_event = original_record_event

        # Verify full rollback: task was NOT committed
        task_count = self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        self.assertEqual(task_count, 0)

        # Verify filesystem integrity: zero orphaned task directories exist on disk
        tasks_dir = Path(self.root) / "tasks"
        task_dirs = list(tasks_dir.iterdir()) if tasks_dir.is_dir() else []
        self.assertEqual(len(task_dirs), 0, "No orphaned task directory should exist on disk after rollback")

        # Verify inbox item remains untouched
        fresh = self.ws.get(self.alice, item["id"])
        self.assertEqual(fresh["state"], HumanInboxState.CAPTURED)
        self.assertEqual(fresh["revision"], 1)
        self.assertIsNone(fresh["converted_task_id"])

        # Audit events must only contain the capture event
        events = self.ws.events(self.alice, item["id"])
        self.assertEqual(len(events), 1)

    def test_06_privacy_isolation_bob_cannot_convert_alice_item(self):
        item = self.ws.capture(self.alice, "Item privado da Alice", project_id=self.root)

        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.store.convert_inbox_item_to_task(self.bob, item["id"], expected_revision=1)

        fresh = self.ws.get(self.alice, item["id"])
        self.assertEqual(fresh["state"], HumanInboxState.CAPTURED)
        self.assertIsNone(fresh["converted_task_id"])

    def test_07_worker_lockout_cannot_convert(self):
        item = self.ws.capture(self.alice, "Item com bloqueio de worker", project_id=self.root)

        # 1. Environment-based worker lockout
        with patch.dict(os.environ, {"AI_COORDINATOR_ROLE": "worker"}):
            with self.assertRaises(HumanWorkspaceAccessDenied):
                self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=1)

        # 2. Role-based worker lockout
        worker_principal = HumanPrincipal("alice", role="worker", session_token=self.alice.session_token)
        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.store.convert_inbox_item_to_task(worker_principal, item["id"], expected_revision=1)

    def test_08_discarded_item_conversion_rejected(self):
        item = self.ws.capture(self.alice, "Item a ser descartado", project_id=self.root)
        self.ws.transition_state(
            self.alice,
            item["id"],
            new_state=HumanInboxState.DISCARDED,
            expected_revision=1,
        )

        with self.assertRaises(HumanWorkspaceError) as ctx:
            self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=2)
        self.assertIn("descartado nao pode ser convertido", str(ctx.exception).lower())

    def test_09_reentrant_nested_transaction_with_savepoint(self):
        item = self.ws.capture(self.alice, "Item transacional aninhado", project_id=self.root)

        # Call within an outer store.transaction()
        with self.store.transaction():
            res = self.store.convert_inbox_item_to_task(self.alice, item["id"], expected_revision=1)
            self.assertTrue(res["created"])

        # Verified committed after outer transaction closes
        fresh = self.ws.get(self.alice, item["id"])
        self.assertEqual(fresh["state"], HumanInboxState.CONVERTED)
        self.assertEqual(self.store.task(res["task"]["id"])["status"], "backlog")

        # Now test that rollback of outer transaction rolls back the nested conversion
        item2 = self.ws.capture(self.alice, "Item que sofrera rollback externo", project_id=self.root)
        try:
            with self.store.transaction():
                self.store.convert_inbox_item_to_task(self.alice, item2["id"], expected_revision=1)
                raise ValueError("Rollback forcado da transacao externa")
        except ValueError:
            pass

        fresh2 = self.ws.get(self.alice, item2["id"])
        self.assertEqual(fresh2["state"], HumanInboxState.CAPTURED)
        self.assertIsNone(fresh2["converted_task_id"])

        # Outer rollback check: verify that NO state.json or mirror folder was left on disk after outer rollback
        tasks_on_disk = [
            d.name
            for d in (Path(self.root) / "tasks").iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
        self.assertEqual(tasks_on_disk, [res["task"]["id"]])

    def test_10_unique_partial_index_prevents_duplicate_converted_task_id(self):
        item1 = self.ws.capture(self.alice, "Item um", project_id=self.root)
        item2 = self.ws.capture(self.alice, "Item dois", project_id=self.root)

        # Convert item1
        res1 = self.store.convert_inbox_item_to_task(self.alice, item1["id"], expected_revision=1)
        task_id = res1["task"]["id"]

        # Attempt to directly point item2 to the same task_id via SQL should violate UNIQUE constraint
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute(
                "UPDATE human_inbox SET converted_task_id = ? WHERE id = ?",
                (task_id, item2["id"]),
            )

    def test_11_rejection_of_non_backlog_status(self):
        item = self.ws.capture(self.alice, "Item com status restrito", project_id=self.root)

        with self.assertRaises(ValueError) as ctx_active:
            self.store.convert_inbox_item_to_task(self.alice, item["id"], status="active")
        self.assertIn("backlog", str(ctx_active.exception).lower())

        with self.assertRaises(ValueError) as ctx_complete:
            self.store.convert_inbox_item_to_task(self.alice, item["id"], status="completed")
        self.assertIn("backlog", str(ctx_complete.exception).lower())

    def test_12_concurrent_conversion_from_distinct_connections(self):
        # Capture item using test fixture store
        item = self.ws.capture(self.alice, "Anotacao concorrente multithread", project_id=self.root)

        results = []
        errors = []

        # Each thread creates its own independent Store connection to SQLite WAL
        def worker_convert():
            worker_store = Store(self.root)
            try:
                principal = worker_store.create_human_principal("alice", supervisor_key=worker_store.supervisor_key)
                res = worker_store.convert_inbox_item_to_task(principal, item["id"], expected_revision=1)
                results.append(res)
            except Exception as e:
                errors.append(e)
            finally:
                worker_store.db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(worker_convert)
            f2 = executor.submit(worker_convert)
            f1.result()
            f2.result()

        # Both threads must complete without unhandled database errors
        self.assertEqual(len(errors), 0, f"Unexpected errors during concurrent conversion: {errors}")
        self.assertEqual(len(results), 2)

        # Both threads returned the exact same task_id
        task_ids = {r["task"]["id"] for r in results}
        self.assertEqual(len(task_ids), 1)

        # Exactly one thread created it; the other handled it idempotently
        created_flags = [r["created"] for r in results]
        self.assertIn(True, created_flags)
        self.assertIn(False, created_flags)

        # SQLite database contains exactly 1 task
        task_count = self.store.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        self.assertEqual(task_count, 1)

        # human_inbox must have exactly 1 event of type convert_to_task
        events = self.ws.events(self.alice, item["id"])
        conv_events = [e for e in events if e["action"] == "convert_to_task"]
        self.assertEqual(len(conv_events), 1)

    def test_13_orphan_task_mirror_reconciliation(self):
        # Inject an orphaned task folder with artifacts into the filesystem
        tasks_dir = Path(self.root) / "tasks"
        orphan_dir = tasks_dir / "orphan_fake_task_9999"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        (orphan_dir / "state.json").write_text("{\"corrupted\": true}", encoding="utf-8")
        (orphan_dir / "execution.log").write_text("critical execution logs to preserve", encoding="utf-8")

        self.assertTrue(orphan_dir.is_dir())

        # Reconciliation must move the orphan to quarantine, NOT delete it
        cleaned = self.store.cleanup_orphaned_task_mirrors()
        self.assertIn("orphan_fake_task_9999", cleaned)
        self.assertFalse(orphan_dir.exists())

        # Verify evidence preservation in quarantine
        quarantine_tasks = Path(self.root) / "quarantine" / "tasks"
        self.assertTrue(quarantine_tasks.is_dir())
        quarantined_dirs = [d for d in quarantine_tasks.iterdir() if d.name.startswith("orphan_fake_task_9999")]
        self.assertEqual(len(quarantined_dirs), 1)
        target_qdir = quarantined_dirs[0]
        self.assertTrue((target_qdir / "execution.log").exists())
        self.assertEqual((target_qdir / "execution.log").read_text(encoding="utf-8"), "critical execution logs to preserve")

    def test_14_reconcile_repairs_missing_or_stale_task_mirrors(self):
        # Create a legitimate task
        task = self.store.new(project=self.root, goal="Tarefa a ser testada para reparo de espelhamento")
        task_id = task["id"]
        state_file = Path(self.root) / "tasks" / task_id / "state.json"
        self.assertTrue(state_file.is_file())

        # 1. Simulate accidental loss of state.json
        state_file.unlink()
        self.assertFalse(state_file.exists())

        # Reconcile should repair missing mirror
        report = self.store.reconcile_task_mirrors()
        self.assertIn(task_id, report["repaired"])
        self.assertTrue(state_file.is_file())

        # 2. Simulate stale / corrupted mirror on disk
        state_file.write_text("{\"revision\": 9999, \"invalid\": true}", encoding="utf-8")
        report2 = self.store.reconcile_task_mirrors()
        self.assertIn(task_id, report2["repaired"])
        disk_data = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(disk_data["revision"], 1)
        self.assertEqual(disk_data["goal"], "Tarefa a ser testada para reparo de espelhamento")

    def test_15_reconcile_symlink_safety_and_invalid_folder_names(self):
        tasks_dir = Path(self.root) / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

        # 1. Non-matching directory names must be ignored (not quarantined)
        ignored_dir = tasks_dir / ".hidden_service_dir"
        ignored_dir.mkdir(parents=True, exist_ok=True)
        short_dir = tasks_dir / "short"
        short_dir.mkdir(parents=True, exist_ok=True)

        report = self.store.reconcile_task_mirrors()
        skipped_names = [s["name"] for s in report["skipped"]]
        self.assertIn(".hidden_service_dir", skipped_names)
        self.assertIn("short", skipped_names)
        self.assertTrue(ignored_dir.is_dir())
        self.assertTrue(short_dir.is_dir())

        # 2. Symlink protection: if symlink can be created, ensure it is safely skipped
        symlink_target = Path(self.root) / "external_target"
        symlink_target.mkdir(parents=True, exist_ok=True)
        (symlink_target / "keep.txt").write_text("safe", encoding="utf-8")
        symlink_path = tasks_dir / "symlink_fake_12345"
        try:
            os.symlink(str(symlink_target), str(symlink_path), target_is_directory=True)
            report_sym = self.store.reconcile_task_mirrors()
            sym_skipped = [s["name"] for s in report_sym["skipped"] if s.get("reason") == "symlink"]
            self.assertIn("symlink_fake_12345", sym_skipped)
            # Target must remain completely intact
            self.assertTrue(symlink_target.is_dir())
            self.assertTrue((symlink_target / "keep.txt").is_file())
        except (OSError, NotImplementedError):
            pass  # Windows without SeCreateSymbolicLinkPrivilege



class HumanFocusCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = str(Path(self.temp_dir.name).resolve())
        self.supervisor_key = "test_sup_secret_key_4b3"
        self.store = Store(self.root, supervisor_key=self.supervisor_key)
        self.ws = self.store.human_workspace

        self.alice = self.store.create_human_principal("alice", supervisor_key=self.supervisor_key)
        self.bob = self.store.create_human_principal("bob", supervisor_key=self.supervisor_key)

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # --- 1. Scoped Savepoint Rollback and Symlink Repair ---
    def test_01_savepoint_rollback_discards_nested_actions_cleanly(self):
        executed_actions = []

        with self.store.transaction():
            self.store.defer_post_commit(lambda: executed_actions.append("root_action"))

            # Enter an inner savepoint
            try:
                with self.ws._transaction(store=self.store):
                    self.store.defer_post_commit(lambda: executed_actions.append("nested_action_rolled_back"))
                    # Simulate an exception that triggers rollback of this savepoint
                    raise RuntimeError("simulated inner error")
            except RuntimeError:
                pass  # Savepoint rolled back

            # Another root-level action after inner rollback
            self.store.defer_post_commit(lambda: executed_actions.append("second_root_action"))

        # Outermost transaction committed. The rolled-back nested action must NOT have run!
        self.assertIn("root_action", executed_actions)
        self.assertIn("second_root_action", executed_actions)
        self.assertNotIn("nested_action_rolled_back", executed_actions)
        self.assertEqual(executed_actions, ["root_action", "second_root_action"])

    def test_02_reconcile_skips_symlink_task_dir_and_symlink_state_file(self):
        task = self.store.new(project=self.root, goal="Tarefa teste symlinks")
        tid = task["id"]
        tasks_dir = Path(self.root) / "tasks"
        task_dir = tasks_dir / tid
        state_file = task_dir / "state.json"

        # 1. State file as symlink
        external_state = Path(self.root) / "external_state.json"
        external_state.write_text("{\"revision\": 1}", encoding="utf-8")
        state_file.unlink()
        try:
            os.symlink(str(external_state), str(state_file))
            report = self.store.reconcile_task_mirrors()
            skipped = [s["name"] for s in report["skipped"] if s.get("reason") == "symlink_file"]
            self.assertIn(tid, skipped)
        except (OSError, NotImplementedError):
            pass

    # --- 2. Focus Checkpoint Persistence and Immutability ---
    def test_03_save_focus_checkpoint_schema_defaults_and_immutability(self):
        cp = self.store.save_focus_checkpoint(
            self.alice,
            project=self.root,
            intent="Definir checkpoints cognitivos imutaveis",
            decisions=["Briefing estritamente deterministico offline", "Append-only com triggers"],
            priorities=["Seguranca", "Conectividade Multi-IA"],
            blockers=["Nenhum"],
            open_questions=["Como lidar com workers?"],
            next_action="Escrever suite de testes da 4B.3",
        )

        self.assertTrue(cp["id"].startswith("fchk_"))
        self.assertEqual(cp["owner_id"], "alice")
        self.assertEqual(cp["project_id"], str(Path(self.root).resolve()))
        self.assertEqual(cp["intent"], "Definir checkpoints cognitivos imutaveis")
        self.assertEqual(len(cp["decisions"]), 2)
        self.assertEqual(len(cp["priorities"]), 2)
        self.assertEqual(cp["blockers"], ["Nenhum"])
        self.assertEqual(cp["open_questions"], ["Como lidar com workers?"])
        self.assertEqual(cp["next_action"], "Escrever suite de testes da 4B.3")
        self.assertEqual(cp["revision"], 1)
        self.assertIsNotNone(cp["created_at"])

        # Check triggers: UPDATE must be rejected by SQLite
        with self.assertRaises(sqlite3.IntegrityError if hasattr(sqlite3, "IntegrityError") else sqlite3.OperationalError):
            self.store.db.execute(
                "UPDATE human_focus_checkpoints SET intent = 'alterado' WHERE id = ?",
                (cp["id"],)
            )

        # DELETE must be rejected by SQLite
        with self.assertRaises(sqlite3.IntegrityError if hasattr(sqlite3, "IntegrityError") else sqlite3.OperationalError):
            self.store.db.execute(
                "DELETE FROM human_focus_checkpoints WHERE id = ?",
                (cp["id"],)
            )

    # --- 3. Revision Sequencing and Optimistic Concurrency ---
    def test_04_revision_sequencing_and_concurrency_fencing(self):
        # First checkpoint: revision 1
        cp1 = self.store.save_focus_checkpoint(
            self.alice,
            project=self.root,
            intent="Primeiro marco de foco",
        )
        self.assertEqual(cp1["revision"], 1)

        # Conflict check: expected_revision=0 or 5 should fail
        with self.assertRaises(HumanWorkspaceConflict):
            self.store.save_focus_checkpoint(
                self.alice,
                project=self.root,
                intent="Tentativa com revisao desatualizada",
                expected_revision=0,
            )

        with self.assertRaises(HumanWorkspaceConflict):
            self.store.save_focus_checkpoint(
                self.alice,
                project=self.root,
                intent="Tentativa com revisao futura",
                expected_revision=5,
            )

        # Expected revision 1 succeeds and produces revision 2
        cp2 = self.store.save_focus_checkpoint(
            self.alice,
            project=self.root,
            intent="Segundo marco de foco",
            expected_revision=1,
        )
        self.assertEqual(cp2["revision"], 2)

        # Latest should be cp2
        latest = self.store.get_latest_focus_checkpoint(self.alice, project=self.root)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], cp2["id"])
        self.assertEqual(latest["revision"], 2)

        # List should return both in descending order
        all_cps = self.store.list_focus_checkpoints(self.alice, project=self.root)
        self.assertEqual(len(all_cps), 2)
        self.assertEqual(all_cps[0]["revision"], 2)
        self.assertEqual(all_cps[1]["revision"], 1)

    # --- 4. Privacy, Tenant Isolation and Worker Lockout ---
    def test_05_privacy_and_tenant_isolation(self):
        self.store.save_focus_checkpoint(
            self.alice,
            project=self.root,
            intent="Privacidade absoluta de Alice",
        )

        # Bob queries the same project: gets None
        bob_latest = self.store.get_latest_focus_checkpoint(self.bob, project=self.root)
        self.assertIsNone(bob_latest)

        bob_list = self.store.list_focus_checkpoints(self.bob, project=self.root)
        self.assertEqual(bob_list, [])

    def test_06_worker_lockout_and_unauthorized_access(self):
        # 1. Principal with worker role is blocked
        worker_principal = HumanPrincipal(owner_id="worker_sub", role="worker")
        with self.assertRaises(HumanWorkspaceAccessDenied):
            self.store.save_focus_checkpoint(
                worker_principal,
                project=self.root,
                intent="Worker tentando gravar foco",
            )

        # 2. Process environment lockout
        with patch.dict(os.environ, {"AI_COORDINATOR_ROLE": "worker"}):
            with self.assertRaises(HumanWorkspaceAccessDenied):
                self.store.save_focus_checkpoint(
                    self.alice,
                    project=self.root,
                    intent="Foco sob processo worker",
                )
            with self.assertRaises(HumanWorkspaceAccessDenied):
                self.store.get_latest_focus_checkpoint(self.alice, project=self.root)
            with self.assertRaises(HumanWorkspaceAccessDenied):
                self.store.list_focus_checkpoints(self.alice, project=self.root)
            with self.assertRaises(HumanWorkspaceAccessDenied):
                self.store.resume_human_project(self.alice, project=self.root)

    # --- 5. Cross-Session Durability ---
    def test_07_cross_session_durability(self):
        cp = self.store.save_focus_checkpoint(
            self.alice,
            project=self.root,
            intent="Durabilidade entre sessoes",
            decisions=["Decisao salva"],
            next_action="Reiniciar store",
        )

        # Close existing store
        self.store.db.close()

        # Reopen brand new store pointing to same root and key
        new_store = Store(self.root, supervisor_key=self.supervisor_key)
        try:
            alice_new = new_store.create_human_principal("alice", supervisor_key=self.supervisor_key)
            loaded = new_store.get_latest_focus_checkpoint(alice_new, project=self.root)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["id"], cp["id"])
            self.assertEqual(loaded["intent"], "Durabilidade entre sessoes")
            self.assertEqual(loaded["decisions"], ["Decisao salva"])
            self.assertEqual(loaded["next_action"], "Reiniciar store")
        finally:
            new_store.db.close()

    # --- 6. Deterministic Offline Briefing ---
    def test_08_resume_human_project_offline_briefing(self):
        # Create a captured inbox item
        self.ws.capture(
            self.alice,
            "Lembrete importante no inbox",
            project_id=self.root,
        )

        # Save focus checkpoint
        self.store.save_focus_checkpoint(
            self.alice,
            project=self.root,
            intent="Concluir entrega da 4B.3",
            priorities=["Verificacao rigorosa", "Briefing sem leak"],
            next_action="Rodar suite completa de testes",
        )

        # Create active task
        task = self.store.new(project=self.root, goal="Entrega operacional da 4B.3")

        # Resume project briefing
        briefing = self.store.resume_human_project(self.alice, project=self.root)

        self.assertTrue(briefing["offline_mode"])
        self.assertEqual(briefing["owner_id"], "alice")
        self.assertEqual(briefing["project"], str(Path(self.root).resolve()))
        self.assertIsNotNone(briefing["human_focus"])
        self.assertEqual(briefing["human_focus"]["intent"], "Concluir entrega da 4B.3")
        self.assertIsNotNone(briefing["task_state"])
        self.assertEqual(briefing["task_state"]["id"], task["id"])
        self.assertEqual(briefing["task_state"]["goal"], "Entrega operacional da 4B.3")
        self.assertEqual(briefing["inbox_summary"]["pending_captured_count"], 1)

        # Briefing text structure
        bt = briefing["briefing_text"]
        self.assertIn("=== BRIEFING DE CONTINUIDADE", bt)
        self.assertIn("Modo: Offline (Zero chamadas externas de IA)", bt)
        self.assertIn("[FOCO COGNITIVO HUMANO", bt)
        self.assertIn("Concluir entrega da 4B.3", bt)
        self.assertIn("[ESTADO OPERACIONAL DA TAREFA", bt)
        self.assertIn("Entrega operacional da 4B.3", bt)
        self.assertIn("[INBOX HUMANO PENDENTE]: 1 notas", bt)

    # --- 7. CLI Subcommands Execution ---
    def test_09_cli_focus_commands(self):
        python_exe = sys.executable
        cli_module = "ai_coordinator.cli"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}

        # 1. focus save via CLI
        cmd_save = [
            python_exe, "-m", cli_module,
            "--root", self.root,
            "--supervisor-key", self.supervisor_key,
            "focus", "save",
            "--project", self.root,
            "--intent", "CLI Focus Test",
            "--next-action", "Validar CLI",
            "--owner", "alice",
        ]
        res_save = subprocess.run(cmd_save, capture_output=True, text=True, env=env)
        self.assertEqual(res_save.returncode, 0, f"CLI save failed: {res_save.stderr}")
        out_save = json.loads(res_save.stdout)
        self.assertEqual(out_save["intent"], "CLI Focus Test")
        self.assertEqual(out_save["next_action"], "Validar CLI")

        # 2. focus show via CLI
        cmd_show = [
            python_exe, "-m", cli_module,
            "--root", self.root,
            "--supervisor-key", self.supervisor_key,
            "focus", "show",
            "--project", self.root,
            "--owner", "alice",
        ]
        res_show = subprocess.run(cmd_show, capture_output=True, text=True, env=env)
        self.assertEqual(res_show.returncode, 0, f"CLI show failed: {res_show.stderr}")
        out_show = json.loads(res_show.stdout)
        self.assertEqual(out_show["intent"], "CLI Focus Test")

        # 3. focus resume via CLI
        cmd_resume = [
            python_exe, "-m", cli_module,
            "--root", self.root,
            "--supervisor-key", self.supervisor_key,
            "focus", "resume",
            "--project", self.root,
            "--owner", "alice",
        ]
        res_resume = subprocess.run(cmd_resume, capture_output=True, text=True, env=env)
        self.assertEqual(res_resume.returncode, 0, f"CLI resume failed: {res_resume.stderr}")
        out_resume = json.loads(res_resume.stdout)
        self.assertTrue(out_resume["offline_mode"])
        self.assertIn("CLI Focus Test", out_resume["briefing_text"])

        # 4. Worker lockout rejection via CLI
        worker_env = {**env, "AI_COORDINATOR_ROLE": "worker"}
        res_worker = subprocess.run(cmd_resume, capture_output=True, text=True, env=worker_env)
        self.assertEqual(res_worker.returncode, 2)
        out_worker = json.loads(res_worker.stdout)
        self.assertIn("error", out_worker)
        self.assertIn("Workers subordinados estao", out_worker["error"])



class Phase4B3SecurityHardeningTests(unittest.TestCase):
    """Hardened security tests for Phase 4B.3-S:
    - CLI Authentication & Session Protection (P0)
    - Cross-project task isolation and link validation
    - Accurate unbounded Inbox SQL aggregations
    - Tamper-proofing of persisted supervisor credentials
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = str(Path(self.temp_dir.name).resolve() / "store_data")
        self.proj_a = str(Path(self.temp_dir.name).resolve() / "project_alpha")
        self.proj_b = str(Path(self.temp_dir.name).resolve() / "project_beta")
        Path(self.proj_a).mkdir(parents=True, exist_ok=True)
        Path(self.proj_b).mkdir(parents=True, exist_ok=True)

        self.supervisor_key = "genuine_supervisor_master_key_4b3s"
        self.store = Store(self.root, supervisor_key=self.supervisor_key)
        self.ws = self.store.human_workspace

        self.alice = self.store.create_human_principal("alice", supervisor_key=self.supervisor_key)
        self.bob = self.store.create_human_principal("bob", supervisor_key=self.supervisor_key)

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # --- 1. Cross-Project Task Isolation in resume_human_project ---
    def test_01_resume_human_project_rejects_cross_project_task(self):
        # Create a task in Project Beta
        task_b = self.store.new(project=self.proj_b, goal="Tarefa exclusiva do Projeto Beta")

        # Attempt to resume Project Alpha requesting task from Project Beta -> must be denied
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx:
            self.store.resume_human_project(self.alice, project=self.proj_a, task_id=task_b["id"])
        self.assertIn("pertence ao projeto", str(ctx.exception).lower())

        # Non-existent task ID in Project Alpha -> NotFound
        with self.assertRaises(HumanWorkspaceNotFoundError):
            self.store.resume_human_project(self.alice, project=self.proj_a, task_id="non_existent_tid_123")

    # --- 2. Cross-Project Task Linking in save_focus_checkpoint ---
    def test_02_save_focus_checkpoint_rejects_cross_project_task_and_nonexistent(self):
        # Create a task in Project Beta
        task_b = self.store.new(project=self.proj_b, goal="Tarefa exclusiva do Projeto Beta")

        # Attempt to bind Project Beta's task to Project Alpha's focus checkpoint -> denied
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx:
            self.store.save_focus_checkpoint(
                self.alice,
                project=self.proj_a,
                intent="Tentativa de vincular tarefa de outro projeto",
                task_id=task_b["id"],
            )
        self.assertIn("incompativel", str(ctx.exception).lower())

        # Non-existent task ID -> NotFound
        with self.assertRaises(HumanWorkspaceNotFoundError):
            self.store.save_focus_checkpoint(
                self.alice,
                project=self.proj_a,
                intent="Tentativa com tarefa inexistente",
                task_id="ghost_task_999",
            )

        # Legitimate task in Project Alpha binds successfully
        task_a = self.store.new(project=self.proj_a, goal="Tarefa valida do Projeto Alpha")
        cp = self.store.save_focus_checkpoint(
            self.alice,
            project=self.proj_a,
            intent="Foco vinculado legitimamente",
            task_id=task_a["id"],
        )
        self.assertEqual(cp["task_id"], task_a["id"])

    # --- 3. Accurate Unbounded Inbox Aggregations (> 50 items) ---
    def test_03_resume_human_project_counts_inbox_beyond_list_limit(self):
        # Capture 65 items (exceeding the standard list_inbox limit of 50)
        # 40 will be marked as read, 25 remain unread
        for i in range(65):
            item = self.ws.capture(
                self.alice,
                f"Item de inbox numero {i}",
                project_id=self.proj_a,
            )
            if i < 40:
                self.ws.mark_read_state(
                    self.alice,
                    item["id"],
                    read_state=HumanInboxReadState.READ,
                    expected_revision=item["revision"],
                )

        briefing = self.store.resume_human_project(self.alice, project=self.proj_a)
        summary = briefing["inbox_summary"]

        self.assertEqual(summary["pending_captured_count"], 65)
        self.assertEqual(summary["unread_count"], 25)
        self.assertIn("[INBOX HUMANO PENDENTE]: 65 notas (25 nao lidas)", briefing["briefing_text"])

    # --- 4. CLI Privilege Separation and Impersonation Defense (P0) ---
    def test_04_cli_blocks_arbitrary_owner_impersonation_without_credentials(self):
        python_exe = sys.executable
        cli_module = "ai_coordinator.cli"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}

        # Caller attempts to select owner 'arbitrary_target_user' without providing supervisor key or session token
        cmd = [
            python_exe, "-m", cli_module,
            "--root", self.root,
            "focus", "resume",
            "--project", self.proj_a,
            "--owner", "arbitrary_target_user",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 2)
        out = json.loads(res.stdout)
        self.assertIn("error", out)
        self.assertIn("requer credencial de supervisor autenticada", out["error"])

    def test_05_cli_blocks_counterfeit_supervisor_key(self):
        python_exe = sys.executable
        cli_module = "ai_coordinator.cli"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}

        # Caller provides forged/wrong supervisor key
        cmd = [
            python_exe, "-m", cli_module,
            "--root", self.root,
            "--supervisor-key", "forged_wrong_key_12345",
            "focus", "resume",
            "--project", self.proj_a,
            "--owner", "alice",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 2)
        out = json.loads(res.stdout)
        self.assertIn("error", out)
        self.assertIn("chave de supervisor invalida", out["error"])

    def test_06_cli_credential_passing_via_files_and_env_vars(self):
        python_exe = sys.executable
        cli_module = "ai_coordinator.cli"
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}

        # 1. Using --supervisor-key-file
        key_file_path = Path(self.temp_dir.name) / "secret.key"
        key_file_path.write_text(self.supervisor_key, encoding="utf-8")

        cmd_file = [
            python_exe, "-m", cli_module,
            "--root", self.root,
            "--supervisor-key-file", str(key_file_path),
            "focus", "save",
            "--project", self.proj_a,
            "--intent", "Focus gravado via key file",
            "--owner", "alice",
        ]
        res_file = subprocess.run(cmd_file, capture_output=True, text=True, env=env)
        self.assertEqual(res_file.returncode, 0, f"Failed: {res_file.stderr}")
        out_file = json.loads(res_file.stdout)
        self.assertEqual(out_file["intent"], "Focus gravado via key file")

        # 2. Using --session-token-file
        alice_token = self.store.issue_human_session_token("alice", supervisor_key=self.supervisor_key)
        token_file_path = Path(self.temp_dir.name) / "session.token"
        token_file_path.write_text(alice_token, encoding="utf-8")

        cmd_token = [
            python_exe, "-m", cli_module,
            "--root", self.root,
            "--session-token-file", str(token_file_path),
            "focus", "show",
            "--project", self.proj_a,
            "--owner", "alice",
        ]
        res_token = subprocess.run(cmd_token, capture_output=True, text=True, env=env)
        self.assertEqual(res_token.returncode, 0, f"Failed: {res_token.stderr}")
        out_token = json.loads(res_token.stdout)
        self.assertEqual(out_token["intent"], "Focus gravado via key file")

    # --- 5. Persisted Credentials Tamper Protection ---
    def test_07_persisted_supervisor_key_tamper_protection(self):
        # A new Store instance opened pointing to same root with a fake supervisor_key
        # must preserve the canonical disk key and reject the fake key
        tampered_store = Store(self.root, supervisor_key="unauthorized_tampered_key")
        try:
            self.assertEqual(tampered_store.supervisor_key, self.supervisor_key)
            self.assertFalse(tampered_store._supervisor_key_matched)

            # Operations requiring supervisor authentication with the tampered key must fail
            with self.assertRaises(HumanWorkspaceAccessDenied):
                tampered_store.issue_human_session_token("alice", supervisor_key="unauthorized_tampered_key")
        finally:
            tampered_store.db.close()


if __name__ == "__main__":
    unittest.main()
