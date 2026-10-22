"""Phase 4A.1/4A.2 — provenance-aware experiential learning over UnifiedKnowledgeCore.

SQLite metadata/lineage and human-reviewed results, not neural fine-tuning.
The trusted coordinator must authenticate principals and isolate the SQLite file
from untrusted workers; a KnowledgePrincipal is NOT a credential.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .experience import ExperienceCategory, ExperienceRecord, ExperienceVisibility
from .knowledge import (
    KnowledgeAccessDenied,
    KnowledgeConflict,
    KnowledgeEvidenceLevel,
    KnowledgeKind,
    KnowledgeNature,
    KnowledgePrincipal,
    KnowledgeVisibility,
    UnifiedKnowledgeCore,
    _now,
)


def _canonical_hash(value: Any) -> str:
    """Stable content digest for provenance, NOT proof of authentic execution."""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class KnowledgeLearning:
    """One knowledge source of truth with append-only experiment observations.

    Experiment identity, privacy, provenance and context reside in knowledge_entries.
    Observations and human reviews are appended here; a worker's observation is never
    promoted into a verified fact or an approved DAG action automatically.
    """

    def __init__(self, knowledge: UnifiedKnowledgeCore):
        self.core = knowledge
        self.db = knowledge.db
        nested = self.db.in_transaction
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS knowledge_experiment_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('observation','review')),
                observation_id INTEGER,
                actor_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                outcome TEXT,
                decision TEXT,
                summary TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                artifact_sha256 TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(entry_id) REFERENCES knowledge_entries(id),
                FOREIGN KEY(observation_id) REFERENCES knowledge_experiment_events(id)
            )"""
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_review_once "
            "ON knowledge_experiment_events(observation_id) WHERE event_type = 'review'"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_experiment_entry "
            "ON knowledge_experiment_events(entry_id, id)"
        )
        self.db.execute(
            """CREATE TRIGGER IF NOT EXISTS knowledge_experiment_no_update
               BEFORE UPDATE ON knowledge_experiment_events
               BEGIN SELECT RAISE(ABORT, 'experiment events append-only'); END"""
        )
        self.db.execute(
            """CREATE TRIGGER IF NOT EXISTS knowledge_experiment_no_delete
               BEFORE DELETE ON knowledge_experiment_events
               BEGIN SELECT RAISE(ABORT, 'experiment events append-only'); END"""
        )
        if not nested:
            self.db.commit()

    @staticmethod
    def _valid_text(value: str, field: str, max_size: int = 5000) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > max_size:
            raise ValueError(f"{field}: texto obrigatorio de ate {max_size} caracteres")
        return value.strip()

    @staticmethod
    def _evidence_refs(refs: Sequence[str]) -> List[str]:
        if isinstance(refs, (str, bytes)):
            raise ValueError("evidence_refs deve ser lista de referencias, nao texto")
        result = sorted({KnowledgeLearning._valid_text(x, "evidence_ref", 500) for x in refs})
        if len(result) > 40:
            raise ValueError("evidence_refs excedeu o limite")
        return result

    def propose_experiment(
        self,
        principal: KnowledgePrincipal,
        project_id: str,
        hypothesis: str,
        expected_success: str,
        falsification: str,
        *,
        context: Optional[Mapping[str, Any]] = None,
        visibility: str = KnowledgeVisibility.PROJECT_SHARED,
        task_id: Optional[str] = None,
        tags: Sequence[str] = (),
        source_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pre-register expectations, including evidence that would invalidate the idea."""
        protocol = {
            "hypothesis": self._valid_text(hypothesis, "hypothesis"),
            "expected_success": self._valid_text(expected_success, "expected_success"),
            "falsification": self._valid_text(falsification, "falsification"),
        }
        context_data = dict(context or {})
        if "experiment_protocol" in context_data:
            raise ValueError("experiment_protocol e reservado")
        context_data["experiment_protocol"] = protocol
        return self.core.capture(
            principal, project_id, KnowledgeKind.HYPOTHESIS, protocol["hypothesis"],
            visibility=visibility, task_id=task_id, context=context_data,
            nature=KnowledgeNature.SPECULATIVE_DRAFT,
            evidence_level=KnowledgeEvidenceLevel.UNVERIFIED,
            source_kind="experiment", source_id=source_id, tags=tags,
            evidence={"protocol_sha256": _canonical_hash(protocol)},
        )

    def _entry(self, principal: KnowledgePrincipal, entry_id: str) -> Dict[str, Any]:
        entry = self.core.get(principal, entry_id)
        if entry is None:
            raise KnowledgeAccessDenied("Experimento ausente ou fora do escopo")
        if entry["kind"] != KnowledgeKind.HYPOTHESIS or entry["source_kind"] != "experiment":
            raise ValueError("Registro nao e um experimento")
        return entry

    def record_observation(
        self,
        principal: KnowledgePrincipal,
        entry_id: str,
        *,
        outcome: str,
        summary: str,
        evidence_refs: Sequence[str],
        artifact_sha256: Optional[str] = None,
    ) -> int:
        """Observation is *reported*, not cryptographically verified or human-approved."""
        self._entry(principal, entry_id)
        if outcome not in ("success", "failed", "inconclusive"):
            raise ValueError("outcome invalido")
        summary = self._valid_text(summary, "summary")
        refs = self._evidence_refs(evidence_refs)
        if artifact_sha256 is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", artifact_sha256):
            raise ValueError("artifact_sha256 deve conter 64 digitos hexadecimais")
        with self.core._transaction():
            cur = self.db.execute(
                """INSERT INTO knowledge_experiment_events
                   (entry_id,event_type,actor_id,actor_role,outcome,decision,summary,
                    evidence_json,artifact_sha256,created_at)
                   VALUES(?,'observation',?,?,?,NULL,?,?,?,?)""",
                (
                    entry_id, principal.principal_id, principal.role, outcome,
                    summary, json.dumps(refs), artifact_sha256.lower() if artifact_sha256 else None,
                    _now(),
                ),
            )
            return int(cur.lastrowid)

    def review_observation(
        self,
        principal: KnowledgePrincipal,
        entry_id: str,
        *,
        observation_id: int,
        decision: str,
        reason: str,
    ) -> int:
        """Only a trusted human context may review; review is NOT Oracle verification."""
        self._entry(principal, entry_id)
        if principal.role != "human":
            raise KnowledgeAccessDenied("Apenas revisao humana pode qualificar resultados")
        if decision not in ("accepted", "rejected"):
            raise ValueError("decision invalida")
        reason = self._valid_text(reason, "reason", 2000)
        with self.core._transaction():
            observation = self.db.execute(
                """SELECT id FROM knowledge_experiment_events
                   WHERE id=? AND entry_id=? AND event_type='observation'""",
                (observation_id, entry_id),
            ).fetchone()
            if observation is None:
                raise KnowledgeConflict("Observacao inexistente neste experimento")
            try:
                cur = self.db.execute(
                    """INSERT INTO knowledge_experiment_events
                       (entry_id,event_type,observation_id,actor_id,actor_role,outcome,
                        decision,summary,evidence_json,artifact_sha256,created_at)
                       VALUES(?,'review',?,?,?,NULL,?,?, '[]',NULL,?)""",
                    (
                        entry_id, observation_id, principal.principal_id, principal.role,
                        decision, reason, _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KnowledgeConflict("Observacao ja revisada") from exc
            return int(cur.lastrowid)

    def timeline(self, principal: KnowledgePrincipal, entry_id: str) -> List[Dict[str, Any]]:
        self._entry(principal, entry_id)
        rows = self.db.execute(
            "SELECT * FROM knowledge_experiment_events WHERE entry_id=? ORDER BY id",
            (entry_id,),
        ).fetchall()
        return [
            {**dict(r), "evidence_refs": json.loads(r["evidence_json"])}
            for r in rows
        ]

    def reviewed_precedents(
        self,
        principal: KnowledgePrincipal,
        *,
        query: str,
        context: Optional[Mapping[str, Any]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Only return observations with an explicit accepted human review.

        Context compatibility is inherited from KnowledgeCore.find; when constraints
        are absent in history the match remains 'unconfirmed' and must not be used
        for unattended execution.
        """
        candidate_entries = self.core.find(
            principal, query=query, kinds=(KnowledgeKind.HYPOTHESIS,),
            include_speculative=True, context=context, limit=100,
        )
        results: List[Dict[str, Any]] = []
        for entry in candidate_entries:
            if entry["source_kind"] != "experiment":
                continue
            rows = self.db.execute(
                """SELECT obs.*, review.actor_id AS reviewed_by, review.summary AS review_reason
                   FROM knowledge_experiment_events AS obs
                   JOIN knowledge_experiment_events AS review
                     ON review.observation_id = obs.id
                    AND review.event_type='review' AND review.decision='accepted'
                   WHERE obs.entry_id=? AND obs.event_type='observation'
                   ORDER BY obs.id DESC""",
                (entry["id"],),
            ).fetchall()
            for r in rows:
                results.append({
                    "knowledge_id": entry["id"],
                    "hypothesis": entry["content"],
                    "context_match": entry["context_match"],
                    "relevance_score": entry["relevance_score"],
                    "outcome": r["outcome"],
                    "observation": r["summary"],
                    "evidence_refs": json.loads(r["evidence_json"]),
                    "artifact_sha256": r["artifact_sha256"],
                    "reviewed_by": r["reviewed_by"],
                    "review_reason": r["review_reason"],
                    "proof_level": "human_reviewed_not_oracle_verified",
                    "evidence_level": KnowledgeEvidenceLevel.HUMAN_REVIEWED,
                    "nature": entry.get("nature", KnowledgeNature.SPECULATIVE_DRAFT),
                })
        results.sort(key=lambda r: (-r["relevance_score"], r["knowledge_id"]))
        return results[:limit]

    def import_legacy_experience(
        self,
        principal: KnowledgePrincipal,
        record: ExperienceRecord,
    ) -> Dict[str, Any]:
        """Explicit, idempotent, read-model migration from the legacy ExperienceStore.

        Legacy rows are NOT deleted and the legacy API continues to work.
        An import does not grant any extra visibility or convert agent text into
        an accepted decision. Importing private legacy notes requires a human
        principal and a trustworthy mapping of the original human author.
        """
        record.validate()
        if record.visibility == ExperienceVisibility.PRIVATE_HUMAN:
            # Legacy records contain only author='human', never a unique author ID.
            # A project-authorized human is NOT necessarily the original author.
            # Deny until an authenticated identity mapping is available.
            raise KnowledgeAccessDenied(
                "Importacao privada indisponivel: origem nao possui identidade humana verificavel"
            )
        if record.project_id not in principal.authorized_projects:
            raise KnowledgeAccessDenied("Importacao fora dos projetos autorizados")
        digest = _canonical_hash(record.to_dict())
        row = self.db.execute(
            """SELECT id FROM knowledge_entries
               WHERE project_id=? AND source_kind='experience_store' AND source_id=?""",
            (record.project_id, record.id),
        ).fetchone()
        if row is not None:
            existing = self.core.get(principal, row["id"])
            if existing is None:
                raise KnowledgeAccessDenied("Importacao existente fora do escopo")
            if existing["evidence"].get("legacy_record_sha256") != digest:
                raise KnowledgeConflict("Registro original mudou apos a importacao")
            return existing
        kind = {
            ExperienceCategory.ARCHITECTURE_DECISION: KnowledgeKind.ARCHITECTURE_DECISION,
            ExperienceCategory.TOOL_FIX: KnowledgeKind.SOLUTION_PATTERN,
            ExperienceCategory.ERROR_PATTERN: KnowledgeKind.ERROR_PATTERN,
            ExperienceCategory.SOLUTION_PATTERN: KnowledgeKind.SOLUTION_PATTERN,
            ExperienceCategory.USER_PREFERENCE: KnowledgeKind.USER_PREFERENCE,
        }[record.category]
        nature = (
            KnowledgeNature.DECISION_RECORD
            if record.category in (ExperienceCategory.ARCHITECTURE_DECISION, ExperienceCategory.USER_PREFERENCE)
            else KnowledgeNature.OBSERVATION
        )
        return self.core.capture(
            principal, record.project_id, kind,
            f"Problema: {record.problem_pattern}\nAbordagem: {record.solution_pattern}",
            task_id=record.task_id, visibility=record.visibility,
            nature=nature,
            evidence_level=KnowledgeEvidenceLevel.UNVERIFIED,
            source_kind="experience_store", source_id=record.id,
            tags=record.context_tags,
            context={
                "legacy_category": record.category,
                "legacy_outcome": record.outcome,
                "legacy_confidence": record.confidence,
            },
            evidence={
                "legacy_record_sha256": digest,
                "legacy_author_role": record.author,
                "legacy_created_at": record.created_at,
                "provenance_status": "imported_unverified",
            },
        )
