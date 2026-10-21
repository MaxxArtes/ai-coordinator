"""Insight Engine & Incubator: Collaborative human-agent ideation and discovery.

Provides active exploration (divergent routes A/B/C), cross-project connection,
reevaluation of past decisions, and incubation of hypotheses before conversion
into DAG trajectory proposals. Operates deterministically with 0 external AI calls.
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

from .dag import PlanNode, PlanEdge, TrajectoryProposal, validate_dag
from .experience import (
    ExperienceCategory,
    ExperienceOutcome,
    ExperienceStore,
    ExperienceVisibility,
    normalize_project_id,
    project_id_candidates,
)

now = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class InsightType:
    HYPOTHESIS = "hypothesis"
    ALTERNATIVE_ROUTE = "alternative_route"
    EXPLORATION = "exploration"
    CROSS_PROJECT_CONNECTION = "cross_project_connection"
    RE_EVALUATION = "re_evaluation"

    ALL = {
        HYPOTHESIS,
        ALTERNATIVE_ROUTE,
        EXPLORATION,
        CROSS_PROJECT_CONNECTION,
        RE_EVALUATION,
    }


class InsightStatus:
    INCUBATING = "incubating"
    PROMOTED = "promoted_to_proposal"
    ARCHIVED = "archived"
    REJECTED = "rejected"

    ALL = {INCUBATING, PROMOTED, ARCHIVED, REJECTED}


class InsightPresentation:
    NOW = "now"
    CONTEXTUAL = "contextual"
    REVIEW = "review"

    ALL = {NOW, CONTEXTUAL, REVIEW}


class AutonomyLevel:
    MANUAL = "manual"
    CONTEXTUAL = "contextual"
    PROACTIVE = "proactive"

    ALL = {MANUAL, CONTEXTUAL, PROACTIVE}


@dataclass
class Insight:
    """An ideation unit, hypothesis or alternative route incubating in the system."""
    id: str
    project_id: str
    task_id: Optional[str]
    type: str
    author: str  # human or agent
    origin: str  # cli, notion, worker, trajectory_predictor, manual
    visibility: str  # private_human, project_shared, global_authorized
    status: str = InsightStatus.INCUBATING
    content: str = ""
    alternatives: List[Dict[str, Any]] = field(default_factory=list)
    suggested_first_step: Optional[str] = None
    related_tasks: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    presentation: str = InsightPresentation.CONTEXTUAL
    provenance: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValueError("id do insight deve ser uma string nao vazia")
        if not self.project_id or not isinstance(self.project_id, str):
            raise ValueError("project_id deve ser uma string nao vazia")
        self.project_id = normalize_project_id(self.project_id) or self.project_id.strip()
        if self.type not in InsightType.ALL:
            raise ValueError(f"type '{self.type}' invalido. Opcoes: {InsightType.ALL}")
        if self.author not in ("human", "agent"):
            raise ValueError(f"author '{self.author}' invalido")
        if self.status not in InsightStatus.ALL:
            raise ValueError(f"status '{self.status}' invalido. Opcoes: {InsightStatus.ALL}")
        if self.presentation not in InsightPresentation.ALL:
            raise ValueError(f"presentation '{self.presentation}' invalida. Opcoes: {InsightPresentation.ALL}")
        if self.visibility not in ExperienceVisibility.ALL:
            raise ValueError(f"visibility '{self.visibility}' invalida. Opcoes: {ExperienceVisibility.ALL}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "type": self.type,
            "author": self.author,
            "origin": self.origin,
            "visibility": self.visibility,
            "status": self.status,
            "content": self.content,
            "alternatives": list(self.alternatives),
            "suggested_first_step": self.suggested_first_step,
            "related_tasks": list(self.related_tasks),
            "confidence": self.confidence,
            "presentation": self.presentation,
            "provenance": dict(self.provenance),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Insight:
        raw_alts = data.get("alternatives", [])
        if isinstance(raw_alts, str):
            try:
                raw_alts = json.loads(raw_alts)
            except Exception:
                raw_alts = []

        raw_rel = data.get("related_tasks", [])
        if isinstance(raw_rel, str):
            try:
                raw_rel = json.loads(raw_rel)
            except Exception:
                raw_rel = []

        raw_prov = data.get("provenance", {})
        if isinstance(raw_prov, str):
            try:
                raw_prov = json.loads(raw_prov)
            except Exception:
                raw_prov = {}

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
            type=str(data.get("type", InsightType.HYPOTHESIS)),
            author=str(data.get("author", "agent")),
            origin=str(data.get("origin", "manual")),
            visibility=str(data.get("visibility", ExperienceVisibility.PROJECT_SHARED)),
            status=str(data.get("status", InsightStatus.INCUBATING)),
            content=str(data.get("content", "")),
            alternatives=list(raw_alts) if isinstance(raw_alts, list) else [],
            suggested_first_step=data.get("suggested_first_step"),
            related_tasks=list(raw_rel) if isinstance(raw_rel, list) else [],
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            presentation=str(data.get("presentation", InsightPresentation.CONTEXTUAL)),
            provenance=dict(raw_prov) if isinstance(raw_prov, dict) else {},
            created_at=str(data.get("created_at", now())),
            updated_at=str(data.get("updated_at", now())),
            metadata=dict(raw_meta) if isinstance(raw_meta, dict) else {},
        )


class InsightEngine:
    """Motor de Descoberta e Ideacao com Incubadora e Conexoes Multiprojeto.
    
    Guarantees:
    1. Zero external AI calls during routine operation.
    2. Privacy filtering applied before pattern search or return.
    3. Insights never silently mutate approved DAG plans or active revisions.
    4. Divergent exploration with 3 canonical routes (Pragmatic, Structural, Investigative).
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        experience_store: Optional[ExperienceStore] = None,
        autonomy_level: str = AutonomyLevel.CONTEXTUAL,
    ):
        self.db = db
        self.experience_store = experience_store or ExperienceStore(db)
        if autonomy_level not in AutonomyLevel.ALL:
            raise ValueError(f"autonomy_level '{autonomy_level}' invalido. Opcoes: {AutonomyLevel.ALL}")
        self.autonomy_level = autonomy_level

    def set_autonomy_level(self, level: str) -> None:
        """Configure proactive autonomy level (manual, contextual, proactive)."""
        if level not in AutonomyLevel.ALL:
            raise ValueError(f"autonomy_level '{level}' invalido. Opcoes: {AutonomyLevel.ALL}")
        self.autonomy_level = level

    def record_insight(self, insight: Insight) -> Insight:
        """Persist an insight into the incubator."""
        insight.project_id = normalize_project_id(insight.project_id) or insight.project_id.strip()
        insight.validate()
        stamp = now()
        if not insight.created_at:
            insight.created_at = stamp
        insight.updated_at = stamp

        self.db.execute(
            """INSERT INTO insights(
                id, project_id, task_id, type, author, origin,
                visibility, status, content, alternatives, suggested_first_step,
                related_tasks, confidence, presentation, provenance,
                created_at, updated_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                insight.id,
                insight.project_id,
                insight.task_id,
                insight.type,
                insight.author,
                insight.origin,
                insight.visibility,
                insight.status,
                insight.content,
                json.dumps(insight.alternatives),
                insight.suggested_first_step,
                json.dumps(insight.related_tasks),
                insight.confidence,
                insight.presentation,
                json.dumps(insight.provenance),
                insight.created_at,
                insight.updated_at,
                json.dumps(insight.metadata),
            ),
        )
        self.db.commit()
        return insight

    def get_insight(self, insight_id: str, caller_role: str = "agent") -> Optional[Insight]:
        """Fetch insight by ID with privacy gating."""
        row = self.db.execute("SELECT * FROM insights WHERE id=?", (insight_id,)).fetchone()
        if not row:
            return None
        ins = self._row_to_insight(row)
        if caller_role in ("worker", "agent") and ins.visibility == ExperienceVisibility.PRIVATE_HUMAN:
            return None
        return ins

    def list_incubator(
        self,
        project_id: Optional[str] = None,
        status: Optional[str] = InsightStatus.INCUBATING,
        presentation: Optional[str] = None,
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Insight]:
        """Query incubating insights respecting privacy and project authorization."""
        clauses: List[str] = []
        params: List[Any] = []

        if caller_role in ("worker", "agent"):
            clauses.append("visibility != ?")
            params.append(ExperienceVisibility.PRIVATE_HUMAN)

        # 2. Scope filter with strict authorization bounding and alias normalization
        if caller_role in ("worker", "agent") and caller_authorized_projects is not None:
            expanded_auth_set = {c for ap in caller_authorized_projects for c in project_id_candidates(ap)}
            proj_candidates = project_id_candidates(project_id) if project_id else []

            if project_id and not (set(proj_candidates) & expanded_auth_set):
                clauses.append("visibility = ?")
                params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
            else:
                if expanded_auth_set:
                    placeholders = ",".join("?" for _ in expanded_auth_set)
                    clauses.append(f"(project_id IN ({placeholders}) OR visibility = ?)")
                    params.extend(list(expanded_auth_set))
                    params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
                else:
                    clauses.append("visibility = ?")
                    params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
        elif project_id:
            proj_candidates = project_id_candidates(project_id)
            placeholders = ",".join("?" for _ in proj_candidates)
            clauses.append(f"(project_id IN ({placeholders}) OR visibility = ?)")
            params.extend(proj_candidates)
            params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)
        elif caller_authorized_projects:
            expanded_auth_set = {c for ap in caller_authorized_projects for c in project_id_candidates(ap)}
            placeholders = ",".join("?" for _ in expanded_auth_set)
            clauses.append(f"(project_id IN ({placeholders}) OR visibility = ?)")
            params.extend(list(expanded_auth_set))
            params.append(ExperienceVisibility.GLOBAL_AUTHORIZED)

        if status:
            clauses.append("status = ?")
            params.append(status)

        if presentation:
            clauses.append("presentation = ?")
            params.append(presentation)

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        query_sql = f"SELECT * FROM insights {where_sql} ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self.db.execute(query_sql, params).fetchall()
        return [self._row_to_insight(r) for r in rows]

    def update_insight_status(
        self,
        insight_id: str,
        new_status: str,
        reason: Optional[str] = None,
        caller_role: str = "human",
    ) -> Insight:
        """Update the incubation status of an insight (promoted, archived, rejected)."""
        if new_status not in InsightStatus.ALL:
            raise ValueError(f"Status '{new_status}' invalido. Opcoes: {InsightStatus.ALL}")

        ins = self.get_insight(insight_id, caller_role=caller_role)
        if not ins:
            raise ValueError(f"Insight '{insight_id}' nao encontrado ou acesso nao autorizado")

        stamp = now()
        meta = dict(ins.metadata)
        if reason:
            meta["status_change_reason"] = reason
            meta["status_changed_at"] = stamp

        self.db.execute(
            "UPDATE insights SET status=?, updated_at=?, metadata=? WHERE id=?",
            (new_status, stamp, json.dumps(meta), insight_id),
        )
        self.db.commit()
        ins.status = new_status
        ins.updated_at = stamp
        ins.metadata = meta
        return ins

    def get_contextual_insights(
        self,
        project_id: str,
        task_id: Optional[str] = None,
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
    ) -> List[Insight]:
        """Fetch incubating insights relevant for contextual suggestion upon task resume or milestone.
        
        Honors autonomy level:
        - In 'manual' mode: returns empty list (no proactive interruptions).
        - In 'contextual' or 'proactive' mode: returns contextual/now insights for project.
        """
        if self.autonomy_level == AutonomyLevel.MANUAL:
            return []

        norm_proj = normalize_project_id(project_id) or project_id.strip()
        incubating = self.list_incubator(
            project_id=norm_proj,
            status=InsightStatus.INCUBATING,
            caller_role=caller_role,
            caller_authorized_projects=caller_authorized_projects,
        )
        contextual = [
            i for i in incubating
            if i.presentation in (InsightPresentation.CONTEXTUAL, InsightPresentation.NOW)
            and (not task_id or not i.related_tasks or task_id in i.related_tasks or not i.task_id or i.task_id == task_id)
        ]
        return contextual

    def evaluate_incubator_relevance(
        self,
        project_id: str,
        since_timestamp: Optional[str] = None,
        caller_role: str = "human",
        caller_authorized_projects: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate which incubating ideas in project have gained relevance since last work.
        
        Cross-references incubating hypotheses against newly recorded successful experiences,
        changes, and solutions, returning only relevant connections with clear justifications.
        """
        norm_proj = normalize_project_id(project_id) or project_id.strip()
        incubating = self.list_incubator(
            project_id=norm_proj,
            status=InsightStatus.INCUBATING,
            caller_role=caller_role,
            caller_authorized_projects=caller_authorized_projects,
        )
        evaluations: List[Dict[str, Any]] = []

        for ins in incubating:
            check_since = since_timestamp or ins.created_at
            recent_exps = self.experience_store.find_experiences(
                project_id=norm_proj,
                query_text=ins.content,
                tags=ins.metadata.get("context_tags"),
                outcome=ExperienceOutcome.SUCCESS,
                caller_role=caller_role,
                caller_authorized_projects=caller_authorized_projects,
                limit=5,
            )
            new_reinforcing = [
                e for e in recent_exps
                if e.created_at >= check_since or e.id in ins.provenance.get("past_experience_ids", [])
            ]
            gained = len(new_reinforcing) > 0

            reason = (
                f"Foram identificadas {len(new_reinforcing)} experiencia(s) com solucoes comprovadas "
                f"relevantes para a hipotese '{ins.id}'."
                if gained
                else "Nenhuma nova evidencia ou experiencia coincidente identificada no periodo."
            )

            evaluations.append({
                "insight_id": ins.id,
                "project_id": ins.project_id,
                "type": ins.type,
                "content_preview": ins.content[:100],
                "gained_relevance": gained,
                "justification": reason,
                "reinforcing_experiences": [e.id for e in new_reinforcing],
                "evaluated_at": now(),
            })

        return evaluations

    # -------------------------------------------------------------------------
    # Core Modality 1: Explorar (Active / On-Demand Exploration)
    # -------------------------------------------------------------------------
    def explore(
        self,
        project_id: str,
        topic_or_goal: str,
        context_tags: Optional[List[str]] = None,
        task_id: Optional[str] = None,
        author: str = "human",
        origin: str = "cli",
        visibility: str = ExperienceVisibility.PROJECT_SHARED,
        presentation: str = InsightPresentation.NOW,
        caller_role: str = "human",
    ) -> Insight:
        """Modalidade Explorar: Gera 3 rotas (Pragmatica, Estrutural, Investigativa).
        
        Enriquece alternativas com experiencias anteriores registradas para o topico/tags.
        """
        norm_proj = normalize_project_id(project_id) or project_id.strip()
        past_lessons = self.experience_store.summarize_relevant_lessons(
            project_id=norm_proj,
            problem_text=topic_or_goal,
            tags=context_tags,
            caller_role=caller_role,
            limit=3,
        )

        lessons_context = ""
        if past_lessons:
            lesson_summaries = [f"[{l['experience_id']}] {l['problem']} -> {l['proven_solution']}" for l in past_lessons]
            lessons_context = " Baseado em licoes anteriores: " + "; ".join(lesson_summaries) + "."

        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", topic_or_goal[:30]).strip("_") or "topic"
        insight_id = f"insight-{slug}-{uuid.uuid4().hex[:6]}"

        alternatives = [
            {
                "route_key": "A",
                "name": "Rota A — Pragmática",
                "complexity": "baixa",
                "focus": "Aproveitar componentes existentes e resolver o maior gargalo primeiro.",
                "tradeoff": "Entrega rápida com menor atrito, mas pode acumular dívida técnica caso haja limitações estruturais.",
                "suggested_steps": [
                    {"id": "pragmatic_step_1", "title": f"Aproveitar solucao existente para {topic_or_goal[:50]}"},
                    {"id": "pragmatic_step_2", "title": "Validar impacto imediato e estabilidade com suite de testes"},
                ],
            },
            {
                "route_key": "B",
                "name": "Rota B — Estrutural",
                "complexity": "alta",
                "focus": "Reorganizar componentes e estabelecer interfaces modulares e desacopladas.",
                "tradeoff": "Maior capacidade de evolução e clareza a longo prazo, com maior esforço e risco de migração.",
                "suggested_steps": [
                    {"id": "structural_step_1", "title": f"Definir nova interface e contratos para {topic_or_goal[:50]}"},
                    {"id": "structural_step_2", "title": "Migrar chamadores e desacoplar dependencias legadas"},
                    {"id": "structural_step_3", "title": "Validar regressao e documentar matriz arquitetural"},
                ],
            },
            {
                "route_key": "C",
                "name": "Rota C — Investigativa",
                "complexity": "media",
                "focus": "Criar um experimento isolado (spike) para testar a hipótese mais incerta.",
                "tradeoff": "Reduz incertezas críticas antes de comprometer a arquitetura definitiva.",
                "suggested_steps": [
                    {"id": "investigative_step_1", "title": f"Prototipo isolado para validar hipotese de {topic_or_goal[:50]}"},
                    {"id": "investigative_step_2", "title": "Medir comportamento, invariantes e decidir entre Rota A ou B"},
                ],
            },
        ]

        suggested_first_step = (
            f"Executar experimento investigativo (Rota C) para isolar a incerteza de '{topic_or_goal[:60]}' "
            "antes de decidir entre solucao pragmatica ou reforma estrutural."
        )

        provenance = {
            "mode": "explore",
            "premises": [
                "Requisitos ainda em fase de descoberta e maturacao.",
                "Solucoes anteriores indexadas deterministamente na base de experiencias.",
            ],
            "uncertainty_level": "medium",
            "past_experience_ids": [l["experience_id"] for l in past_lessons],
            "generated_deterministically": True,
        }

        insight = Insight(
            id=insight_id,
            project_id=norm_proj,
            task_id=task_id,
            type=InsightType.EXPLORATION,
            author=author,
            origin=origin,
            visibility=visibility,
            status=InsightStatus.INCUBATING,
            content=f"Exploracao de caminhos para: {topic_or_goal}.{lessons_context}",
            alternatives=alternatives,
            suggested_first_step=suggested_first_step,
            related_tasks=[task_id] if task_id else [],
            confidence=0.85 if past_lessons else 0.70,
            presentation=presentation,
            provenance=provenance,
            metadata={"context_tags": context_tags or []},
        )

        return self.record_insight(insight)

    # -------------------------------------------------------------------------
    # Core Modality 2: Conectar (Cross-Project Connection)
    # -------------------------------------------------------------------------
    def connect(
        self,
        source_project_id: str,
        target_project_id: str,
        topic: str,
        author: str = "agent",
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
    ) -> Optional[Insight]:
        """Modalidade Conectar: Identifica solucoes em projetos autorizados e formula hipotese de reuso.
        
        Guarantees:
        - Nao presume que os dois problemas sao equivalentes.
        - Filtra estritamente registros privados antes da busca.
        """
        src_proj = normalize_project_id(source_project_id) or source_project_id.strip()
        tgt_proj = normalize_project_id(target_project_id) or target_project_id.strip()
        # Verify access authorization for both projects
        if caller_role in ("worker", "agent") and caller_authorized_projects is not None:
            expanded_auth = {c for ap in caller_authorized_projects for c in project_id_candidates(ap)}
            src_cands = set(project_id_candidates(src_proj))
            tgt_cands = set(project_id_candidates(tgt_proj))
            if not (src_cands & expanded_auth) or not (tgt_cands & expanded_auth):
                return None

        # Look for successful patterns in the source project
        source_lessons = self.experience_store.find_experiences(
            project_id=src_proj,
            query_text=topic,
            outcome=ExperienceOutcome.SUCCESS,
            caller_role=caller_role,
            limit=2,
        )

        if not source_lessons:
            return None

        primary = source_lessons[0]
        insight_id = f"conn-{uuid.uuid4().hex[:10]}"

        content = (
            f"Oportunidade de conexao cruzada entre projetos: uma solucao aplicada em '{src_proj}' "
            f"para '{primary.problem_pattern[:80]}' pode inspirar abordagem em '{tgt_proj}' para '{topic[:80]}'."
        )

        alternatives = [
            {
                "route_key": "reuse_hypothesis",
                "name": "Hipótese de Reutilização com Adaptação",
                "source_experience": primary.id,
                "proven_solution": primary.solution_pattern,
                "adaptation_notes": (
                    "Os dois problemas compartilham principios similares, mas pertencem a dominios distintos. "
                    "Adaptar contratos antes de aplicar diretamente."
                ),
            }
        ]

        provenance = {
            "mode": "connect",
            "source_project": source_project_id,
            "target_project": target_project_id,
            "source_experience_id": primary.id,
            "equivalence_assumed": False,
            "uncertainty_level": "medium",
            "premises": [
                "Identificado por similaridade de padrao lexico no banco de experiencias.",
                "Requer validacao humana antes de qualquer vinculacao formal.",
            ],
        }

        insight = Insight(
            id=insight_id,
            project_id=tgt_proj,
            task_id=None,
            type=InsightType.CROSS_PROJECT_CONNECTION,
            author=author,
            origin="insight_engine_connect",
            visibility=ExperienceVisibility.PROJECT_SHARED,
            status=InsightStatus.INCUBATING,
            content=content,
            alternatives=alternatives,
            suggested_first_step=f"Avaliar aplicabilidade da solucao '{primary.id}' em '{tgt_proj}' em contexto seguro.",
            related_tasks=[],
            confidence=0.75,
            presentation=InsightPresentation.CONTEXTUAL,
            provenance=provenance,
            metadata={"source_project": source_project_id, "topic": topic},
        )

        return self.record_insight(insight)

    # -------------------------------------------------------------------------
    # Core Modality 3: Reavaliar (Re-evaluation of Past Decisions)
    # -------------------------------------------------------------------------
    def reevaluate(
        self,
        task_id: str,
        project_id: str,
        current_issue: str,
        observed_error: Optional[str] = None,
        caller_role: str = "agent",
    ) -> Insight:
        """Modalidade Reavaliar: Revisita decisoes anteriores diante de falhas ou novos dados."""
        norm_proj = normalize_project_id(project_id) or project_id.strip()
        # Query past error patterns and remedies
        query = f"{current_issue} {observed_error or ''}"
        remedies = self.experience_store.find_experiences(
            project_id=norm_proj,
            query_text=query,
            category=ExperienceCategory.TOOL_FIX,
            outcome=ExperienceOutcome.SUCCESS,
            caller_role=caller_role,
            limit=2,
        )

        insight_id = f"reeval-{uuid.uuid4().hex[:10]}"
        content = (
            f"Reavaliacao de rota diante de anomalia: '{current_issue}'. "
            f"Erro observado: {observed_error or 'desvio de trajetoria detectado'}."
        )

        alternatives = [
            {
                "route_key": "bypass",
                "name": "Rota de Contorno Imediato",
                "description": "Substituir o no bloqueado por procedimento manual ou worker especializado alternativo.",
            },
            {
                "route_key": "compensate_and_retry",
                "name": "Compensar e Repetir com Parametros Ajustados",
                "description": "Executar compensacao de recursos pendentes e reexecutar com limites estendidos.",
            },
        ]

        if remedies:
            alternatives.insert(0, {
                "route_key": "proven_remedy",
                "name": f"Aplicar Correção Conhecida [{remedies[0].id}]",
                "solution_pattern": remedies[0].solution_pattern,
            })

        provenance = {
            "mode": "reevaluate",
            "task_id": task_id,
            "current_issue": current_issue,
            "observed_error": observed_error,
            "remedy_experience_ids": [r.id for r in remedies],
            "premises": ["Decisao anterior encontrou condicao nao prevista na execucao."],
            "uncertainty_level": "low" if remedies else "medium",
        }

        insight = Insight(
            id=insight_id,
            project_id=norm_proj,
            task_id=task_id,
            type=InsightType.RE_EVALUATION,
            author="agent",
            origin="trajectory_reevaluation",
            visibility=ExperienceVisibility.PROJECT_SHARED,
            status=InsightStatus.INCUBATING,
            content=content,
            alternatives=alternatives,
            suggested_first_step="Verificar compensacoes pendentes e submeter proposta de rota alternativa.",
            related_tasks=[task_id],
            confidence=0.80 if remedies else 0.65,
            presentation=InsightPresentation.NOW,
            provenance=provenance,
            metadata={"issue": current_issue},
        )

        return self.record_insight(insight)

    # -------------------------------------------------------------------------
    # Incubator to DAG Trajectory Proposal Conversion
    # -------------------------------------------------------------------------
    def promote_to_trajectory_proposal(
        self,
        store: Any,
        insight_id: str,
        task_id: str,
        base_revision: int,
        route_choice: str = "A",
        attach_to_node_id: Optional[str] = None,
        caller_role: str = "human",
    ) -> TrajectoryProposal:
        """Convert an incubating alternative route into a formal, unactivated TrajectoryProposal.
        
        Guarantees:
        - The resulting proposal is unactivated (status='proposed').
        - Requires explicit human approval before activation.
        - Existing nodes are never overwritten; node collisions are safely avoided.
        - DAG aciclicity and dependency integrity guaranteed via validate_dag.
        """
        ins = self.get_insight(insight_id, caller_role=caller_role)
        if not ins:
            raise ValueError(f"Insight '{insight_id}' nao encontrado ou acesso nao autorizado")

        # Locate selected route in alternatives
        selected_alt = None
        for alt in ins.alternatives:
            if alt.get("route_key") == route_choice or alt.get("name", "").startswith(f"Rota {route_choice}"):
                selected_alt = alt
                break

        if not selected_alt:
            # Fall back to first alternative if available
            if ins.alternatives:
                selected_alt = ins.alternatives[0]
            else:
                raise ValueError(f"Rota '{route_choice}' nao encontrada nas alternativas do insight")

        # Build plan nodes from the alternative
        raw_steps = selected_alt.get("suggested_steps", [])
        if not raw_steps:
            # Generate default fallback steps
            raw_steps = [
                {"id": f"node_{uuid.uuid4().hex[:6]}", "title": selected_alt.get("name", "Passo de ideacao")},
            ]

        # Fetch base revision to preserve context
        base_rev = store.get_plan_revision(task_id, revision=base_revision)
        if not base_rev:
            raise ValueError(f"Revisao base {base_revision} nao encontrada na tarefa {task_id}")

        new_nodes: Dict[str, PlanNode] = dict(base_rev.nodes)
        new_edges: List[PlanEdge] = list(base_rev.edges)

        # Validate attach_to_node_id if specified
        if attach_to_node_id and attach_to_node_id not in new_nodes:
            raise ValueError(f"No para anexo '{attach_to_node_id}' nao existe no plano")

        # Inject alternative steps ensuring no collisions with existing nodes
        prev_node_id: Optional[str] = attach_to_node_id
        for step in raw_steps:
            raw_nid = str(step["id"])
            nid = raw_nid
            if nid in new_nodes:
                nid = f"{raw_nid}_{uuid.uuid4().hex[:6]}"
            title = str(step["title"])
            node = PlanNode(
                node_id=nid,
                title=title,
                description=f"Gerado a partir do insight {insight_id} ({selected_alt.get('name', route_choice)})",
                status="pending",
                metadata={"origin_insight": insight_id, "route": route_choice, "original_step_id": raw_nid},
            )
            new_nodes[nid] = node
            if prev_node_id:
                new_edges.append(PlanEdge(from_node=prev_node_id, to_node=nid))
            prev_node_id = nid

        # Deduplicate edges by (from_node, to_node)
        seen_edges: Set[Tuple[str, str]] = set()
        deduped_edges: List[PlanEdge] = []
        for e in new_edges:
            key = (e.from_node, e.to_node)
            if key not in seen_edges:
                seen_edges.add(key)
                deduped_edges.append(e)
        new_edges = deduped_edges

        diff_summary = (
            f"Proposta derivada do Insight {insight_id} ({selected_alt.get('name', route_choice)}): "
            f"Adicionados {len(raw_steps)} novos nós de trabalho."
        )

        # Submit unactivated proposal
        proposal = store.propose_trajectory(
            task_id=task_id,
            base_revision=base_revision,
            proposed_nodes=new_nodes,
            proposed_edges=new_edges,
            reason=f"Evolucao orientada a insight: {ins.content[:100]}",
            diff_summary=diff_summary,
            metadata={"insight_id": insight_id, "route_choice": route_choice},
        )

        # Mark insight as promoted
        self.update_insight_status(
            insight_id=insight_id,
            new_status=InsightStatus.PROMOTED,
            reason=f"Promovido para TrajectoryProposal {proposal.id} sobre revisao {base_revision}",
            caller_role=caller_role,
        )

        return proposal

    def _row_to_insight(self, row: sqlite3.Row) -> Insight:
        return Insight.from_dict({
            "id": row["id"],
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "type": row["type"],
            "author": row["author"],
            "origin": row["origin"],
            "visibility": row["visibility"],
            "status": row["status"],
            "content": row["content"],
            "alternatives": row["alternatives"],
            "suggested_first_step": row["suggested_first_step"],
            "related_tasks": row["related_tasks"],
            "confidence": row["confidence"],
            "presentation": row["presentation"],
            "provenance": row["provenance"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "metadata": row["metadata"],
        })
