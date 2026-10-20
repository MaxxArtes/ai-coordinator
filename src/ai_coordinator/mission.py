"""Deterministic Mission Control (Torre de Controle) for AI Coordinator.

Fase 2:
- 2A: Formal Flight Plan contract (goal, acceptance_criteria, allowed_paths, budget, mandatory_landing_checks).
- 2B: Telemetry and radio silence detection (heartbeat, event stream, prolonged silence alert).
- 2C: Trajectory control and guardrails (drift detection, allowed paths, budget enforcement).
- 2D: Landing verification gate (prevents task completion if mandatory criteria not verified, even on exit code 0).

Standard library + SQLite only. Deterministic (no mandatory LLM calls).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple, Union

now = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class MissionControlError(ValueError):
    """Base error for Mission Control violations."""
    pass


class RadioSilenceError(MissionControlError):
    """Raised when progress is attempted during prolonged radio silence (uncheckpointed steps threshold exceeded)."""
    pass


class BudgetExceededError(MissionControlError):
    """Raised when execution exceeds allocated flight plan budget (steps, time, or calls)."""
    pass


class UnauthorizedPathError(MissionControlError):
    """Raised when an operation targets a path outside allowed_paths."""
    pass


class LandingVerificationError(MissionControlError):
    """Raised when attempting to mark mission as complete without passing mandatory landing checks."""
    pass


class DeviationType:
    PROLONGED_SILENCE = "deviation_prolonged_silence"
    PATH_VIOLATION = "deviation_path_violation"
    BUDGET_EXCEEDED = "deviation_budget_exceeded"
    CHECK_FAILED = "deviation_check_failed"


class DriftStatus:
    NOMINAL = "nominal"
    DEVIATION_DETECTED = "deviation_detected"
    BLOCKED = "blocked"


def is_path_allowed(target_path: Union[str, Path], allowed_paths: List[Union[str, Path]]) -> bool:
    """Deterministic path boundary check supporting Windows and POSIX path resolution."""
    if not allowed_paths:
        return False
    target_norm = os.path.normcase(os.path.abspath(str(target_path)))
    for allowed in allowed_paths:
        allowed_norm = os.path.normcase(os.path.abspath(str(allowed)))
        if target_norm == allowed_norm:
            return True
        # Check subpath containment
        if target_norm.startswith(allowed_norm + os.sep) or target_norm.startswith(allowed_norm + "/"):
            return True
    return False


@dataclass
class FlightBudget:
    max_steps: int = 50
    max_elapsed_seconds: float = 3600.0
    max_uncheckpointed_steps: int = 10
    max_worker_calls: int = 15

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FlightBudget:
        return cls(
            max_steps=int(data.get("max_steps", 50)),
            max_elapsed_seconds=float(data.get("max_elapsed_seconds", 3600.0)),
            max_uncheckpointed_steps=int(data.get("max_uncheckpointed_steps", 10)),
            max_worker_calls=int(data.get("max_worker_calls", 15)),
        )


@dataclass
class LandingCheck:
    check_name: str
    status: str = "pending"  # "pending", "passed", "failed"
    evidence: Optional[str] = None
    verified_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FlightPlan:
    task_id: str
    goal: str
    acceptance_criteria: List[str]
    allowed_paths: List[str]
    budget: FlightBudget = field(default_factory=FlightBudget)
    mandatory_landing_checks: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)

    def validate(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise MissionControlError("Plano de voo deve conter um objetivo valido")
        if len(self.goal) > 1500:
            raise MissionControlError("Objetivo do plano de voo excede 1500 caracteres")
        if not isinstance(self.acceptance_criteria, list) or not self.acceptance_criteria:
            raise MissionControlError("Plano de voo deve conter lista nao-vazia de criterios de aceitacao")
        for crit in self.acceptance_criteria:
            if not isinstance(crit, str) or not crit.strip():
                raise MissionControlError("Criterio de aceitacao deve ser texto nao-vazio")
        if not isinstance(self.allowed_paths, list) or not self.allowed_paths:
            raise MissionControlError("Plano de voo deve conter lista nao-vazia de caminhos autorizados (allowed_paths)")
        for p in self.allowed_paths:
            if not isinstance(p, str) or not p.strip():
                raise MissionControlError("Caminho autorizado invalido")
        if self.budget.max_steps <= 0:
            raise MissionControlError("max_steps deve ser maior que zero")
        if self.budget.max_elapsed_seconds <= 0:
            raise MissionControlError("max_elapsed_seconds deve ser maior que zero")
        if self.budget.max_uncheckpointed_steps <= 0:
            raise MissionControlError("max_uncheckpointed_steps deve ser maior que zero")
        if self.budget.max_worker_calls <= 0:
            raise MissionControlError("max_worker_calls deve ser maior que zero")
        if not isinstance(self.mandatory_landing_checks, list):
            raise MissionControlError("mandatory_landing_checks deve ser uma lista")
        for chk in self.mandatory_landing_checks:
            if not isinstance(chk, str) or not chk.strip():
                raise MissionControlError("Nome de verificacao de pouso obrigatoria deve ser texto nao-vazio")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "acceptance_criteria": self.acceptance_criteria,
            "allowed_paths": self.allowed_paths,
            "budget": self.budget.to_dict(),
            "mandatory_landing_checks": self.mandatory_landing_checks,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FlightPlan:
        budget_data = data.get("budget", {})
        budget = FlightBudget.from_dict(budget_data) if isinstance(budget_data, dict) else FlightBudget()
        return cls(
            task_id=data["task_id"],
            goal=data["goal"],
            acceptance_criteria=list(data.get("acceptance_criteria", [])),
            allowed_paths=list(data.get("allowed_paths", [])),
            budget=budget,
            mandatory_landing_checks=list(data.get("mandatory_landing_checks", [])),
            created_at=data.get("created_at", now()),
            updated_at=data.get("updated_at", now()),
        )


class MissionControl:
    """Mission Control Tower supervising flight plans, telemetry, drift, and landing verification."""

    def __init__(self, store: Any):
        self.store = store

    @property
    def db(self) -> sqlite3.Connection:
        return self.store.db

    def set_flight_plan(self, plan: Union[FlightPlan, Dict[str, Any]]) -> FlightPlan:
        if isinstance(plan, dict):
            plan_obj = FlightPlan.from_dict(plan)
        elif isinstance(plan, FlightPlan):
            plan_obj = plan
        else:
            raise MissionControlError("Plano de voo deve ser FlightPlan ou dicionario")

        plan_obj.validate()

        with self.store.transaction():
            # Ensure task exists
            task_row = self.db.execute("SELECT id FROM tasks WHERE id=?", (plan_obj.task_id,)).fetchone()
            if not task_row:
                raise ValueError("Tarefa nao encontrada para vincular plano de voo")

            stamp = now()
            plan_obj.updated_at = stamp
            self.db.execute(
                """INSERT INTO flight_plans(
                    task_id, goal, acceptance_criteria, allowed_paths, budget,
                    mandatory_landing_checks, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    goal=excluded.goal,
                    acceptance_criteria=excluded.acceptance_criteria,
                    allowed_paths=excluded.allowed_paths,
                    budget=excluded.budget,
                    mandatory_landing_checks=excluded.mandatory_landing_checks,
                    updated_at=excluded.updated_at
                """,
                (
                    plan_obj.task_id,
                    plan_obj.goal,
                    json.dumps(plan_obj.acceptance_criteria, ensure_ascii=False),
                    json.dumps(plan_obj.allowed_paths, ensure_ascii=False),
                    json.dumps(plan_obj.budget.to_dict(), ensure_ascii=False),
                    json.dumps(plan_obj.mandatory_landing_checks, ensure_ascii=False),
                    plan_obj.created_at,
                    plan_obj.updated_at,
                )
            )
            self.db.execute(
                "UPDATE tasks SET flight_plan_id=? WHERE id=?",
                (plan_obj.task_id, plan_obj.task_id)
            )
            # Re-mirror updated task
            self.store.mirror(self.store.task(plan_obj.task_id))
        return plan_obj

    def get_flight_plan(self, task_id: str) -> Optional[FlightPlan]:
        row = self.db.execute("SELECT * FROM flight_plans WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        return FlightPlan(
            task_id=row["task_id"],
            goal=row["goal"],
            acceptance_criteria=json.loads(row["acceptance_criteria"]),
            allowed_paths=json.loads(row["allowed_paths"]),
            budget=FlightBudget.from_dict(json.loads(row["budget"])),
            mandatory_landing_checks=json.loads(row["mandatory_landing_checks"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def check_clearance(self, task_id: str) -> None:
        """Deterministic pre-flight check before reserving a run or executing an autonomous step."""
        row = self.db.execute(
            "SELECT status, step_count, uncheckpointed_steps, drift_status, created FROM tasks WHERE id=?",
            (task_id,)
        ).fetchone()
        if not row:
            raise ValueError("Tarefa nao encontrada")
        if row["status"] != "active":
            raise ValueError("Tarefa encerrada")

        plan = self.get_flight_plan(task_id)
        if not plan:
            # No flight plan attached; legacy unconstrained task
            return

        uncheckpointed = row["uncheckpointed_steps"] or 0
        step_count = row["step_count"] or 0

        # Check prolonged radio silence
        if uncheckpointed >= plan.budget.max_uncheckpointed_steps:
            raise RadioSilenceError(
                f"Silencio de radio prolongado detectado: {uncheckpointed} passos sem checkpoint "
                f"(limite maximo: {plan.budget.max_uncheckpointed_steps}). "
                f"Execucao bloqueada ate registro de novo checkpoint de progresso."
            )

        # Check budget limits
        if step_count >= plan.budget.max_steps:
            raise BudgetExceededError(
                f"Orcamento de passos excedido: {step_count} passos executados "
                f"(limite: {plan.budget.max_steps}). Reavalie a missao."
            )

        # Check drift status
        if row["drift_status"] == DriftStatus.BLOCKED:
            raise MissionControlError(
                f"Trajetoria bloqueada por desvio deterministico (drift_status: blocked). "
                f"Verifique os eventos de telemetria e restabeleca a rota."
            )

    def record_step(
        self,
        task_id: str,
        action_name: str,
        details: str = "",
        paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Record an execution step in mission telemetry with drift and silence monitoring."""
        with self.store.transaction():
            task_row = self.db.execute(
                "SELECT id, step_count, uncheckpointed_steps, drift_status FROM tasks WHERE id=?",
                (task_id,)
            ).fetchone()
            if not task_row:
                raise ValueError("Tarefa nao encontrada")

            plan = self.get_flight_plan(task_id)

            current_step = (task_row["step_count"] or 0) + 1
            current_uncheckpointed = (task_row["uncheckpointed_steps"] or 0) + 1
            drift_status = task_row["drift_status"] or DriftStatus.NOMINAL

            # 1. Path guardrail check
            path_violation = None
            if plan and paths:
                for p in paths:
                    if not is_path_allowed(p, plan.allowed_paths):
                        drift_status = DriftStatus.BLOCKED
                        self._log_telemetry(
                            task_id, current_step, current_uncheckpointed,
                            DeviationType.PATH_VIOLATION, "error",
                            f"Acesso a caminho nao autorizado: {p}. Caminhos permitidos: {plan.allowed_paths}"
                        )
                        path_violation = p
                        break

            # 2. Radio silence check
            if plan and current_uncheckpointed >= plan.budget.max_uncheckpointed_steps:
                drift_status = DriftStatus.BLOCKED
                self._log_telemetry(
                    task_id, current_step, current_uncheckpointed,
                    DeviationType.PROLONGED_SILENCE, "warning",
                    f"Silencio de radio: {current_uncheckpointed} passos sem checkpoint (limite: {plan.budget.max_uncheckpointed_steps})"
                )

            # 3. Budget exceeded check
            if plan and current_step > plan.budget.max_steps:
                drift_status = DriftStatus.BLOCKED
                self._log_telemetry(
                    task_id, current_step, current_uncheckpointed,
                    DeviationType.BUDGET_EXCEEDED, "critical",
                    f"Orcamento excedido: {current_step} passos > max {plan.budget.max_steps}"
                )

            # Log standard step telemetry
            self._log_telemetry(
                task_id, current_step, current_uncheckpointed,
                "step", "info", f"Action: {action_name}. Details: {details[:200]}"
            )

            self.db.execute(
                "UPDATE tasks SET step_count=?, uncheckpointed_steps=?, drift_status=?, updated=? WHERE id=?",
                (current_step, current_uncheckpointed, drift_status, now(), task_id)
            )

            res = {
                "task_id": task_id,
                "step_count": current_step,
                "uncheckpointed_steps": current_uncheckpointed,
                "drift_status": drift_status,
                "action": action_name,
            }
            # Mirror updated task
            self.store.mirror(self.store.task(task_id))

        if path_violation:
            raise UnauthorizedPathError(
                f"Desvio de rota: acesso ao caminho '{path_violation}' viola os caminhos autorizados pelo plano de voo."
            )

        return res

    def _log_telemetry(
        self,
        task_id: str,
        step_number: int,
        uncheckpointed_steps: int,
        event_type: str,
        severity: str,
        details: str,
    ) -> None:
        self.db.execute(
            """INSERT INTO telemetry_events(
                task_id, step_number, uncheckpointed_steps, event_type, severity, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, step_number, uncheckpointed_steps, event_type, severity, details, now())
        )

    def on_checkpoint_recorded(self, task_id: str) -> None:
        """Reset uncheckpointed steps counter and clear silence block upon verified checkpoint."""
        row = self.db.execute("SELECT step_count, drift_status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return
        step_count = row["step_count"] or 0
        new_drift = DriftStatus.NOMINAL

        self.db.execute(
            "UPDATE tasks SET uncheckpointed_steps=0, drift_status=?, updated=? WHERE id=?",
            (new_drift, now(), task_id)
        )
        self._log_telemetry(
            task_id, step_count, 0, "checkpoint_recorded", "info", "Radio silence cleared by progress checkpoint"
        )

    def record_landing_check(
        self,
        task_id: str,
        check_name: str,
        status: str,
        evidence: Optional[str] = None
    ) -> Dict[str, Any]:
        """Record the outcome of a landing verification check."""
        if not check_name or not isinstance(check_name, str):
            raise ValueError("Nome da verificacao de pouso invalido")
        if status not in ("passed", "failed", "pending"):
            raise ValueError("Status da verificacao deve ser 'passed', 'failed' ou 'pending'")
        if status == "passed" and (not evidence or not isinstance(evidence, str) or not evidence.strip()):
            raise ValueError("Evidencia obrigatoria para verificacao com status 'passed'")

        with self.store.transaction():
            task_row = self.db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task_row:
                raise ValueError("Tarefa nao encontrada")

            stamp = now()
            self.db.execute(
                """INSERT INTO landing_checks(task_id, check_name, status, evidence, verified_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(task_id, check_name) DO UPDATE SET
                       status=excluded.status,
                       evidence=excluded.evidence,
                       verified_at=excluded.verified_at
                """,
                (task_id, check_name, status, evidence, stamp)
            )

            step_count_row = self.db.execute("SELECT step_count FROM tasks WHERE id=?", (task_id,)).fetchone()
            step_count = step_count_row[0] if step_count_row and step_count_row[0] else 0
            self._log_telemetry(
                task_id, step_count, 0, "landing_check_recorded",
                "info" if status == "passed" else "warning",
                f"Check '{check_name}' set to {status}: {evidence or ''}"
            )
            res = {
                "task_id": task_id,
                "check_name": check_name,
                "status": status,
                "evidence": evidence,
                "verified_at": stamp,
            }
            self.store.mirror(self.store.task(task_id))
            return res

    def get_landing_checks(self, task_id: str) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT check_name, status, evidence, verified_at FROM landing_checks WHERE task_id=? ORDER BY check_name ASC",
            (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def verify_landing(self, task_id: str) -> Tuple[bool, List[str]]:
        """Evaluate whether all mandatory landing checks specified in flight plan are passed."""
        plan = self.get_flight_plan(task_id)
        if not plan or not plan.mandatory_landing_checks:
            # No flight plan or no mandatory checks required
            return True, []

        recorded = {
            r["check_name"]: r
            for r in self.get_landing_checks(task_id)
        }

        unmet = []
        for check in plan.mandatory_landing_checks:
            record = recorded.get(check)
            if not record or record["status"] != "passed":
                status_str = record["status"] if record else "not_recorded"
                unmet.append(f"{check} (status: {status_str})")

        return len(unmet) == 0, unmet

    def validate_landing_gate(self, task_id: str) -> None:
        """Gatekeeper: prevents marking task complete if mandatory landing checks are not verified."""
        passed, unmet = self.verify_landing(task_id)
        if not passed:
            raise LandingVerificationError(
                f"Pouso bloqueado deterministico: as seguintes verificacoes obrigatorias nao foram satisfeitas: "
                f"{unmet}. Processo com exit_code 0 nao autoriza conclusao da missao sem pouso validado."
            )

    def telemetry_summary(self, task_id: str) -> Dict[str, Any]:
        """Return summary of steps, deviations, and events for task."""
        task_row = self.db.execute(
            "SELECT step_count, uncheckpointed_steps, drift_status FROM tasks WHERE id=?",
            (task_id,)
        ).fetchone()
        if not task_row:
            raise ValueError("Tarefa nao encontrada")
        events = self.db.execute(
            "SELECT event_type, severity, details, created_at FROM telemetry_events WHERE task_id=? ORDER BY id DESC LIMIT 20",
            (task_id,)
        ).fetchall()
        return {
            "step_count": task_row["step_count"] or 0,
            "uncheckpointed_steps": task_row["uncheckpointed_steps"] or 0,
            "drift_status": task_row["drift_status"] or DriftStatus.NOMINAL,
            "recent_events": [dict(e) for e in events],
        }
