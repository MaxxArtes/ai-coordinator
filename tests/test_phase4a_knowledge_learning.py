"""Phase 4A.1/4A.2 — provenance, negative evidence and human feedback tests."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_coordinator import (
    Store, KnowledgeLearning, KnowledgePrincipal, KnowledgeVisibility,
    KnowledgeAccessDenied, KnowledgeConflict,
)
from ai_coordinator.experience import (
    ExperienceRecord, ExperienceAuthor, ExperienceCategory, ExperienceVisibility,
)


class LearningFromPitaiATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state")
        self.core = self.store.knowledge_learning
        self.owner = KnowledgePrincipal("owner", "human", frozenset(("alpha", "beta")))
        self.worker = KnowledgePrincipal("worker", "agent", frozenset(("alpha",)))
        self.other = KnowledgePrincipal("other", "agent", frozenset(("beta",)))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def _experiment(self, visibility=KnowledgeVisibility.PROJECT_SHARED):
        return self.core.propose_experiment(
            self.owner, "alpha",
            "Trocar sleep por sincronizacao explicita no teste concorrente",
            "Teste E2E passa repetidamente em Linux e Windows",
            "Falha no teste de estresse ou regressao de processos",
            visibility=visibility, tags=("concorrencia", "ci"),
            context={"os": "windows", "runtime": "python"},
        )

    def test_01_store_exposes_learning(self):
        self.assertIsInstance(self.store.knowledge_learning, KnowledgeLearning)

    def test_02_protocol_preregistered_and_sealed(self):
        e = self._experiment()
        protocol = e["context"]["experiment_protocol"]
        self.assertIn("falsification", protocol)
        self.assertEqual(len(e["evidence"]["protocol_sha256"]), 64)
        with self.assertRaises(ValueError):
            self.core.propose_experiment(
                self.owner, "alpha", "hipotese", "sucesso", "falha",
                context={"experiment_protocol": {"override": True}},
            )

    def test_03_unreviewed_observations_never_rank_as_proven(self):
        e = self._experiment()
        obs = self.core.record_observation(
            self.worker, e["id"], outcome="success", summary="O worker reportou sucesso",
            evidence_refs=("ci/run-42",),
        )
        self.assertGreater(obs, 0)
        self.assertEqual(self.core.reviewed_precedents(self.worker, query="sincronizacao"), [])
        self.assertEqual(self.core.timeline(self.owner, e["id"])[0]["outcome"], "success")

    def test_04_negative_result_can_be_reviewed_and_retrieved(self):
        e = self._experiment()
        obs = self.core.record_observation(
            self.worker, e["id"], outcome="failed",
            summary="O novo metodo introduziu race condition",
            evidence_refs=("ci/run-failed",),
        )
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.review_observation(
                self.worker, e["id"], observation_id=obs,
                decision="accepted", reason="Aprovar indevidamente",
            )
        self.core.review_observation(
            self.owner, e["id"], observation_id=obs, decision="accepted",
            reason="Revisao humana conferiu os logs",
        )
        records = self.core.reviewed_precedents(
            self.worker, query="sincronizacao",
            context={"os": "windows", "runtime": "python"},
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"], "failed")
        self.assertEqual(records[0]["context_match"], "confirmed")
        self.assertEqual(records[0]["proof_level"], "human_reviewed_not_oracle_verified")

    def test_05_no_duplicate_or_cross_experiment_review(self):
        e = self._experiment()
        other = self._experiment()
        obs = self.core.record_observation(
            self.worker, e["id"], outcome="inconclusive",
            summary="Amostra insuficiente", evidence_refs=(),
        )
        with self.assertRaises(KnowledgeConflict):
            self.core.review_observation(
                self.owner, other["id"], observation_id=obs,
                decision="accepted", reason="Engano de entrada",
            )
        self.core.review_observation(
            self.owner, e["id"], observation_id=obs,
            decision="rejected", reason="Precisamos repetir o ensaio",
        )
        with self.assertRaises(KnowledgeConflict):
            self.core.review_observation(
                self.owner, e["id"], observation_id=obs,
                decision="accepted", reason="Tentativa de sobrescrever",
            )

    def test_06_private_experiment_is_not_visible_to_workers(self):
        e = self._experiment(visibility=KnowledgeVisibility.PRIVATE_HUMAN)
        self.assertIsNone(self.store.knowledge_core.get(self.worker, e["id"]))
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.record_observation(
                self.worker, e["id"], outcome="success",
                summary="Acesso indevido", evidence_refs=(),
            )
        self.assertEqual(self.core.reviewed_precedents(self.worker, query="sincronizacao"), [])

    def test_07_scope_filtered_before_looking_at_results(self):
        e = self._experiment()
        self.assertEqual(self.core.timeline(self.owner, e["id"]), [])
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.timeline(self.other, e["id"])
        self.assertEqual(self.core.reviewed_precedents(self.other, query="sincronizacao"), [])

    def test_08_events_are_append_only(self):
        e = self._experiment()
        eid = self.core.record_observation(
            self.worker, e["id"], outcome="failed",
            summary="falha confirmavel", evidence_refs=("log/run",),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("UPDATE knowledge_experiment_events SET outcome='success' WHERE id=?", (eid,))
        self.store.db.rollback()

    def test_09_legacy_import_idempotent_and_no_fake_approval(self):
        legacy = self.store.experience_store.record_experience(
            project_id="alpha", problem_pattern="Checkpoint corrompido",
            solution_pattern="Restaurar ultimo valido com CRC",
            category=ExperienceCategory.TOOL_FIX,
            context_tags=["ckpt"], visibility=ExperienceVisibility.PROJECT_SHARED,
        )
        first = self.core.import_legacy_experience(self.owner, legacy)
        again = self.core.import_legacy_experience(self.owner, legacy)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(first["state"], "captured")
        self.assertEqual(first["source_id"], legacy.id)
        self.assertEqual(first["evidence"]["provenance_status"], "imported_unverified")
        total = self.store.db.execute(
            "SELECT count(*) FROM knowledge_entries WHERE source_kind='experience_store'"
        ).fetchone()[0]
        self.assertEqual(total, 1)
        self.assertIsNotNone(self.store.experience_store.get_experience(legacy.id))
        self.assertEqual(self.core.reviewed_precedents(self.worker, query="checkpoint"), [])

    def test_10_changed_legacy_payload_rejected(self):
        legacy = self.store.experience_store.record_experience(
            project_id="alpha", problem_pattern="Bug original",
            solution_pattern="Correcao original",
        )
        self.core.import_legacy_experience(self.owner, legacy)
        legacy.solution_pattern = "Uma solucao divergente"
        with self.assertRaises(KnowledgeConflict):
            self.core.import_legacy_experience(self.owner, legacy)

    def test_11_private_legacy_cannot_be_imported_by_agent(self):
        record = ExperienceRecord(
            id="legacy-private", project_id="alpha", task_id=None,
            category=ExperienceCategory.USER_PREFERENCE,
            problem_pattern="Preferencia pessoal", solution_pattern="Manter privado",
            author=ExperienceAuthor.HUMAN,
            visibility=ExperienceVisibility.PRIVATE_HUMAN,
        )
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.import_legacy_experience(self.worker, record)
        created = self.core.import_legacy_experience(self.owner, record)
        self.assertIsNone(self.store.knowledge_core.get(self.worker, created["id"]))
        self.assertIsNotNone(self.store.knowledge_core.get(self.owner, created["id"]))

    def test_12_out_of_scope_import_rejected(self):
        record = ExperienceRecord(
            id="legacy-other", project_id="beta", task_id=None,
            category=ExperienceCategory.SOLUTION_PATTERN,
            problem_pattern="bug", solution_pattern="fix",
        )
        with self.assertRaises(KnowledgeAccessDenied):
            self.core.import_legacy_experience(self.worker, record)

    def test_13_malformed_evidence_digest_rejected(self):
        e = self._experiment()
        with self.assertRaises(ValueError):
            self.core.record_observation(
                self.worker, e["id"], outcome="success",
                summary="resultado", evidence_refs=(), artifact_sha256="falso",
            )

    def test_14_missing_context_never_claimed_confirmed(self):
        e = self.core.propose_experiment(
            self.owner, "alpha", "Hipotese sobre retry em pipeline",
            "Passa no teste", "Desvio de estado",
        )
        obs = self.core.record_observation(
            self.worker, e["id"], outcome="success",
            summary="Passou no ensaio", evidence_refs=("ci/run-7",),
        )
        self.core.review_observation(
            self.owner, e["id"], observation_id=obs,
            decision="accepted", reason="Conferi o resultado",
        )
        res = self.core.reviewed_precedents(
            self.worker, query="retry", context={"os": "windows"},
        )
        self.assertEqual(res[0]["context_match"], "unconfirmed")


if __name__ == "__main__":
    unittest.main()
