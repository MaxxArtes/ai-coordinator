"""Deterministic experience capture, retrieval, and continuous learning engine.

Stores and queries past decisions, failure patterns, successful remedies,
and architecture preferences in SQLite WAL with strict privacy fencing and 0 external AI calls.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

now = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ExperienceCategory:
    ARCHITECTURE_DECISION = "architecture_decision"
    TOOL_FIX = "tool_fix"
    ERROR_PATTERN = "error_pattern"
    SOLUTION_PATTERN = "solution_pattern"
    USER_PREFERENCE = "user_preference"

    ALL = {
        ARCHITECTURE_DECISION,
        TOOL_FIX,
        ERROR_PATTERN,
        SOLUTION_PATTERN,
        USER_PREFERENCE,
    }


class ExperienceOutcome:
    SUCCESS = "success"
    FAILED = "failed"
    NEUTRAL = "neutral"

    ALL = {SUCCESS, FAILED, NEUTRAL}


class ExperienceVisibility:
    PRIVATE_HUMAN = "private_human"
    PROJECT_SHARED = "project_shared"
    GLOBAL_AUTHORIZED = "global_authorized"

    ALL = {PRIVATE_HUMAN, PROJECT_SHARED, GLOBAL_AUTHORIZED}


class ExperienceAuthor:
    HUMAN = "human"
    AGENT = "agent"

    ALL = {HUMAN, AGENT}


@dataclass
class ExperienceRecord:
    """An auditable record of an architectural, tooling, or operational lesson learned."""
    id: str
    project_id: str
    task_id: Optional[str]
    category: str
    problem_pattern: str
    solution_pattern: str
    outcome: str = ExperienceOutcome.SUCCESS
    context_tags: List[str] = field(default_factory=list)
    confidence: str = "medium"  # high, medium, low
    author: str = ExperienceAuthor.AGENT
    visibility: str = ExperienceVisibility.PROJECT_SHARED
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValueError("id da experiencia deve ser uma string nao vazia")
        if not self.project_id or not isinstance(self.project_id, str):
            raise ValueError("project_id deve ser uma string nao vazia")
        if not self.category or self.category not in ExperienceCategory.ALL:
            raise ValueError(f"category '{self.category}' invalida. Opcoes: {ExperienceCategory.ALL}")
        if not self.problem_pattern or not isinstance(self.problem_pattern, str):
            raise ValueError("problem_pattern deve ser uma string nao vazia")
        if not self.solution_pattern or not isinstance(self.solution_pattern, str):
            raise ValueError("solution_pattern deve ser uma string nao vazia")
        if self.outcome not in ExperienceOutcome.ALL:
            raise ValueError(f"outcome '{self.outcome}' invalido. Opcoes: {ExperienceOutcome.ALL}")
        if self.confidence not in ("high", "medium", "low"):
            raise ValueError(f"confidence '{self.confidence}' invalido. Opcoes: ('high', 'medium', 'low')")
        if self.visibility not in ExperienceVisibility.ALL:
            raise ValueError(f"visibility '{self.visibility}' invalido. Opcoes: {ExperienceVisibility.ALL}")
        if self.author not in ExperienceAuthor.ALL:
            raise ValueError(f"author '{self.author}' invalido. Opcoes: {ExperienceAuthor.ALL}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "category": self.category,
            "problem_pattern": self.problem_pattern,
            "solution_pattern": self.solution_pattern,
            "outcome": self.outcome,
            "context_tags": list(self.context_tags),
            "confidence": self.confidence,
            "author": self.author,
            "visibility": self.visibility,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ExperienceRecord:
        raw_tags = data.get("context_tags", [])
        if isinstance(raw_tags, str):
            try:
                raw_tags = json.loads(raw_tags)
            except Exception:
                raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

        raw_meta = data.get("metadata", {})
        if isinstance(raw_meta, str):
            try:
                raw_meta = json.loads(raw_meta)
            except Exception:
                raw_meta = {}

        return cls(
            id=str(data.get("id", "")),
            project_id=str(data.get("project_id", "")),
            task_id=data.get("task_id"),
            category=str(data.get("category", ExperienceCategory.SOLUTION_PATTERN)),
            problem_pattern=str(data.get("problem_pattern", "")),
            solution_pattern=str(data.get("solution_pattern", "")),
            outcome=str(data.get("outcome", ExperienceOutcome.SUCCESS)),
            context_tags=list(raw_tags) if isinstance(raw_tags, list) else [],
            confidence=str(data.get("confidence", "medium")),
            author=str(data.get("author", ExperienceAuthor.AGENT)),
            visibility=str(data.get("visibility", ExperienceVisibility.PROJECT_SHARED)),
            created_at=str(data.get("created_at", now())),
            updated_at=str(data.get("updated_at", now())),
            metadata=dict(raw_meta) if isinstance(raw_meta, dict) else {},
        )


def _tokenize(text: str) -> Set[str]:
    """Extract lowercased word tokens for deterministic matching."""
    if not text:
        return set()
    return {w for w in re.findall(r"[a-z0-9_-]{2,}", text.lower())}


class ExperienceStore:
    """Deterministic SQLite WAL persistence and privacy-fenced retrieval for experiences."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def record_experience(
        self,
        project_id: str,
        problem_pattern: str,
        solution_pattern: str,
        category: str = ExperienceCategory.SOLUTION_PATTERN,
        outcome: str = ExperienceOutcome.SUCCESS,
        task_id: Optional[str] = None,
        context_tags: Optional[List[str]] = None,
        confidence: str = "medium",
        author: str = ExperienceAuthor.AGENT,
        visibility: str = ExperienceVisibility.PROJECT_SHARED,
        metadata: Optional[Dict[str, Any]] = None,
        record_id: Optional[str] = None,
    ) -> ExperienceRecord:
        """Create and persist an auditable experience record."""
        rec_id = record_id or f"exp-{uuid.uuid4().hex[:12]}"
        stamp = now()
        tags = [str(t).strip().lower() for t in (context_tags or []) if str(t).strip()]

        rec = ExperienceRecord(
            id=rec_id,
            project_id=str(project_id).strip(),
            task_id=str(task_id).strip() if task_id else None,
            category=category,
            problem_pattern=problem_pattern.strip(),
            solution_pattern=solution_pattern.strip(),
            outcome=outcome,
            context_tags=tags,
            confidence=confidence,
            author=author,
            visibility=visibility,
            created_at=stamp,
            updated_at=stamp,
            metadata=metadata or {},
        )
        rec.validate()

        self.db.execute(
            """INSERT INTO experience_records(
                id, project_id, task_id, category, problem_pattern,
                solution_pattern, outcome, context_tags, confidence,
                author, visibility, created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rec.id,
                rec.project_id,
                rec.task_id,
                rec.category,
                rec.problem_pattern,
                rec.solution_pattern,
                rec.outcome,
                json.dumps(rec.context_tags),
                rec.confidence,
                rec.author,
                rec.visibility,
                rec.created_at,
                rec.updated_at,
                json.dumps(rec.metadata),
            ),
        )
        self.db.commit()
        return rec

    def get_experience(
        self,
        record_id: str,
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
    ) -> Optional[ExperienceRecord]:
        """Fetch an experience record by ID, applying privacy gating and project scope bounding."""
        row = self.db.execute(
            "SELECT * FROM experience_records WHERE id=?",
            (record_id,),
        ).fetchone()
        if not row:
            return None

        rec = self._row_to_record(row)
        # Privacy filter: workers/agents cannot access private human records
        if caller_role in ("worker", "agent"):
            if rec.visibility == ExperienceVisibility.PRIVATE_HUMAN:
                return None
            if caller_authorized_projects is not None and rec.visibility != ExperienceVisibility.GLOBAL_AUTHORIZED:
                if rec.project_id not in caller_authorized_projects:
                    return None
        return rec

    def find_experiences(
        self,
        project_id: Optional[str] = None,
        query_text: Optional[str] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
        outcome: Optional[str] = None,
        author_filter: Optional[str] = None,
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[ExperienceRecord]:
        """Deterministic search with strict privacy fencing and token ranking.
        
        Guarantees:
        1. Privacy fencing applied before matching (private_human hidden from agents).
        2. Scope bounding (project-specific or authorized cross-project).
        3. Deterministic lexical scoring (0 external AI calls).
        """
        clauses: List[str] = []
        params: List[Any] = []

        # 1. Privacy filter: workers and agents NEVER receive private_human records
        if caller_role in ("worker", "agent"):
            clauses.append("visibility != ?")
            params.append(ExperienceVisibility.PRIVATE_HUMAN)

        # 2. Scope filter with strict authorization bounding
        if caller_role in ("worker", "agent") and caller_authorized_projects is not None:
            if project_id and project_id not in caller_authorized_projects:
                clauses.append("visibility = ?")
                params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
            else:
                if caller_authorized_projects:
                    placeholders = ",".join("?" for _ in caller_authorized_projects)
                    clauses.append(f"(project_id IN ({placeholders}) OR visibility = ?)")
                    params.extend(caller_authorized_projects)
                    params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
                else:
                    clauses.append("visibility = ?")
                    params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
        elif project_id:
            clauses.append("(project_id = ? OR visibility = ?)")
            params.append(project_id)
            params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
        elif caller_authorized_projects:
            placeholders = ",".join("?" for _ in caller_authorized_projects)
            clauses.append(f"(project_id IN ({placeholders}) OR visibility = ?)")
            params.extend(caller_authorized_projects)
            params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)

        # 3. Category & Outcome filters
        if category:
            clauses.append("category = ?")
            params.append(category)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        if author_filter:
            clauses.append("author = ?")
            params.append(author_filter)

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        query_sql = f"SELECT * FROM experience_records {where_sql} ORDER BY created_at DESC"

        rows = self.db.execute(query_sql, params).fetchall()
        candidates = [self._row_to_record(r) for r in rows]

        if not query_text and not tags:
            return candidates[:limit]

        # Deterministic scoring
        search_tokens = _tokenize(query_text or "")
        search_tags = {str(t).strip().lower() for t in (tags or []) if str(t).strip()}

        scored: List[Tuple[float, ExperienceRecord]] = []
        for cand in candidates:
            score = 0.0
            cand_tags = set(cand.context_tags)
            # Exact tag match weight = 5.0
            matching_tags = search_tags.intersection(cand_tags)
            score += len(matching_tags) * 5.0

            # Problem & Solution token overlap weight = 1.0 each
            cand_tokens = _tokenize(cand.problem_pattern + " " + cand.solution_pattern)
            matching_tokens = search_tokens.intersection(cand_tokens)
            base_match = (len(matching_tags) * 5.0) + (len(matching_tokens) * 1.0)

            if base_match > 0.0 or (not search_tokens and not search_tags):
                score = base_match
                # Confidence boost
                if cand.confidence == "high":
                    score += 0.5
                elif cand.confidence == "low":
                    score -= 0.2

                # Outcome boost (success is preferred)
                if cand.outcome == ExperienceOutcome.SUCCESS:
                    score += 1.0

                scored.append((score, cand))

        scored.sort(key=lambda pair: (pair[0], pair[1].created_at), reverse=True)
        return [pair[1] for pair in scored[:limit]]

    def summarize_relevant_lessons(
        self,
        project_id: str,
        problem_text: str,
        tags: Optional[List[str]] = None,
        caller_role: str = "agent",
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Return compact, actionable lessons for consumption by InsightEngine or Reconstructor."""
        records = self.find_experiences(
            project_id=project_id,
            query_text=problem_text,
            tags=tags,
            outcome=ExperienceOutcome.SUCCESS,
            caller_role=caller_role,
            limit=limit,
        )
        return [
            {
                "experience_id": r.id,
                "category": r.category,
                "problem": r.problem_pattern,
                "proven_solution": r.solution_pattern,
                "confidence": r.confidence,
                "tags": r.context_tags,
                "author": r.author,
            }
            for r in records
        ]

    def _row_to_record(self, row: sqlite3.Row) -> ExperienceRecord:
        return ExperienceRecord.from_dict({
            "id": row["id"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "category": row["category"],
            "problem_pattern": row["problem_pattern"],
            "solution_pattern": row["solution_pattern"],
            "outcome": row["outcome"],
            "context_tags": row["context_tags"],
            "confidence": row["confidence"],
            "author": row["author"],
            "visibility": row["visibility"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "metadata": row["metadata"],
        })
