"""Acceptance tests for Phase 4A: unified knowledge, access, provenance and decisions."""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_coordinator.invariants import InvariantOracle, UntrustedEvidenceError
from ai_coordinator.knowledge import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeEvidenceLevel,
    KnowledgeKind,
    KnowledgeNature,
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
        self.runs = {
            ("task-integration-99", "run-101"): {"status": "complete", "exit_code": 0},
            ("task-find-20", "run-202"): {"status": "complete", "exit_code": 0},
        }
        self.oracle = InvariantOracle(
            run_lookup=lambda task, run: self.runs.get((task, run)),
            secret="supervisor_test_secret_32_bytes_long!",
        )
        self.core = UnifiedKnowledgeCore(self.db, oracle=self.oracle)
        self.alice = KnowledgePrincipal("alice", "human", frozenset(("project-a", "project-b")), True, True)
        self.bob = KnowledgePrincipal("bob", "human", frozenset(("project-a",)))
        self.agent_a = KnowledgePrincipal("agent-a", "agent", frozenset(("project-a",)))
        self.agent_b = KnowledgePrincipal("agent-b", "agent", frozenset(("project-b",)))
        self.supervisor = KnowledgePrincipal("supervisor", "supervisor", frozenset(("project-a", "project-b")), True, True)
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
        self.assertEqual(len(self.core.find(self.alice, include_speculative=True)), 1)

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

    def test_16_three_independent_dimensions(self):
        # 1. Thought captured defaults to speculative_draft and unverified
        thought = self.core.capture(self.alice, "project-a", KnowledgeKind.THOUGHT, "Ideia preliminar")
        self.assertEqual(thought["state"], KnowledgeState.CAPTURED)
        self.assertEqual(thought["nature"], KnowledgeNature.SPECULATIVE_DRAFT)
        self.assertEqual(thought["evidence_level"], KnowledgeEvidenceLevel.UNVERIFIED)

        # 2. Decision captured defaults to decision_record and unverified
        decision = self._decision("Adotar PostgreSQL no servico de relatorios")
        self.assertEqual(decision["state"], KnowledgeState.CAPTURED)
        self.assertEqual(decision["nature"], KnowledgeNature.DECISION_RECORD)
        self.assertEqual(decision["evidence_level"], KnowledgeEvidenceLevel.UNVERIFIED)

        # 3. Accepted by human: state is accepted, nature is decision_record, evidence is human_reviewed (NEVER oracle_verified)
        accepted = self._accept(decision)
        self.assertEqual(accepted["state"], KnowledgeState.ACCEPTED)
        self.assertEqual(accepted["nature"], KnowledgeNature.DECISION_RECORD)
        self.assertEqual(accepted["evidence_level"], KnowledgeEvidenceLevel.HUMAN_REVIEWED)
        self.assertNotEqual(accepted["evidence_level"], KnowledgeEvidenceLevel.ORACLE_VERIFIED)

    def test_17_worker_and_human_cannot_forge_oracle_verified(self):
        # 1. Capture cannot directly declare oracle_verified, validated_finding or human_reviewed
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(
                self.agent_a, "project-a", KnowledgeKind.OBSERVATION, "Teste passou",
                evidence_level=KnowledgeEvidenceLevel.ORACLE_VERIFIED,
                visibility=KnowledgeVisibility.PROJECT_SHARED,
            )
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(
                self.agent_a, "project-a", KnowledgeKind.OBSERVATION, "Fato provado",
                nature=KnowledgeNature.VALIDATED_FINDING,
                visibility=KnowledgeVisibility.PROJECT_SHARED,
            )
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(
                self.agent_a, "project-a", KnowledgeKind.OBSERVATION, "Auto declaracao",
                evidence_level=KnowledgeEvidenceLevel.HUMAN_REVIEWED,
                visibility=KnowledgeVisibility.PROJECT_SHARED,
            )

        # Human cannot capture as oracle_verified directly
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(
                self.alice, "project-a", KnowledgeKind.OBSERVATION, "Teste manual",
                evidence_level=KnowledgeEvidenceLevel.ORACLE_VERIFIED,
            )

        # Supervisor cannot bypass capture either: all entries start unverified
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.capture(
                self.supervisor, "project-a", KnowledgeKind.OBSERVATION, "Direto supervisor",
                evidence_level=KnowledgeEvidenceLevel.ORACLE_VERIFIED,
                visibility=KnowledgeVisibility.PROJECT_SHARED,
            )

        # 2. Transition cannot directly set oracle_verified or validated_finding
        rec = self._decision("Mudar cache")
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.transition(
                self.alice, rec["id"], expected_revision=1,
                new_state=KnowledgeState.PROPOSED, reason="Tentativa invalida",
                new_evidence_level=KnowledgeEvidenceLevel.ORACLE_VERIFIED,
            )
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.transition(
                self.alice, rec["id"], expected_revision=1,
                new_state=KnowledgeState.PROPOSED, reason="Tentativa invalida",
                new_nature=KnowledgeNature.VALIDATED_FINDING,
            )

        # Agent cannot transition to human_reviewed
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.transition(
                self.agent_a, rec["id"], expected_revision=1,
                new_state=KnowledgeState.PROPOSED, reason="Tentativa agente",
                new_evidence_level=KnowledgeEvidenceLevel.HUMAN_REVIEWED,
            )

        # 3. Neither worker nor human can call verify_with_oracle
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.verify_with_oracle(
                self.agent_a, rec["id"], expected_revision=1,
                oracle_token="token-com-mais-de-16-caracteres",
                verification_evidence={"task_id": "task-test-01"},
            )
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.verify_with_oracle(
                self.alice, rec["id"], expected_revision=1,
                oracle_token="token-com-mais-de-16-caracteres",
                verification_evidence={"task_id": "task-test-01"},
            )

        # 4. Supervisor calling verify_with_oracle with arbitrary forged token is rejected by InvariantOracle
        with self.assertRaises(UntrustedEvidenceError):
            self.core.verify_with_oracle(
                self.supervisor, rec["id"], expected_revision=1,
                oracle_token="arbitrary_token_12345678",
                verification_evidence={
                    "task_id": "task-test-01",
                    "plan_revision": 1,
                    "source": "trusted_test_runner",
                },
            )

        # 5. Token generated with untrusted key is rejected
        from ai_coordinator.invariants import generate_verification_token
        rogue_token = generate_verification_token(
            task_id="task-test-01",
            plan_revision=1,
            invariant_id=rec["id"],
            source="trusted_test_runner",
            secret="rogue_untrusted_secret_key",
        )
        with self.assertRaises(UntrustedEvidenceError):
            self.core.verify_with_oracle(
                self.supervisor, rec["id"], expected_revision=1,
                oracle_token=rogue_token,
                verification_evidence={
                    "task_id": "task-test-01",
                    "plan_revision": 1,
                    "source": "trusted_test_runner",
                },
            )

        # 6. Non-existent run_id is rejected by InvariantOracle
        with self.assertRaises(UntrustedEvidenceError):
            self.core.verify_with_oracle(
                self.supervisor, rec["id"], expected_revision=1,
                oracle_token="fake_token_1234567890",
                verification_evidence={
                    "task_id": "task-test-01",
                    "run_id": "fake-run-999",
                    "plan_revision": 1,
                    "source": "trusted_test_runner",
                },
            )

    def test_18_supervisor_verify_with_oracle_success(self):
        task_id = "task-integration-99"
        run_id = "run-101"
        rec = self.core.capture(
            self.alice, "project-a", KnowledgeKind.OBSERVATION,
            "Sincronizacao de processos com teste concorrente",
            task_id=task_id,
            visibility=KnowledgeVisibility.PROJECT_SHARED,
        )
        # Generate authentic HMAC-SHA256 token signed with the supervisor oracle secret
        valid_token = self.oracle.issue_evidence_token(
            task_id=task_id,
            plan_revision=1,
            invariant_id=rec["id"],
            source="trusted_test_runner",
            run_id=run_id,
            exit_code=0,
        )
        evidence = {
            "task_id": task_id,
            "run_id": run_id,
            "plan_revision": 1,
            "invariant_id": rec["id"],
            "source": "trusted_test_runner",
            "exit_code": 0,
            "command": "pytest -k concurrency",
        }
        verified = self.core.verify_with_oracle(
            self.supervisor, rec["id"], expected_revision=1,
            oracle_token=valid_token,
            verification_evidence=evidence,
            nature=KnowledgeNature.VALIDATED_FINDING,
            reason="Oraculo confirmou execucao com sucesso",
        )
        self.assertEqual(verified["evidence_level"], KnowledgeEvidenceLevel.ORACLE_VERIFIED)
        self.assertEqual(verified["nature"], KnowledgeNature.VALIDATED_FINDING)
        self.assertIn("oracle_verification", verified["evidence"])
        self.assertEqual(verified["evidence"]["oracle_verification"]["token"], valid_token)
        self.assertEqual(verified["evidence"]["oracle_verification"]["verified_by"], "supervisor")
        self.assertEqual(verified["evidence"]["oracle_verification"]["task_id"], task_id)
        self.assertEqual(verified["evidence"]["oracle_verification"]["run_id"], run_id)

        events = self.core.events(self.supervisor, rec["id"])
        self.assertEqual(events[-1]["event_type"], "oracle_verify")

    def test_19_retrocompatible_schema_migration(self):
        # Simulate legacy database created without nature and evidence_level
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE knowledge_entries (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_id TEXT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                author_id TEXT NOT NULL,
                author_role TEXT NOT NULL,
                visibility TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                source_kind TEXT NOT NULL,
                source_id TEXT,
                tags_json TEXT NOT NULL,
                context_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                superseded_by TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO knowledge_entries VALUES
                ('legacy-thought', 'project-a', NULL, 'thought', 'Ideia antiga', 'alice', 'human',
                 'private_human', 'captured', 1, 'manual', NULL, '[]', '{}', '{}',
                 '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', NULL),
                ('legacy-decision', 'project-a', NULL, 'decision', 'Decisao antiga', 'alice', 'human',
                 'project_shared', 'accepted', 1, 'manual', NULL, '[]', '{}', '{}',
                 '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', NULL),
                ('legacy-obs', 'project-a', NULL, 'observation', 'Observacao antiga', 'alice', 'human',
                 'project_shared', 'captured', 1, 'manual', NULL, '[]', '{}', '{}',
                 '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', NULL)"""
        )
        conn.commit()

        # Instantiate core; should migrate schema and add missing columns with retrocompatible classification
        core = UnifiedKnowledgeCore(conn)

        thought_row = core.get(self.alice, "legacy-thought")
        self.assertIsNotNone(thought_row)
        self.assertEqual(thought_row["nature"], KnowledgeNature.SPECULATIVE_DRAFT)
        self.assertEqual(thought_row["evidence_level"], KnowledgeEvidenceLevel.UNVERIFIED)

        decision_row = core.get(self.alice, "legacy-decision")
        self.assertIsNotNone(decision_row)
        self.assertEqual(decision_row["nature"], KnowledgeNature.DECISION_RECORD)
        self.assertEqual(decision_row["evidence_level"], KnowledgeEvidenceLevel.UNVERIFIED)

        obs_row = core.get(self.alice, "legacy-obs")
        self.assertIsNotNone(obs_row)
        self.assertEqual(obs_row["nature"], KnowledgeNature.OBSERVATION)
        self.assertEqual(obs_row["evidence_level"], KnowledgeEvidenceLevel.UNVERIFIED)

        conn.close()

    def test_20_find_filters_by_nature_and_evidence_level(self):
        # 1. Thought: nature is speculative_draft
        self.core.capture(self.alice, "project-a", KnowledgeKind.THOUGHT, "Rascunho A",
                          visibility=KnowledgeVisibility.PROJECT_SHARED)

        # 2. Decision: nature is decision_record
        dec = self._decision("Decisao B")
        self._accept(dec)

        # 3. Observation: verified by oracle -> nature is validated_finding, evidence is oracle_verified
        task_id = "task-find-20"
        run_id = "run-202"
        obs = self.core.capture(self.alice, "project-a", KnowledgeKind.OBSERVATION, "Evidencia C",
                                task_id=task_id, visibility=KnowledgeVisibility.PROJECT_SHARED)
        token = self.oracle.issue_evidence_token(
            task_id=task_id,
            plan_revision=1,
            invariant_id=obs["id"],
            source="trusted_test_runner",
            run_id=run_id,
            exit_code=0,
        )
        self.core.verify_with_oracle(
            self.supervisor, obs["id"], expected_revision=1,
            oracle_token=token,
            verification_evidence={
                "task_id": task_id,
                "run_id": run_id,
                "plan_revision": 1,
                "invariant_id": obs["id"],
                "source": "trusted_test_runner",
                "exit_code": 0,
            },
        )

        # Default find() excludes speculative_draft from operational context!
        operational = self.core.find(self.alice, project_id="project-a")
        self.assertEqual(len(operational), 2)
        operational_natures = {r["nature"] for r in operational}
        self.assertNotIn(KnowledgeNature.SPECULATIVE_DRAFT, operational_natures)
        self.assertIn(KnowledgeNature.DECISION_RECORD, operational_natures)
        self.assertIn(KnowledgeNature.VALIDATED_FINDING, operational_natures)

        # Explicit include_speculative=True returns all 3 entries
        all_entries = self.core.find(self.alice, project_id="project-a", include_speculative=True)
        self.assertEqual(len(all_entries), 3)

        # Explicit natures filter
        drafts = self.core.find(self.alice, project_id="project-a", natures=[KnowledgeNature.SPECULATIVE_DRAFT])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["nature"], KnowledgeNature.SPECULATIVE_DRAFT)

        decisions = self.core.find(self.alice, project_id="project-a", natures=[KnowledgeNature.DECISION_RECORD])
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["nature"], KnowledgeNature.DECISION_RECORD)

        oracle_verified = self.core.find(self.alice, project_id="project-a", evidence_levels=[KnowledgeEvidenceLevel.ORACLE_VERIFIED])
        self.assertEqual(len(oracle_verified), 1)
        self.assertEqual(oracle_verified[0]["evidence_level"], KnowledgeEvidenceLevel.ORACLE_VERIFIED)


if __name__ == "__main__":
    unittest.main()
