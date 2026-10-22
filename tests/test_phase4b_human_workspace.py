"""Tests for Phase 4B.1 - Human Workspace (Human Inbox, Privacy, Lifecycle, Audit Log, and Worker Isolation)."""

import os
import sqlite3
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
        self.root = self.temp_dir.name
        self.store = Store(self.root)
        self.ws = self.store.human_workspace

        self.alice = self.store.create_human_principal("alice")
        self.bob = self.store.create_human_principal("bob")

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
                self.alice, item["id"], new_state=HumanInboxState.CONVERTED, expected_revision=1
            )

        # 3. triaged -> converted (valid)
        converted = self.ws.transition_state(
            self.alice, item["id"], new_state=HumanInboxState.CONVERTED, expected_revision=2,
            reason="Convertido para tarefa de backlog",
        )
        self.assertEqual(converted["state"], HumanInboxState.CONVERTED)
        self.assertEqual(converted["revision"], 3)

        # 4. converted -> captured is illegal!
        with self.assertRaises(ValueError):
            self.ws.transition_state(
                self.alice, item["id"], new_state=HumanInboxState.CAPTURED, expected_revision=3
            )

        # 5. converted -> discarded (valid)
        discarded = self.ws.transition_state(
            self.alice, item["id"], new_state=HumanInboxState.DISCARDED, expected_revision=3,
            reason="Descartado apos analise",
        )
        self.assertEqual(discarded["state"], HumanInboxState.DISCARDED)
        self.assertEqual(discarded["revision"], 4)

        # 6. discarded -> captured (re-opening is valid)
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
        valid_token = self.store.issue_human_session_token("alice")
        authenticated_alice = HumanPrincipal("alice", role="human", session_token=valid_token)
        self.ws.verify_principal(authenticated_alice, require_session_token=True)

        # Forged token is rejected
        forged_alice = HumanPrincipal("alice", role="human", session_token="hsess_alice_forged_digest_12345678")
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_forge:
            self.ws.verify_principal(forged_alice, require_session_token=True)
        self.assertIn("invalido ou falsificado", str(ctx_forge.exception))

        # Mismatched owner token (Bob's token used for Alice) is rejected
        bob_token = self.store.issue_human_session_token("bob")
        alice_with_bob_token = HumanPrincipal("alice", role="human", session_token=bob_token)
        with self.assertRaises(HumanWorkspaceAccessDenied) as ctx_mismatch:
            self.ws.verify_principal(alice_with_bob_token, require_session_token=True)
        self.assertIn("proprietario divergente", str(ctx_mismatch.exception))


if __name__ == "__main__":
    unittest.main()
