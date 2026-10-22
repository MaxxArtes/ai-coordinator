"""Acceptance tests for Phase 4A: unified knowledge, access, provenance and decisions."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_coordinator.knowledge import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeKind,
    KnowledgePrincipal,
    KnowledgeState,
    KnowledgeTransitionError,
    KnowledgeVisibility,
    UnifiedKnowledgeCore,
)
from ai_coordinator.storage import Store


class UnifiedKnowledgeCoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.core = UnifiedKnowledgeCore(self.db)
        self.alice = KnowledgePrincipal("alice", "human", frozenset(("project-a", "project-b")), True, True)
        self.bob = KnowledgePrincipal("bob", "human", frozenset(("project-a",)))
        self.agent_a = KnowledgePrincipal("agent-a", "agent", frozenset(("project-a",)))
        self.agent_b = KnowledgePrincipal("agent-b", "agent", frozenset(("project-b",)))
        self.unscoped = KnowledgePrincipal("no-project", "agent")

    def tearDown(self):
        self.db.close()

    def _decision(self, name="Usar SQLite WAL", visibility=KnowledgeVisibility.PROJECT_SHARED):
        return self.core.capture(
            self.alice, "project-a", KnowledgeKind.ARCHITECTURE_DECISION,
            name, visibility=visibility, tags=("architecture", "sqlite"),
            context={"engine": "sqlite", "runtime": "python"},
            source_kind="human",
        )

    def _accept(self, entry):
        proposed = self.core.transition(
            self.alice, entry["id"], expected_revision=1,
            new_state=KnowledgeState.PROPOSED, reason="Analisar proposta",
        )
        return self.core.transition(
            self.alice, entry["id"], expected_revision=proposed["revision"],
            new_state=KnowledgeState.ACCEPTED, reason="Decisao humana aprovada",
        )

    def test_01_private_note_only_author_can_read(self):
        private = self.core.capture(
            self.alice, "project-a", KnowledgeKind.THOUGHT, "Planejamento pessoal sensivel",
        )
        self.assertIsNotNone(self.core.get(self.alice, private["id"]))
        self.assertIsNone(self.core.get(self.bob, private["id"]))
        self.assertIsNone(self.core.get(self.agent_a, private["id"]))
        self.assertEqual(self.core.find(self.agent_a, query="pessoal"), [])

    def test_02_agent_cannot_create_private_human_entry(self):
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(self.agent_a, "project-a", KnowledgeKind.THOUGHT, "Minha nota")
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(self.unscoped, "project-a", KnowledgeKind.OBSERVATION, "Outra nota",
                              visibility=KnowledgeVisibility.PROJECT_SHARED)

    def test_03_project_isolation_before_search(self):
        self.core.capture(self.alice, "project-a", KnowledgeKind.OBSERVATION,
                          "Erro de compilacao raro", visibility=KnowledgeVisibility.PROJECT_SHARED)
        other = self.core.capture(self.alice, "project-b", KnowledgeKind.OBSERVATION,
                                  "Erro de compilacao raro", visibility=KnowledgeVisibility.PROJECT_SHARED)
        self.assertEqual(len(self.core.find(self.agent_a, query="compilacao")), 1)
        self.assertEqual(self.core.find(self.agent_b, query="compilacao")[0]["id"], other["id"])
        self.assertEqual(self.core.find(self.unscoped, query="compilacao"), [])

    def test_04_global_visibility_is_explicit(self):
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(self.agent_a, "project-a", KnowledgeKind.LESSON, "Licao global",
                              visibility=KnowledgeVisibility.GLOBAL_AUTHORIZED)
        global_note = self.core.capture(
            self.alice, "project-a", KnowledgeKind.LESSON, "Licao global",
            visibility=KnowledgeVisibility.GLOBAL_AUTHORIZED,
        )
        self.assertIsNone(self.core.get(self.agent_a, global_note["id"]))
        self.assertIsNotNone(self.core.get(self.alice, global_note["id"]))

    def test_05_human_approval_not_agent_self_approval(self):
        entry = self._decision()
        proposed = self.core.transition(self.alice, entry["id"], expected_revision=1,
                                        new_state=KnowledgeState.PROPOSED, reason="Revisao")
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.transition(self.agent_a, entry["id"], expected_revision=2,
                                 new_state=KnowledgeState.ACCEPTED, reason="Agente aprova")
        self.assertEqual(self.core.get(self.alice, entry["id"])["state"], KnowledgeState.PROPOSED)
        accepted = self.core.transition(self.alice, entry["id"], expected_revision=2,
                                        new_state=KnowledgeState.ACCEPTED, reason="Aceito")
        self.assertEqual(accepted["revision"], 3)
        self.assertEqual(len(self.core.events(self.alice, entry["id"])), 3)

    def test_06_superseding_requires_accepted_replacement_in_same_project(self):
        old = self._accept(self._decision("SQLite WAL para a fila"))
        replacement = self._decision("Banco nativo com jobs", visibility=KnowledgeVisibility.PROJECT_SHARED)
        with self.assertRaises(KnowledgeTransitionError):
            self.core.transition(self.alice, old["id"], expected_revision=3,
                                 new_state=KnowledgeState.SUPERSEDED, reason="Nova arquitetura",
                                 replacement_id=replacement["id"])
        new = self._accept(replacement)
        superseded = self.core.transition(
            self.alice, old["id"], expected_revision=3,
            new_state=KnowledgeState.SUPERSEDED, reason="Nova decisao validada",
            replacement_id=new["id"],
        )
        self.assertEqual(superseded["superseded_by"], new["id"])
        self.assertEqual(superseded["state"], KnowledgeState.SUPERSEDED)
        with self.assertRaises(KnowledgeTransitionError):
            self.core.transition(self.alice, old["id"], expected_revision=4,
                                 new_state=KnowledgeState.PROPOSED, reason="Voltar no tempo")

    def test_07_optimistic_revision_fencing(self):
        rec = self._decision()
        self.core.transition(self.alice, rec["id"], expected_revision=1,
                             new_state=KnowledgeState.PROPOSED, reason="Primeira")
        with self.assertRaises(KnowledgeConflict):
            self.core.transition(self.alice, rec["id"], expected_revision=1,
                                 new_state=KnowledgeState.ARCHIVED, reason="Conflito")
        self.assertEqual(self.core.get(self.alice, rec["id"])["revision"], 2)

    def test_08_context_incompatible_is_not_reused(self):
        self._decision()
        good = self.core.find(self.alice, query="sqlite", context={"engine": "sqlite"})
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0]["context_match"], "confirmed")
        bad = self.core.find(self.alice, query="sqlite", context={"engine": "postgres"})
        self.assertEqual(bad, [])

    def test_09_missing_context_is_explicitly_unconfirmed(self):
        entry = self.core.capture(self.alice, "project-a", KnowledgeKind.LESSON,
                                  "Processos precisam checkpoint", visibility=KnowledgeVisibility.PROJECT_SHARED)
        matching = self.core.find(self.alice, query="checkpoint", context={"os": "windows"})
        self.assertEqual(matching[0]["id"], entry["id"])
        self.assertEqual(matching[0]["context_match"], "unconfirmed")

    def test_10_events_are_append_only(self):
        entry = self._decision()
        ev = self.core.events(self.alice, entry["id"])[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM knowledge_events WHERE id=?", (ev["id"],))
        self.db.rollback()

    def test_11_source_id_is_unique_per_project_and_source(self):
        self.core.capture(self.alice, "project-a", KnowledgeKind.THOUGHT, "Uma ideia",
                          source_kind="notion", source_id="page-123")
        with self.assertRaises(KnowledgeConflict):
            self.core.capture(self.alice, "project-a", KnowledgeKind.THOUGHT, "Outra ideia",
                              source_kind="notion", source_id="page-123")
        self.assertEqual(len(self.core.find(self.alice)), 1)

    def test_12_links_do_not_disclose_private_target(self):
        private = self.core.capture(self.alice, "project-a", KnowledgeKind.THOUGHT, "Nota reservada")
        shared = self._decision()
        self.core.link(self.alice, shared["id"], private["id"], "inspired_by")
        self.assertEqual(len(self.core.links(self.alice, shared["id"])), 1)
        self.assertEqual(self.core.links(self.agent_a, shared["id"]), [])
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.link(self.agent_a, shared["id"], private["id"], "inspired_by")

    def test_13_existing_outer_transaction_is_not_committed(self):
        self.db.execute("BEGIN IMMEDIATE")
        created = self.core.capture(self.alice, "project-a", KnowledgeKind.THOUGHT, "Pensamento transacional")
        self.assertIsNotNone(self.core.get(self.alice, created["id"]))
        self.db.rollback()
        self.assertIsNone(self.core.get(self.alice, created["id"]))

    def test_14_only_decisions_can_be_accepted(self):
        rec = self.core.capture(self.alice, "project-a", KnowledgeKind.HYPOTHESIS,
                                "Talvez mudar a rota", visibility=KnowledgeVisibility.PROJECT_SHARED)
        self.core.transition(self.alice, rec["id"], expected_revision=1,
                             new_state=KnowledgeState.PROPOSED, reason="Avaliar")
        with self.assertRaises(KnowledgeTransitionError):
            self.core.transition(self.alice, rec["id"], expected_revision=2,
                                 new_state=KnowledgeState.ACCEPTED, reason="Nao virar decisao")

    def test_15_store_exposes_knowledge_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            try:
                self.assertIsInstance(store.knowledge_core, UnifiedKnowledgeCore)
                human = KnowledgePrincipal("owner", "human", frozenset(("project-x",)))
                record = store.knowledge_core.capture(human, "project-x", KnowledgeKind.THOUGHT, "Captura local")
                self.assertEqual(store.knowledge_core.get(human, record["id"])["content"], "Captura local")
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
