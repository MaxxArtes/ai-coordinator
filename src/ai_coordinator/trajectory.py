"""Deterministic trajectory risk prediction and moving horizon analysis.

Evaluates execution telemetry against DAG flight plans, projecting step, time,
token, and failure envelopes without external AI calls or mutation of active DAG state.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime as dt
import json
import math
from typing import Any, Dict, List, Optional, Set, Tuple
import uuid

from .dag import (
    DagError,
    DagValidationError,
    PlanEdge,
    PlanNode,
    PlanRevision,
    StaleRevisionError,
    validate_dag,
)
from .mission import FlightBudget, FlightPlan


# ---------------------------------------------------------------------------
# Constants and Enums
# ---------------------------------------------------------------------------

class TrajectoryStatus:
    NOMINAL = "nominal"
    AT_RISK = "at_risk"
    DEVIATED = "deviated"
    INSUFFICIENT_DATA = "insufficient_data"


class ConfidenceLevel:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class TokenUsageQuality:
    COMPLETE = "complete"
    PARTIAL = "partial"
    MISSING = "missing"


class Recommendation:
    CONTINUE = "continue"
    CHECKPOINT_AND_REVIEW = "checkpoint_and_review"
    REPLAN_PROPOSAL = "replan_proposal"
    HALT_EXCEEDED = "halt_exceeded"
    GATHER_DATA = "gather_data"


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class TelemetrySnapshot:
    """Normalized metrics capturing current operational state."""
    task_id: str
    step_count: int
    uncheckpointed_steps: int
    drift_status: str
    total_runs: int
    completed_runs: int
    failed_runs: int
    token_usage_total: Optional[int]
    token_usage_quality: str  # complete, partial, missing
    completed_nodes_count: int
    observed_steps_per_node: Optional[float]
    observed_failure_rate: float
    elapsed_duration_seconds: float
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BudgetProjection:
    """Deterministic projection of remaining operational margins."""
    max_steps: int
    consumed_steps: int
    remaining_steps: int
    estimated_required_steps: Optional[int]
    max_uncheckpointed_steps: int
    current_uncheckpointed_steps: int
    projected_uncheckpointed_steps: int
    steps_exhaustion_projected: bool
    silence_exhaustion_projected: bool
    max_duration_seconds: Optional[float]
    elapsed_duration_seconds: float
    time_exhaustion_projected: bool
    max_cost_tokens: Optional[int]
    consumed_tokens: Optional[int]
    tokens_exhaustion_projected: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TrajectoryPrediction:
    """Immutable prognostic evaluation for a specific DAG plan revision."""
    id: str
    task_id: str
    plan_revision: int
    current_node: Optional[str]
    horizon_nodes: List[str]
    status: str  # nominal, at_risk, deviated, insufficient_data
    reason: str
    confidence: str  # high, medium, low, unknown
    token_usage_quality: str  # complete, partial, missing
    remaining_steps: int
    estimated_required_steps: Optional[int]
    recommendation: str
    requires_human_approval: bool
    budget_projection: Dict[str, Any]
    evidence: List[str]
    created_at: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "plan_revision": self.plan_revision,
            "current_node": self.current_node,
            "horizon_nodes": list(self.horizon_nodes),
            "status": self.status,
            "reason": self.reason,
            "confidence": self.confidence,
            "token_usage_quality": self.token_usage_quality,
            "remaining_steps": self.remaining_steps,
            "estimated_required_steps": self.estimated_required_steps,
            "recommendation": self.recommendation,
            "requires_human_approval": self.requires_human_approval,
            "budget_projection": dict(self.budget_projection),
            "evidence": list(self.evidence),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TrajectoryPrediction:
        return cls(
            id=str(data.get("id", "")),
            task_id=str(data.get("task_id", "")),
            plan_revision=int(data.get("plan_revision", 1)),
            current_node=data.get("current_node"),
            horizon_nodes=list(data.get("horizon_nodes", [])),
            status=str(data.get("status", TrajectoryStatus.NOMINAL)),
            reason=str(data.get("reason", "")),
            confidence=str(data.get("confidence", ConfidenceLevel.UNKNOWN)),
            token_usage_quality=str(data.get("token_usage_quality", TokenUsageQuality.MISSING)),
            remaining_steps=int(data.get("remaining_steps", 0)),
            estimated_required_steps=data.get("estimated_required_steps"),
            recommendation=str(data.get("recommendation", Recommendation.CONTINUE)),
            requires_human_approval=bool(data.get("requires_human_approval", False)),
            budget_projection=dict(data.get("budget_projection", {})),
            evidence=list(data.get("evidence", [])),
            created_at=str(data.get("created_at", "")),
            metadata=dict(data.get("metadata", {})),
        )


# ---------------------------------------------------------------------------
# Alert Deduplication and Hysteresis
# ---------------------------------------------------------------------------

class AlertDeduplicator:
    """Hysteresis filter preventing duplicate alerts from flooding storage."""

    def __init__(self) -> None:
        # Key: (task_id, plan_revision) -> (status, reason_category, severity)
        self._last_state: Dict[Tuple[str, int], Tuple[str, str, str]] = {}
        # Key: (task_id, plan_revision) -> count of suppressed duplicates
        self._suppressed_counts: Dict[Tuple[str, int], int] = {}

    @staticmethod
    def _categorize_reason(reason: str) -> str:
        r = reason.lower()
        if "passos" in r or "step" in r:
            return "steps_exhaustion"
        if "silencio" in r or "checkpoint" in r:
            return "silence_exhaustion"
        if "falha" in r or "fail" in r:
            return "failure_rate"
        if "duracao" in r or "tempo" in r or "duration" in r:
            return "duration_exhaustion"
        if "token" in r:
            return "token_exhaustion"
        return "general"

    def should_emit(self, prediction: TrajectoryPrediction) -> bool:
        """Return True if this prediction represents a significant state change, False if suppressed."""
        key = (prediction.task_id, prediction.plan_revision)
        reason_cat = self._categorize_reason(prediction.reason)
        severity = "warning" if prediction.status == TrajectoryStatus.AT_RISK else (
            "error" if prediction.status == TrajectoryStatus.DEVIATED else "info"
        )
        current_tuple = (prediction.status, reason_cat, severity)

        if key not in self._last_state:
            return True

        last_status, last_cat, last_sev = self._last_state[key]
        if (prediction.status != last_status) or (reason_cat != last_cat) or (severity != last_sev):
            return True

        # State is identical -> suppress redundant alert
        self._suppressed_counts[key] = self._suppressed_counts.get(key, 0) + 1
        return False

    def record_emission(self, prediction: TrajectoryPrediction) -> None:
        """Record an alert emission into the hysteresis filter."""
        key = (prediction.task_id, prediction.plan_revision)
        reason_cat = self._categorize_reason(prediction.reason)
        severity = "warning" if prediction.status == TrajectoryStatus.AT_RISK else (
            "error" if prediction.status == TrajectoryStatus.DEVIATED else "info"
        )
        self._last_state[key] = (prediction.status, reason_cat, severity)

    def suppression_count(self, task_id: str, plan_revision: int) -> int:
        """Return how many redundant alerts were suppressed for this task and revision."""
        return self._suppressed_counts.get((task_id, plan_revision), 0)

    def reset(self, task_id: Optional[str] = None) -> None:
        """Reset hysteresis state for task or all tasks."""
        if task_id:
            keys = [k for k in self._last_state if k[0] == task_id]
            for k in keys:
                self._last_state.pop(k, None)
                self._suppressed_counts.pop(k, None)
        else:
            self._last_state.clear()
            self._suppressed_counts.clear()


# ---------------------------------------------------------------------------
# Trajectory Predictor Engine
# ---------------------------------------------------------------------------

class TrajectoryPredictor:
    """Deterministic prediction engine for DAG trajectory deviations."""

    def __init__(self, store: Any, deduplicator: Optional[AlertDeduplicator] = None) -> None:
        self.store = store
        self.deduplicator = deduplicator or AlertDeduplicator()

    def normalize_telemetry(self, task_id: str, revision: PlanRevision) -> TelemetrySnapshot:
        """Extract and normalize all available runtime metrics and telemetry quality."""
        task_row = self.store.task(task_id)
        if not task_row:
            raise DagValidationError(f"Tarefa '{task_id}' nao encontrada")

        step_count = int(task_row.get("step_count") or 0)
        uncheckpointed_steps = int(task_row.get("uncheckpointed_steps") or 0)
        drift_status = str(task_row.get("drift_status") or "nominal")
        created_str = str(task_row.get("created") or "")
        updated_str = str(task_row.get("updated") or "")

        # Compute elapsed duration
        elapsed_seconds = 0.0
        if created_str and updated_str:
            try:
                c_dt = dt.datetime.fromisoformat(created_str)
                u_dt = dt.datetime.fromisoformat(updated_str)
                elapsed_seconds = max(0.0, (u_dt - c_dt).total_seconds())
            except Exception:
                pass

        # Runs telemetry
        runs = self.store.runs(task_id)
        total_runs = len(runs)
        completed_runs = sum(1 for r in runs if r.get("status") == "complete")
        failed_runs = sum(1 for r in runs if r.get("status") == "failed")
        observed_failure_rate = (failed_runs / total_runs) if total_runs > 0 else 0.0

        # Token telemetry quality assessment
        runs_with_valid_usage = 0
        total_tokens = 0
        for r in runs:
            usage_data = r.get("usage")
            if usage_data:
                try:
                    if isinstance(usage_data, str):
                        parsed = json.loads(usage_data)
                    elif isinstance(usage_data, dict):
                        parsed = usage_data
                    else:
                        parsed = {}

                    tok = None
                    if "total_tokens" in parsed:
                        tok = parsed["total_tokens"]
                    elif "tokens" in parsed:
                        tok = parsed["tokens"]
                    elif "output_tokens" in parsed or "input_tokens" in parsed:
                        tok = int(parsed.get("input_tokens", 0)) + int(parsed.get("output_tokens", 0))

                    if tok is not None and isinstance(tok, (int, float)) and tok > 0:
                        total_tokens += int(tok)
                        runs_with_valid_usage += 1
                except Exception:
                    pass

        if total_runs == 0:
            token_quality = TokenUsageQuality.MISSING
            total_tokens_val = None
        elif runs_with_valid_usage == total_runs:
            token_quality = TokenUsageQuality.COMPLETE
            total_tokens_val = total_tokens
        elif runs_with_valid_usage > 0:
            token_quality = TokenUsageQuality.PARTIAL
            total_tokens_val = total_tokens
        else:
            token_quality = TokenUsageQuality.MISSING
            total_tokens_val = None

        # DAG node metrics
        completed_nodes_count = sum(1 for n in revision.nodes.values() if n.status == "completed")
        observed_steps_per_node: Optional[float] = None
        if completed_nodes_count > 0 and step_count > 0:
            observed_steps_per_node = float(step_count) / float(completed_nodes_count)

        return TelemetrySnapshot(
            task_id=task_id,
            step_count=step_count,
            uncheckpointed_steps=uncheckpointed_steps,
            drift_status=drift_status,
            total_runs=total_runs,
            completed_runs=completed_runs,
            failed_runs=failed_runs,
            token_usage_total=total_tokens_val,
            token_usage_quality=token_quality,
            completed_nodes_count=completed_nodes_count,
            observed_steps_per_node=observed_steps_per_node,
            observed_failure_rate=observed_failure_rate,
            elapsed_duration_seconds=elapsed_seconds,
            created_at=updated_str or created_str or dt.datetime.now(dt.timezone.utc).isoformat(),
        )

    def select_horizon_nodes(
        self,
        revision: PlanRevision,
        current_node_id: Optional[str] = None,
        horizon_depth: int = 3,
    ) -> List[str]:
        """Select a moving horizon window of forward active/pending nodes."""
        if horizon_depth < 1:
            raise DagValidationError("horizon_depth deve ser >= 1")

        topo_order = validate_dag(revision.nodes, revision.edges)

        if current_node_id:
            if current_node_id not in revision.nodes:
                raise DagValidationError(f"Nó atual '{current_node_id}' nao existe na revisao do plano")
            start_idx = topo_order.index(current_node_id)
            candidates = topo_order[start_idx:]
        else:
            candidates = topo_order

        # Pick nodes that are not completed or skipped
        horizon: List[str] = []
        for nid in candidates:
            node = revision.nodes[nid]
            if node.status in ("completed", "skipped"):
                continue
            horizon.append(nid)
            if len(horizon) >= horizon_depth:
                break

        return horizon

    def project_budget(
        self,
        telemetry: TelemetrySnapshot,
        horizon_nodes: List[str],
        revision: PlanRevision,
        flight_budget: FlightBudget,
    ) -> Tuple[BudgetProjection, str, List[str], Optional[int]]:
        """Compute budget projections, confidence, and audit evidence without guessing."""
        consumed_steps = telemetry.step_count
        remaining_steps = max(0, flight_budget.max_steps - consumed_steps)
        current_uncheckpointed = telemetry.uncheckpointed_steps

        evidence: List[str] = []
        node_estimates: List[int] = []
        nodes_with_declared_estimates = 0

        for nid in horizon_nodes:
            node = revision.nodes[nid]
            declared = (
                node.metadata.get("estimated_steps")
                or node.metadata.get("weight")
                or node.metadata.get("budget_steps")
            )
            if declared is not None and isinstance(declared, (int, float)) and declared > 0:
                node_estimates.append(int(math.ceil(declared)))
                nodes_with_declared_estimates += 1
            elif telemetry.observed_steps_per_node is not None:
                node_estimates.append(int(math.ceil(telemetry.observed_steps_per_node)))
            else:
                pass

        total_estimated_steps: Optional[int] = None
        confidence: str = ConfidenceLevel.UNKNOWN

        if len(node_estimates) == len(horizon_nodes) and len(horizon_nodes) > 0:
            total_estimated_steps = sum(node_estimates)
            if nodes_with_declared_estimates == len(horizon_nodes):
                confidence = ConfidenceLevel.HIGH
                evidence.append("Estimativa fundamentada em orcamento declarado em todos os nós do horizonte")
            elif telemetry.completed_nodes_count >= 3:
                confidence = ConfidenceLevel.MEDIUM
                evidence.append(
                    f"Estimativa fundamentada na taxa media observada ({telemetry.observed_steps_per_node:.1f} passos/nó em {telemetry.completed_nodes_count} nós concluidos)"
                )
            else:
                confidence = ConfidenceLevel.LOW
                evidence.append(
                    f"Estimativa incerta baseada em baixa amostragem ({telemetry.completed_nodes_count} nó(s) concluido(s))"
                )
        elif len(node_estimates) > 0:
            # Partial estimates: check if known estimates alone already breach the budget
            known_sum = sum(node_estimates)
            if (consumed_steps + known_sum > flight_budget.max_steps) or (
                current_uncheckpointed + known_sum >= flight_budget.max_uncheckpointed_steps
            ):
                total_estimated_steps = known_sum
                confidence = ConfidenceLevel.HIGH if nodes_with_declared_estimates == len(node_estimates) else ConfidenceLevel.MEDIUM
                evidence.append(
                    f"Estimativa de nós conhecidos ({known_sum} passos em {len(node_estimates)}/{len(horizon_nodes)} nós) ultrapassa os limites operacionais"
                )
            else:
                total_estimated_steps = None
                confidence = ConfidenceLevel.UNKNOWN
                evidence.append("Sem estimativas declaradas completas para todos os nós do horizonte")
        else:
            total_estimated_steps = None
            confidence = ConfidenceLevel.UNKNOWN
            evidence.append("Sem estimativas declaradas nem historico observavel suficiente para projetar passos")

        # Step exhaustion
        steps_exhaustion = False
        if total_estimated_steps is not None:
            projected_total = consumed_steps + total_estimated_steps
            if projected_total > flight_budget.max_steps:
                steps_exhaustion = True
                evidence.append(
                    f"Passos projetados ({projected_total}) ultrapassam o teto maximo ({flight_budget.max_steps})"
                )

        # Radio silence (uncheckpointed steps) exhaustion
        silence_step_increment = total_estimated_steps if total_estimated_steps is not None else 1
        projected_uncheckpointed = current_uncheckpointed + silence_step_increment
        silence_exhaustion = False
        if current_uncheckpointed >= flight_budget.max_uncheckpointed_steps:
            silence_exhaustion = True
            evidence.append(
                f"Silencio de radio ja no limite ou excedido ({current_uncheckpointed}/{flight_budget.max_uncheckpointed_steps})"
            )
        elif projected_uncheckpointed >= flight_budget.max_uncheckpointed_steps or (
            current_uncheckpointed >= flight_budget.max_uncheckpointed_steps - 2 and silence_step_increment >= 2
        ):
            silence_exhaustion = True
            evidence.append(
                f"Silencio de radio projetado ({projected_uncheckpointed}) atingira ou excedera o limite ({flight_budget.max_uncheckpointed_steps})"
            )

        # Duration exhaustion
        time_exhaustion = False
        max_duration = getattr(flight_budget, "max_elapsed_seconds", None) or getattr(flight_budget, "max_duration_seconds", None)
        if max_duration and max_duration > 0:
            if telemetry.elapsed_duration_seconds >= max_duration:
                time_exhaustion = True
                evidence.append(
                    f"Duracao decorrida ({telemetry.elapsed_duration_seconds:.1f}s) excede o limite maximo ({max_duration}s)"
                )

        # Token exhaustion
        token_exhaustion = False
        max_tokens = getattr(flight_budget, "max_cost_tokens", None)
        if max_tokens and max_tokens > 0 and telemetry.token_usage_total:
            if telemetry.token_usage_total > max_tokens:
                token_exhaustion = True
                evidence.append(
                    f"Consumo de tokens ({telemetry.token_usage_total}) ultrapassou o teto ({max_tokens})"
                )
            if telemetry.token_usage_quality == TokenUsageQuality.PARTIAL:
                evidence.append("Aviso: metrica de tokens parcial; consumo real pode ser superior")

        projection = BudgetProjection(
            max_steps=flight_budget.max_steps,
            consumed_steps=consumed_steps,
            remaining_steps=remaining_steps,
            estimated_required_steps=total_estimated_steps,
            max_uncheckpointed_steps=flight_budget.max_uncheckpointed_steps,
            current_uncheckpointed_steps=current_uncheckpointed,
            projected_uncheckpointed_steps=projected_uncheckpointed,
            steps_exhaustion_projected=steps_exhaustion,
            silence_exhaustion_projected=silence_exhaustion,
            max_duration_seconds=max_duration,
            elapsed_duration_seconds=telemetry.elapsed_duration_seconds,
            time_exhaustion_projected=time_exhaustion,
            max_cost_tokens=max_tokens,
            consumed_tokens=telemetry.token_usage_total,
            tokens_exhaustion_projected=token_exhaustion,
        )

        return projection, confidence, evidence, total_estimated_steps

    def predict(
        self,
        task_id: str,
        plan_revision: Optional[int] = None,
        current_node_id: Optional[str] = None,
        horizon_depth: int = 3,
        flight_plan: Optional[FlightPlan] = None,
    ) -> TrajectoryPrediction:
        """Produce deterministic diagnostic prediction of trajectory risk without state mutation."""
        # 1. Fetch active plan revision and verify optimistic fencing
        latest_rev = self.store.get_plan_revision(task_id)
        if not latest_rev:
            raise DagValidationError(f"Tarefa '{task_id}' nao possui revisao de plano DAG cadastrada")

        if plan_revision is not None and plan_revision != latest_rev.revision:
            raise StaleRevisionError(
                f"Revisao informada {plan_revision} e obsoleta; revisao ativa e {latest_rev.revision}"
            )

        active_rev = latest_rev

        # 2. Telemetry normalization
        telemetry = self.normalize_telemetry(task_id, active_rev)

        # 3. Resolve flight budget
        if flight_plan is not None:
            flight_budget = flight_plan.budget
        else:
            flight_budget = FlightBudget(
                max_steps=int(active_rev.metadata.get("max_steps", 50)),
                max_uncheckpointed_steps=int(active_rev.metadata.get("max_uncheckpointed_steps", 10)),
            )

        # 4. Moving horizon selection
        horizon_nodes = self.select_horizon_nodes(active_rev, current_node_id, horizon_depth)

        # 5. Budget projection
        projection, confidence, evidence, total_estimated = self.project_budget(
            telemetry, horizon_nodes, active_rev, flight_budget
        )

        # 6. Deterministic status classification
        prediction_id = uuid.uuid4().hex[:16]
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

        # Priority 1: Confirmed Deviation
        if telemetry.drift_status == "deviated" or telemetry.step_count > flight_budget.max_steps:
            status = TrajectoryStatus.DEVIATED
            reason = (
                f"Desvio confirmado: {telemetry.step_count} passos > limite {flight_budget.max_steps}"
                if telemetry.step_count > flight_budget.max_steps
                else "Desvio confirmado na execucao da missao"
            )
            recommendation = (
                Recommendation.REPLAN_PROPOSAL
                if telemetry.step_count > flight_budget.max_steps
                else Recommendation.CHECKPOINT_AND_REVIEW
            )
            requires_human_approval = True

        # Priority 2: At Risk — Anticipated Exhaustion (Steps, Silence, Time, Failure Rate)
        elif projection.steps_exhaustion_projected:
            status = TrajectoryStatus.AT_RISK
            reason = f"Passos projetados ({telemetry.step_count + (total_estimated or 0)}) excedem o orcamento maximo ({flight_budget.max_steps})"
            recommendation = Recommendation.REPLAN_PROPOSAL
            requires_human_approval = False

        elif projection.silence_exhaustion_projected:
            status = TrajectoryStatus.AT_RISK
            reason = f"Silencio de radio iminente: {telemetry.uncheckpointed_steps} passos sem checkpoint (limite: {flight_budget.max_uncheckpointed_steps})"
            recommendation = Recommendation.CHECKPOINT_AND_REVIEW
            requires_human_approval = False

        elif telemetry.total_runs >= 3 and telemetry.observed_failure_rate >= 0.5:
            status = TrajectoryStatus.AT_RISK
            reason = f"Taxa de falhas observada elevada: {telemetry.failed_runs}/{telemetry.total_runs} execucoes falharam ({telemetry.observed_failure_rate*100:.0f}%)"
            recommendation = Recommendation.CHECKPOINT_AND_REVIEW
            requires_human_approval = False

        elif projection.time_exhaustion_projected:
            status = TrajectoryStatus.AT_RISK
            reason = f"Tempo de execucao ({telemetry.elapsed_duration_seconds:.1f}s) excede o teto ({projection.max_duration_seconds}s)"
            recommendation = Recommendation.CHECKPOINT_AND_REVIEW
            requires_human_approval = False

        # Priority 3: Insufficient Data / Unknown
        elif confidence == ConfidenceLevel.UNKNOWN or total_estimated is None:
            status = TrajectoryStatus.INSUFFICIENT_DATA
            reason = "Metricas historicas ou declaradas insuficientes para projetar trajetoria"
            recommendation = Recommendation.GATHER_DATA
            requires_human_approval = False

        # Priority 4: Nominal
        else:
            status = TrajectoryStatus.NOMINAL
            reason = "Trajetoria dentro do envelope operacional previsto"
            recommendation = Recommendation.CONTINUE
            requires_human_approval = False

        return TrajectoryPrediction(
            id=prediction_id,
            task_id=task_id,
            plan_revision=active_rev.revision,
            current_node=current_node_id or (horizon_nodes[0] if horizon_nodes else None),
            horizon_nodes=horizon_nodes,
            status=status,
            reason=reason,
            confidence=confidence,
            token_usage_quality=telemetry.token_usage_quality,
            remaining_steps=projection.remaining_steps,
            estimated_required_steps=total_estimated,
            recommendation=recommendation,
            requires_human_approval=requires_human_approval,
            budget_projection=projection.to_dict(),
            evidence=evidence,
            created_at=now_iso,
        )

    def record_prediction(
        self,
        prediction: TrajectoryPrediction,
        force: bool = False,
    ) -> Optional[str]:
        """Save prediction to storage if deduplication permits or force=True."""
        if not force and not self.deduplicator.should_emit(prediction):
            return None

        self.deduplicator.record_emission(prediction)
        return self.store.record_trajectory_prediction(prediction)
