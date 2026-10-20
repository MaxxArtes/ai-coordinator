"""Atomic SQLite state persistence, optimistic fencing, and conservative reconciliation for AI Coordinator."""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from .process import safe_process_alive, ProcessSupervisor
from .mission import (
    FlightBudget,
    FlightPlan,
    LandingCheck,
    MissionControl,
    MissionControlError,
    RadioSilenceError,
    BudgetExceededError,
    UnauthorizedPathError,
    LandingVerificationError,
    DeviationType,
    DriftStatus,
)
from .dag import (
    DagError,
    CycleDetectedError,
    DagValidationError,
    StaleRevisionError,
    ActiveWorkerFencingError,
    PlanNode,
    PlanEdge,
    PlanRevision,
    TrajectoryProposal,
    TrajectoryEvent,
    validate_dag,
    get_ready_nodes,
)

now = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

FIELDS = {"summary", "decisions", "changed_files", "checks", "pending", "next_step"}

def scrub(text: str) -> str:
    """Basic secret scrubbing for stored texts."""
    if not isinstance(text, str):
        return text
    # Remove common patterns of bearer tokens or keys
    return re.sub(r'(sk-[a-zA-Z0-9_-]{20,})', '[REDACTED_SECRET]', text)

def validate_id(ident: str) -> str:
    if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9_-]{8,64}", ident):
        raise ValueError("Identificador invalido: use letras minusculas, numeros, sublinhado e hifen (8 a 64 chars)")
    return ident

def validate_memory_workstream_id(ident: Optional[str]) -> Optional[str]:
    if ident is None or ident == "":
        return None
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", ident):
        raise ValueError("ID de sessao/workstream ai-memory deve ser UUID valido")
    return ident

def project_path(path: Union[str, Path]) -> str:
    resolved = Path(path).resolve()
    if not resolved.is_dir():
        raise ValueError(f"Diretorio do projeto nao existe: {resolved}")
    return str(resolved)

def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class RunState:
    RESERVED = "reserved"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    STOPPING = "stopping"
    CANCELLED = "cancelled"
    NEEDS_INPUT = "needs_input"
    INTERRUPTED = "interrupted"
    IDENTITY_UNKNOWN = "identity_unknown"
    UNSUPERVISED = "unsupervised"


ACTIVE_RUN_STATES: Set[str] = {
    RunState.RESERVED,
    RunState.STARTING,
    RunState.RUNNING,
    RunState.CANCEL_REQUESTED,
    RunState.STOPPING,
    RunState.NEEDS_INPUT,
    RunState.UNSUPERVISED,
}

TERMINAL_RUN_STATES: Set[str] = {
    RunState.COMPLETE,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.INTERRUPTED,
    RunState.IDENTITY_UNKNOWN,
}

VALID_STATUS_TRANSITIONS: Dict[str, Set[str]] = {
    RunState.RESERVED: {
        RunState.STARTING,
        RunState.RUNNING,  # backwards compatibility
        RunState.INTERRUPTED,
        RunState.CANCEL_REQUESTED,
        RunState.FAILED,
    },
    RunState.STARTING: {
        RunState.RUNNING,
        RunState.INTERRUPTED,
        RunState.CANCEL_REQUESTED,
        RunState.FAILED,
        RunState.UNSUPERVISED,
        RunState.IDENTITY_UNKNOWN,
    },
    RunState.RUNNING: {
        RunState.COMPLETE,
        RunState.FAILED,
        RunState.CANCEL_REQUESTED,
        RunState.STOPPING,
        RunState.INTERRUPTED,
        RunState.NEEDS_INPUT,
        RunState.UNSUPERVISED,
        RunState.IDENTITY_UNKNOWN,
    },
    RunState.CANCEL_REQUESTED: {
        RunState.STOPPING,
        RunState.CANCELLED,
        RunState.COMPLETE,  # normal exit observed concurrent to cancel
        RunState.FAILED,
        RunState.INTERRUPTED,
    },
    RunState.STOPPING: {
        RunState.CANCELLED,
        RunState.COMPLETE,
        RunState.FAILED,
        RunState.INTERRUPTED,
    },
    RunState.NEEDS_INPUT: {
        RunState.RUNNING,
        RunState.CANCEL_REQUESTED,
        RunState.INTERRUPTED,
        RunState.FAILED,
    },
    RunState.UNSUPERVISED: {
        RunState.RUNNING,
        RunState.COMPLETE,
        RunState.FAILED,
        RunState.INTERRUPTED,
        RunState.STOPPING,
        RunState.CANCELLED,
    },
    RunState.IDENTITY_UNKNOWN: {
        RunState.RUNNING,
        RunState.INTERRUPTED,
    },
    RunState.INTERRUPTED: set(),
    RunState.COMPLETE: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


class Store:
    """Atomic SQLite state persistence with optimistic revision fencing and conservative reconciliation."""

    def __init__(self, root: Union[str, Path]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "coordination.sqlite", timeout=20.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self._init_schema()
        self.mission_control = MissionControl(self)

    def _init_schema(self) -> None:
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            ai_memory_workstream_id TEXT,
            flight_plan_id TEXT,
            step_count INTEGER DEFAULT 0,
            uncheckpointed_steps INTEGER DEFAULT 0,
            drift_status TEXT DEFAULT 'nominal'
          );

          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            category TEXT NOT NULL,
            status TEXT NOT NULL,
            client TEXT,
            model TEXT,
            started TEXT NOT NULL,
            ended TEXT,
            attempts TEXT NOT NULL,
            input_path TEXT NOT NULL,
            output_path TEXT,
            error TEXT,
            pid INTEGER,
            owner_pid INTEGER,
            usage TEXT,
            process_create_time INTEGER,
            owner_create_time INTEGER,
            revision INTEGER DEFAULT 1,
            observed_at TEXT,
            last_heartbeat TEXT,
            exit_code INTEGER,
            cancel_requested_at TEXT
          );

          CREATE TABLE IF NOT EXISTS run_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            revision INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
          );

          CREATE TABLE IF NOT EXISTS flight_plans (
            task_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL,
            allowed_paths TEXT NOT NULL,
            budget TEXT NOT NULL,
            mandatory_landing_checks TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
          );

          CREATE TABLE IF NOT EXISTS landing_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            check_name TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence TEXT,
            verified_at TEXT NOT NULL,
            UNIQUE(task_id, check_name)
          );

          CREATE TABLE IF NOT EXISTS telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            step_number INTEGER NOT NULL,
            uncheckpointed_steps INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
          );

          CREATE TABLE IF NOT EXISTS plan_revisions (
            task_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            parent_revision INTEGER,
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            metadata TEXT,
            PRIMARY KEY (task_id, revision)
          );

          CREATE TABLE IF NOT EXISTS plan_nodes (
            task_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            node_id TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            entry_criteria TEXT NOT NULL DEFAULT '[]',
            exit_criteria TEXT NOT NULL DEFAULT '[]',
            deliverables TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            assigned_role TEXT DEFAULT 'worker',
            metadata TEXT,
            PRIMARY KEY (task_id, revision, node_id)
          );

          CREATE TABLE IF NOT EXISTS plan_edges (
            task_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            from_node TEXT NOT NULL,
            to_node TEXT NOT NULL,
            edge_type TEXT NOT NULL DEFAULT 'depends_on',
            metadata TEXT,
            PRIMARY KEY (task_id, revision, from_node, to_node)
          );

          CREATE TABLE IF NOT EXISTS trajectory_proposals (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            base_revision INTEGER NOT NULL,
            proposed_revision INTEGER,
            reason TEXT NOT NULL,
            diff_summary TEXT NOT NULL,
            proposal_data TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'proposed',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            metadata TEXT
          );

          CREATE TABLE IF NOT EXISTS trajectory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            revision INTEGER,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            details TEXT,
            created_at TEXT NOT NULL
          );

          CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);
          CREATE INDEX IF NOT EXISTS idx_runs_task_status ON runs(task_id, status);
          CREATE INDEX IF NOT EXISTS idx_landing_checks_task ON landing_checks(task_id);
          CREATE INDEX IF NOT EXISTS idx_telemetry_events_task ON telemetry_events(task_id);
          CREATE INDEX IF NOT EXISTS idx_plan_nodes_task_rev ON plan_nodes(task_id, revision);
          CREATE INDEX IF NOT EXISTS idx_plan_edges_task_rev ON plan_edges(task_id, revision);
          CREATE INDEX IF NOT EXISTS idx_trajectory_proposals_task ON trajectory_proposals(task_id, status);
          CREATE INDEX IF NOT EXISTS idx_trajectory_events_task ON trajectory_events(task_id);
        """)

        # Dynamic migrations for any existing columns
        run_columns = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        needed_columns = {
            "usage": "TEXT",
            "process_create_time": "INTEGER",
            "owner_create_time": "INTEGER",
            "revision": "INTEGER DEFAULT 1",
            "observed_at": "TEXT",
            "last_heartbeat": "TEXT",
            "exit_code": "INTEGER",
            "cancel_requested_at": "TEXT",
        }
        for col, col_type in needed_columns.items():
            if col not in run_columns:
                try:
                    self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        self.db.commit()

        task_columns = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        needed_task_columns = {
            "ai_memory_workstream_id": "TEXT",
            "flight_plan_id": "TEXT",
            "step_count": "INTEGER DEFAULT 0",
            "uncheckpointed_steps": "INTEGER DEFAULT 0",
            "drift_status": "TEXT DEFAULT 'nominal'",
        }
        for col, col_type in needed_task_columns.items():
            if col not in task_columns:
                try:
                    self.db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        self.db.commit()

    @contextlib.contextmanager
    def transaction(self):
        """Context manager executing BEGIN IMMEDIATE for atomic, isolated writes (re-entrant)."""
        if self.db.in_transaction:
            yield
        else:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def _record_event(
        self,
        run_id: str,
        task_id: str,
        from_status: Optional[str],
        to_status: str,
        revision: int,
        event_type: str,
        details: Optional[str] = None
    ) -> None:
        self.db.execute(
            """INSERT INTO run_events(run_id, task_id, from_status, to_status, revision, event_type, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, task_id, from_status, to_status, revision, event_type, details, now())
        )

    def events(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM run_events WHERE run_id=? ORDER BY id ASC",
            (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def task(self, ident: str) -> Dict[str, Any]:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (validate_id(ident),)).fetchone()
        if not row:
            raise ValueError("Tarefa nao encontrada")
        value = dict(row)
        value["snapshot"] = json.loads(value["snapshot"])
        value["ai_memory_workstream_id"] = validate_memory_workstream_id(
            value.get("ai_memory_workstream_id")
        )
        fp = self.mission_control.get_flight_plan(ident)
        value["flight_plan"] = fp.to_dict() if fp else None
        value["landing_checks"] = self.mission_control.get_landing_checks(ident)
        return value

    def mirror(self, value: Dict[str, Any]) -> None:
        atomic_json(self.root / "tasks" / value["id"] / "state.json", value)

    def new(
        self,
        project: Union[str, Path],
        goal: str,
        ai_memory_workstream_id: Optional[str] = None,
        flight_plan: Optional[Union[Dict[str, Any], FlightPlan]] = None
    ) -> Dict[str, Any]:
        if not goal.strip() or len(goal) > 1500:
            raise ValueError("Objetivo deve ter 1 a 1500 caracteres")
        if os.environ.get("AI_COORDINATOR_ROLE") == "worker":
            raise ValueError("Worker nao pode criar tarefas")
        if ai_memory_workstream_id is None:
            ai_memory_workstream_id = os.environ.get("AI_MEMORY_WORKSTREAM_ID")
        linked_workstream = validate_memory_workstream_id(ai_memory_workstream_id)
        ident, stamp = uuid.uuid4().hex[:16], now()
        snapshot = {
            "summary": "",
            "decisions": [],
            "changed_files": [],
            "checks": [],
            "pending": [],
            "next_step": "",
        }
        with self.transaction():
            self.db.execute(
                "INSERT INTO tasks (id,project,goal,status,revision,snapshot,created,updated,ai_memory_workstream_id,step_count,uncheckpointed_steps,drift_status) VALUES (?,?,?,?,?,?,?,?,?,0,0,'nominal')",
                (ident, project_path(project), scrub(goal), "active", 1,
                 json.dumps(snapshot), stamp, stamp, linked_workstream),
            )
            if flight_plan:
                if isinstance(flight_plan, dict):
                    fp_dict = dict(flight_plan)
                    fp_dict["task_id"] = ident
                    if "goal" not in fp_dict:
                        fp_dict["goal"] = goal
                    self.mission_control.set_flight_plan(fp_dict)
                elif isinstance(flight_plan, FlightPlan):
                    flight_plan.task_id = ident
                    self.mission_control.set_flight_plan(flight_plan)
            created_task = self.task(ident)
            self.mirror(created_task)
        return created_task

    def context(self, project: Union[str, Path]) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id,goal,status,revision,updated,ai_memory_workstream_id FROM tasks WHERE project=? AND status='active' ORDER BY updated DESC LIMIT 8",
            (project_path(project),)
        )
        return [dict(row) for row in rows]

    def checkpoint(self, ident: str, revision: int, patch: Dict[str, Any], status: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(patch, dict) or set(patch) - FIELDS:
            raise ValueError("Checkpoint possui campos desconhecidos")
        for key, value in patch.items():
            if key in ("summary", "next_step"):
                if not isinstance(value, str) or len(value) > 5000:
                    raise ValueError("Resumo/proximo passo: texto de ate 5000 caracteres")
            elif not isinstance(value, list) or len(value) > 40 or any(not isinstance(x, str) or len(x) > 1500 for x in value):
                raise ValueError("Listas: ate 40 textos curtos")
        with self.transaction():
            current = self.task(ident)
            if revision != current["revision"]:
                raise ValueError("Revisao mudou: releia resume antes de gravar")
            if status == "complete":
                self.mission_control.validate_landing_gate(ident)
            current["snapshot"].update(patch)
            snapshot = scrub(json.dumps(current["snapshot"], ensure_ascii=False))
            if len(snapshot) > 12000:
                raise ValueError("Checkpoint excede 12000 caracteres; resuma e referencie arquivos")
            self.db.execute(
                "UPDATE tasks SET snapshot=?,revision=revision+1,status=?,updated=? WHERE id=?",
                (snapshot, status or current["status"], now(), ident)
            )
            self.mission_control.on_checkpoint_recorded(ident)
            updated_task = self.task(ident)
            self.mirror(updated_task)
        return updated_task

    def runs(self, ident: str) -> List[Dict[str, Any]]:
        self.task(ident)
        return [dict(row) for row in self.db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY started DESC LIMIT 8", (ident,))]

    def reserve(self, ident: str, category: str, prompt: str, max_calls: int) -> Tuple[str, Path, int]:
        ident = validate_id(ident)
        with self.transaction():
            task = self.task(ident)
            if task["status"] != "active":
                raise ValueError("Tarefa encerrada")

            # Deterministic mission control clearance check (silence, budget, drift)
            self.mission_control.check_clearance(ident)

            # Check if any run is currently active for this task
            placeholders = ",".join("?" for _ in ACTIVE_RUN_STATES)
            active_runs = self.db.execute(
                f"SELECT * FROM runs WHERE task_id=? AND status IN ({placeholders})",
                [ident] + list(ACTIVE_RUN_STATES)
            ).fetchall()
            if active_runs:
                raise ValueError("Worker em execucao; consulte resume antes de repetir")

            used = sum(len(json.loads(row[0])) for row in self.db.execute("SELECT attempts FROM runs WHERE task_id=?", (ident,)))
            if used >= max_calls:
                raise ValueError("Limite de chamadas da tarefa atingido; reveja a tarefa, nao repita em loop")

            # Record step in mission control telemetry
            self.mission_control.record_step(ident, action_name=f"reserve_{category}", details=f"Prompt: {prompt[:60]}")

            run_id = uuid.uuid4().hex[:16]
            folder = self.root / "tasks" / ident / "runs" / run_id
            folder.mkdir(parents=True, exist_ok=True)
            inp = folder / "input.md"
            inp.write_text(prompt, encoding="utf-8")

            owner_pid = os.getpid()
            owner_ident = ProcessSupervisor.capture_identity(owner_pid)
            owner_create_time = owner_ident.create_time_ns if owner_ident else None

            stamp = now()
            self.db.execute(
                """INSERT INTO runs(
                    id, task_id, category, status, started, attempts, input_path,
                    owner_pid, owner_create_time, revision, last_heartbeat
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (run_id, ident, category, RunState.RESERVED, stamp, "[]", str(inp),
                 owner_pid, owner_create_time, stamp)
            )
            self._record_event(run_id, ident, None, RunState.RESERVED, 1, "reserve", f"Reserved by owner PID {owner_pid}")
            return run_id, folder, max_calls - used

    def update_run(self, ident: str, expected_revision: Optional[int] = None, **values) -> Dict[str, Any]:
        allowed = {
            "status", "client", "model", "ended", "attempts", "output_path",
            "error", "pid", "usage", "process_create_time", "owner_create_time",
            "revision", "observed_at", "last_heartbeat", "exit_code", "cancel_requested_at"
        }
        if not values or set(values) - allowed:
            raise ValueError("Atualizacao de worker invalida")
        with self.transaction():
            row = self.db.execute("SELECT revision, status, task_id FROM runs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("Execucao nao encontrada")
            curr_rev = row["revision"] or 1
            curr_status = row["status"]
            if expected_revision is not None and curr_rev != expected_revision:
                raise ValueError(f"Revisao obsoleta: esperado rev {expected_revision}, atual rev {curr_rev}")

            new_rev = curr_rev + 1
            update_dict = dict(values)
            if "revision" not in update_dict:
                update_dict["revision"] = new_rev

            keys = list(update_dict)
            self.db.execute(
                "UPDATE runs SET " + ",".join(k + "=?" for k in keys) + " WHERE id=?",
                [update_dict[k] for k in keys] + [ident]
            )
            if "status" in update_dict and update_dict["status"] != curr_status:
                self._record_event(
                    ident, row["task_id"], curr_status, update_dict["status"],
                    new_rev, f"update_status_to_{update_dict['status']}", None
                )
            return dict(self.db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone())

    def transition_run(
        self,
        run_id: str,
        to_status: str,
        expected_status: Optional[Union[str, Set[str], Tuple[str, ...]]] = None,
        expected_revision: Optional[int] = None,
        event_type: Optional[str] = None,
        details: Optional[str] = None,
        **values
    ) -> Dict[str, Any]:
        validate_id(run_id)
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Execucao nao encontrada")
            current_status = row["status"]
            current_revision = row["revision"] or 1

            if expected_revision is not None and current_revision != expected_revision:
                raise ValueError(f"Revisao obsoleta: esperado rev {expected_revision}, atual rev {current_revision}")

            if expected_status is not None:
                expected_set = {expected_status} if isinstance(expected_status, str) else set(expected_status)
                if current_status not in expected_set:
                    raise ValueError(f"Conflito de estado: esperado {expected_set}, atual {current_status}")
            else:
                valid_next = VALID_STATUS_TRANSITIONS.get(current_status, set())
                if to_status not in valid_next and to_status != current_status:
                    raise ValueError(f"Transicao de estado invalida: {current_status} -> {to_status}")

            new_revision = current_revision + 1
            now_stamp = now()
            update_fields = dict(values)
            update_fields["status"] = to_status
            update_fields["revision"] = new_revision
            if "observed_at" not in update_fields:
                update_fields["observed_at"] = now_stamp
            if to_status in (RunState.COMPLETE, RunState.FAILED, RunState.CANCELLED):
                if "ended" not in update_fields:
                    update_fields["ended"] = now_stamp
            if to_status == RunState.CANCEL_REQUESTED and "cancel_requested_at" not in update_fields:
                update_fields["cancel_requested_at"] = now_stamp

            set_clause = ", ".join(f"{k}=?" for k in update_fields)
            self.db.execute(
                f"UPDATE runs SET {set_clause} WHERE id=?",
                list(update_fields.values()) + [run_id]
            )

            ev_type = event_type or f"transition_to_{to_status}"
            self._record_event(run_id, row["task_id"], current_status, to_status, new_revision, ev_type, details)
            return dict(self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def record_heartbeat(self, run_id: str, expected_revision: Optional[int] = None) -> None:
        with self.transaction():
            row = self.db.execute("SELECT revision, status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Execucao nao encontrada")
            curr_rev = row["revision"] or 1
            if expected_revision is not None and curr_rev != expected_revision:
                raise ValueError(f"Revisao obsoleta: esperado rev {expected_revision}, atual rev {curr_rev}")
            self.db.execute(
                "UPDATE runs SET last_heartbeat=?, revision=revision+1 WHERE id=?",
                (now(), run_id)
            )

    def request_cancel(self, run_id: str) -> Dict[str, Any]:
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Execucao nao encontrada")
            curr_status = row["status"]
            if curr_status in TERMINAL_RUN_STATES:
                raise ValueError(f"Nao e possivel cancelar execucao com status terminal '{curr_status}'")
            return self.transition_run(
                run_id,
                to_status=RunState.CANCEL_REQUESTED,
                event_type="cancel_requested",
                details="Cancelamento solicitado pelo operador ou sistema"
            )

    def confirm_exit(
        self,
        run_id: str,
        exit_code: Optional[int] = None,
        output_path: Optional[str] = None,
        error: Optional[str] = None,
        **values
    ) -> Dict[str, Any]:
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("Execucao nao encontrada")
            curr_status = row["status"]
            now_stamp = now()

            if curr_status in (RunState.CANCEL_REQUESTED, RunState.STOPPING):
                target_status = RunState.COMPLETE if exit_code == 0 else RunState.CANCELLED
                note = "Cancelamento solicitado; saida normal observada" if exit_code == 0 else "Cancelamento confirmado"
                return self.transition_run(
                    run_id,
                    to_status=target_status,
                    exit_code=exit_code,
                    ended=now_stamp,
                    output_path=output_path,
                    error=error or note,
                    event_type="confirm_exit_after_cancel",
                    details=f"Exit code {exit_code} observed after {curr_status}",
                    **values
                )
            elif exit_code == 0:
                return self.transition_run(
                    run_id,
                    to_status=RunState.COMPLETE,
                    exit_code=0,
                    ended=now_stamp,
                    output_path=output_path,
                    event_type="confirm_exit",
                    details="Process completed with exit code 0",
                    **values
                )
            else:
                return self.transition_run(
                    run_id,
                    to_status=RunState.FAILED,
                    exit_code=exit_code,
                    ended=now_stamp,
                    error=error or f"Processo encerrou com codigo de saida {exit_code}",
                    event_type="confirm_exit",
                    details=f"Process failed with exit code {exit_code}",
                    **values
                )

    def reconcile(self, task_id: Optional[str] = None, silence_threshold_seconds: float = 60.0) -> List[Dict[str, Any]]:
        """Conservative recovery and reconciliation for active runs without fabricating terminal outcomes."""
        reconciled = []
        with self.transaction():
            query = "SELECT * FROM runs WHERE status IN (?, ?, ?, ?, ?, ?, ?)"
            params: list[Any] = [
                RunState.RESERVED,
                RunState.STARTING,
                RunState.RUNNING,
                RunState.CANCEL_REQUESTED,
                RunState.STOPPING,
                RunState.NEEDS_INPUT,
                RunState.UNSUPERVISED,
            ]
            if task_id:
                query += " AND task_id=?"
                params.append(task_id)

            rows = self.db.execute(query, params).fetchall()
            now_stamp = now()

            for row in rows:
                r = dict(row)
                run_id = r["id"]
                status = r["status"]
                owner_pid = r.get("owner_pid")
                owner_create_time = r.get("owner_create_time")
                proc_pid = r.get("pid")
                proc_create_time = r.get("process_create_time")

                # 1. Owner check
                owner_alive = False
                if owner_pid and owner_pid > 0:
                    try:
                        owner_alive = safe_process_alive(owner_pid, expected_create_time_ns=owner_create_time)
                    except (PermissionError, OSError):
                        owner_alive = False

                # 2. Worker process check
                proc_alive = False
                identity_known = True
                if proc_pid and proc_pid > 0:
                    try:
                        proc_alive = safe_process_alive(proc_pid, expected_create_time_ns=proc_create_time)
                    except (PermissionError, OSError):
                        identity_known = False

                if not identity_known:
                    # Identity cannot be verified by OS queries
                    res = self.transition_run(
                        run_id,
                        to_status=RunState.IDENTITY_UNKNOWN,
                        observed_at=now_stamp,
                        error="Identidade do processo impossivel de consultar com certeza no SO",
                        event_type="reconcile_identity_unknown"
                    )
                    reconciled.append(res)
                    continue

                if status in (RunState.RESERVED, RunState.STARTING):
                    if not owner_alive and not proc_alive:
                        # Crash between reservation and process startup: leave exit_code and ended as NULL
                        res = self.transition_run(
                            run_id,
                            to_status=RunState.INTERRUPTED,
                            observed_at=now_stamp,
                            error="Queda antes da inicializacao do processo pelo supervisor",
                            event_type="reconcile_startup_crash"
                        )
                        reconciled.append(res)
                    elif not owner_alive and proc_alive:
                        res = self.transition_run(
                            run_id,
                            to_status=RunState.UNSUPERVISED,
                            observed_at=now_stamp,
                            event_type="reconcile_unsupervised"
                        )
                        reconciled.append(res)

                elif status == RunState.RUNNING:
                    if proc_alive and not owner_alive:
                        # Supervisor dead, worker still running: mark unsupervised, do NOT terminate
                        res = self.transition_run(
                            run_id,
                            to_status=RunState.UNSUPERVISED,
                            observed_at=now_stamp,
                            event_type="reconcile_unsupervised"
                        )
                        reconciled.append(res)
                    elif not proc_alive:
                        # Both supervisor and worker absent without final recording
                        # exit_code and ended remain NULL, observed_at is recorded
                        res = self.transition_run(
                            run_id,
                            to_status=RunState.INTERRUPTED,
                            observed_at=now_stamp,
                            error="Supervisor e cliente ausentes sem registro final",
                            event_type="reconcile_absent_final"
                        )
                        reconciled.append(res)
                    elif owner_alive and proc_alive:
                        # Both alive. Check heartbeat silence
                        # Silencioso, mas processo ainda vivo: nao presumir morte!
                        pass

                elif status == RunState.UNSUPERVISED:
                    if not proc_alive:
                        res = self.transition_run(
                            run_id,
                            to_status=RunState.INTERRUPTED,
                            observed_at=now_stamp,
                            error="Processo nao supervisionado finalizou sem supervisor",
                            event_type="reconcile_unsupervised_exit"
                        )
                        reconciled.append(res)

                elif status in (RunState.CANCEL_REQUESTED, RunState.STOPPING):
                    if not proc_alive:
                        res = self.transition_run(
                            run_id,
                            to_status=RunState.CANCELLED,
                            ended=now_stamp,
                            observed_at=now_stamp,
                            event_type="reconcile_cancellation_confirmed"
                        )
                        reconciled.append(res)

        return reconciled

    def recover(self, task: str, run: str) -> Dict[str, Any]:
        self.task(task)
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE id=? AND task_id=?", (validate_id(run), task)).fetchone()
            if not row or row["status"] not in ACTIVE_RUN_STATES:
                raise ValueError("Execucao pendente nao encontrada")
            row_dict = dict(row)
            if safe_process_alive(row_dict.get("owner_pid"), row_dict.get("owner_create_time")) or safe_process_alive(row_dict.get("pid"), row_dict.get("process_create_time")):
                raise ValueError("Processo ainda existe; nao recuperar nem repetir")
            return self.transition_run(
                run,
                to_status=RunState.INTERRUPTED,
                ended=now(),
                error="Processos ausentes; interrupcao confirmada",
                pid=None,
                process_create_time=None,
                event_type="recover",
                details="Recuperacao manual confirmada"
            )

    def resume(self, task_id: str) -> Dict[str, Any]:
        task_id = validate_id(task_id)
        with self.transaction():
            self.reconcile(task_id=task_id)
            return {"task": self.task(task_id), "recent_runs": self.runs(task_id)}

    def set_flight_plan(self, plan: Union[FlightPlan, Dict[str, Any]]) -> FlightPlan:
        return self.mission_control.set_flight_plan(plan)

    def get_flight_plan(self, ident: str) -> Optional[FlightPlan]:
        return self.mission_control.get_flight_plan(ident)

    def record_landing_check(
        self,
        ident: str,
        check_name: str,
        status: str,
        evidence: Optional[str] = None
    ) -> Dict[str, Any]:
        return self.mission_control.record_landing_check(ident, check_name, status, evidence)

    def get_landing_checks(self, ident: str) -> List[Dict[str, Any]]:
        return self.mission_control.get_landing_checks(ident)

    def record_step(
        self,
        ident: str,
        action_name: str,
        details: str = "",
        paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return self.mission_control.record_step(ident, action_name, details, paths)

    def verify_landing(self, ident: str) -> Tuple[bool, List[str]]:
        return self.mission_control.verify_landing(ident)

    def telemetry_summary(self, ident: str) -> Dict[str, Any]:
        return self.mission_control.telemetry_summary(ident)

    # -------------------------------------------------------------------------
    # Fase 3A: DAG Versionada e Propostas de Trajetoria
    # -------------------------------------------------------------------------

    def create_plan_revision(
        self,
        task_id: str,
        nodes: Union[List[PlanNode], List[Dict[str, Any]], Dict[str, Any]],
        edges: Union[List[PlanEdge], List[Dict[str, Any]]],
        summary: str,
        parent_revision: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PlanRevision:
        """Atomically create an immutable plan revision after validating DAG integrity."""
        task_id = validate_id(task_id)
        if not summary:
            raise DagValidationError("summary obrigatorio para criacao de revisao do plano")

        # Normalize nodes
        nodes_dict: Dict[str, PlanNode] = {}
        if isinstance(nodes, dict):
            for k, v in nodes.items():
                if isinstance(v, PlanNode):
                    nodes_dict[k] = v
                else:
                    d = dict(v)
                    if "node_id" not in d:
                        d["node_id"] = k
                    nodes_dict[k] = PlanNode.from_dict(d)
        elif isinstance(nodes, list):
            for item in nodes:
                node = item if isinstance(item, PlanNode) else PlanNode.from_dict(item)
                nodes_dict[node.node_id] = node

        # Normalize edges
        edges_list: List[PlanEdge] = []
        for e in edges:
            edge = e if isinstance(e, PlanEdge) else PlanEdge.from_dict(e)
            edges_list.append(edge)

        # Validate DAG (detects dangling edges, self-loops, and cycles)
        validate_dag(nodes_dict, edges_list)

        with self.transaction():
            task = self.task(task_id)
            if not task:
                raise ValueError("Tarefa nao encontrada")

            # Determine new revision number and check optimistic fencing
            row = self.db.execute(
                "SELECT revision, status FROM plan_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                (task_id,)
            ).fetchone()

            if row:
                latest_rev = row["revision"]
                if parent_revision is not None and parent_revision != latest_rev:
                    raise StaleRevisionError(
                        f"Revisao base obsoleta: revisao ativa e {latest_rev}, mas foi informada {parent_revision}"
                    )
                new_rev = latest_rev + 1
                resolved_parent = latest_rev
            else:
                if parent_revision is not None and parent_revision != 0:
                    raise StaleRevisionError(f"Revisao pai {parent_revision} informada, mas plano ainda nao possui revisoes")
                new_rev = 1
                resolved_parent = None

            stamp = now()

            # Mark previous active revision as superseded
            if row:
                self.db.execute(
                    "UPDATE plan_revisions SET status='superseded' WHERE task_id=? AND status='active'",
                    (task_id,)
                )

            # Insert immutable plan_revisions
            self.db.execute(
                """INSERT INTO plan_revisions(
                    task_id, revision, parent_revision, summary, status, created_at, metadata
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (task_id, new_rev, resolved_parent, summary, stamp, json.dumps(metadata or {}))
            )

            # Insert plan_nodes
            for nid, node in nodes_dict.items():
                self.db.execute(
                    """INSERT INTO plan_nodes(
                        task_id, revision, node_id, title, description, entry_criteria,
                        exit_criteria, deliverables, status, assigned_role, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id,
                        new_rev,
                        node.node_id,
                        node.title,
                        node.description,
                        json.dumps(node.entry_criteria),
                        json.dumps(node.exit_criteria),
                        json.dumps(node.deliverables),
                        node.status,
                        node.assigned_role,
                        json.dumps(node.metadata),
                    )
                )

            # Insert plan_edges
            for edge in edges_list:
                self.db.execute(
                    """INSERT INTO plan_edges(
                        task_id, revision, from_node, to_node, edge_type, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (task_id, new_rev, edge.from_node, edge.to_node, edge.edge_type, json.dumps(edge.metadata))
                )

            # Record trajectory event
            self._record_trajectory_event(
                task_id=task_id,
                revision=new_rev,
                event_type="plan_revision_created",
                severity="info",
                details=f"Criada revisao {new_rev} com {len(nodes_dict)} nós e {len(edges_list)} arestas. Resumo: {summary}",
            )

            return PlanRevision(
                task_id=task_id,
                revision=new_rev,
                parent_revision=resolved_parent,
                summary=summary,
                status="active",
                created_at=stamp,
                nodes=nodes_dict,
                edges=edges_list,
                metadata=metadata or {},
            )

    def get_plan_revision(self, task_id: str, revision: Optional[int] = None) -> Optional[PlanRevision]:
        """Fetch a specific or the latest active immutable plan revision."""
        task_id = validate_id(task_id)
        if revision is not None:
            rev_row = self.db.execute(
                "SELECT * FROM plan_revisions WHERE task_id=? AND revision=?",
                (task_id, revision)
            ).fetchone()
        else:
            rev_row = self.db.execute(
                "SELECT * FROM plan_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1",
                (task_id,)
            ).fetchone()

        if not rev_row:
            return None

        rev = rev_row["revision"]
        node_rows = self.db.execute(
            "SELECT * FROM plan_nodes WHERE task_id=? AND revision=? ORDER BY node_id ASC",
            (task_id, rev)
        ).fetchall()

        nodes: Dict[str, PlanNode] = {}
        for r in node_rows:
            nodes[r["node_id"]] = PlanNode(
                node_id=r["node_id"],
                title=r["title"],
                description=r["description"] or "",
                entry_criteria=json.loads(r["entry_criteria"] or "[]"),
                exit_criteria=json.loads(r["exit_criteria"] or "[]"),
                deliverables=json.loads(r["deliverables"] or "[]"),
                status=r["status"],
                assigned_role=r["assigned_role"] or "worker",
                metadata=json.loads(r["metadata"] or "{}"),
            )

        edge_rows = self.db.execute(
            "SELECT * FROM plan_edges WHERE task_id=? AND revision=? ORDER BY from_node, to_node ASC",
            (task_id, rev)
        ).fetchall()

        edges = [
            PlanEdge(
                from_node=r["from_node"],
                to_node=r["to_node"],
                edge_type=r["edge_type"] or "depends_on",
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in edge_rows
        ]

        return PlanRevision(
            task_id=task_id,
            revision=rev,
            parent_revision=rev_row["parent_revision"],
            summary=rev_row["summary"],
            status=rev_row["status"],
            created_at=rev_row["created_at"],
            nodes=nodes,
            edges=edges,
            metadata=json.loads(rev_row["metadata"] or "{}"),
        )

    def list_plan_revisions(self, task_id: str) -> List[Dict[str, Any]]:
        """List all immutable plan revisions in historical chronological order."""
        task_id = validate_id(task_id)
        rows = self.db.execute(
            "SELECT task_id, revision, parent_revision, summary, status, created_at FROM plan_revisions WHERE task_id=? ORDER BY revision ASC",
            (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def propose_trajectory(
        self,
        task_id: str,
        base_revision: int,
        proposed_nodes: Union[List[PlanNode], List[Dict[str, Any]], Dict[str, Any]],
        proposed_edges: Union[List[PlanEdge], List[Dict[str, Any]]],
        reason: str,
        diff_summary: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TrajectoryProposal:
        """Create an unactivated candidate trajectory proposal without altering execution."""
        task_id = validate_id(task_id)
        if not reason:
            raise DagValidationError("reason obrigatoria para proposta de trajetoria")

        # Validate base revision exists
        base_rev = self.get_plan_revision(task_id, revision=base_revision)
        if not base_rev:
            raise DagValidationError(f"Revisao base {base_revision} nao encontrada para a tarefa '{task_id}'")

        # Normalize and validate proposed DAG
        nodes_dict: Dict[str, PlanNode] = {}
        if isinstance(proposed_nodes, dict):
            for k, v in proposed_nodes.items():
                if isinstance(v, PlanNode):
                    nodes_dict[k] = v
                else:
                    d = dict(v)
                    if "node_id" not in d:
                        d["node_id"] = k
                    nodes_dict[k] = PlanNode.from_dict(d)
        elif isinstance(proposed_nodes, list):
            for item in proposed_nodes:
                node = item if isinstance(item, PlanNode) else PlanNode.from_dict(item)
                nodes_dict[node.node_id] = node

        edges_list: List[PlanEdge] = []
        for e in proposed_edges:
            edge = e if isinstance(e, PlanEdge) else PlanEdge.from_dict(e)
            edges_list.append(edge)

        # Validate DAG constraints (cycles, dangling edges)
        validate_dag(nodes_dict, edges_list)

        proposal_id = uuid.uuid4().hex[:16]
        stamp = now()
        payload = {
            "nodes": {nid: n.to_dict() for nid, n in nodes_dict.items()},
            "edges": [e.to_dict() for e in edges_list],
        }

        with self.transaction():
            self.db.execute(
                """INSERT INTO trajectory_proposals(
                    id, task_id, base_revision, proposed_revision, reason,
                    diff_summary, proposal_data, status, created_at, metadata
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, 'proposed', ?, ?)""",
                (
                    proposal_id,
                    task_id,
                    base_revision,
                    reason,
                    diff_summary,
                    json.dumps(payload),
                    stamp,
                    json.dumps(metadata or {}),
                )
            )

            self._record_trajectory_event(
                task_id=task_id,
                revision=base_revision,
                event_type="trajectory_proposed",
                severity="info",
                details=f"Proposta {proposal_id} criada sobre revisao {base_revision}: {reason}",
            )

        return TrajectoryProposal(
            id=proposal_id,
            task_id=task_id,
            base_revision=base_revision,
            proposed_revision=None,
            reason=reason,
            diff_summary=diff_summary,
            nodes=nodes_dict,
            edges=edges_list,
            status="proposed",
            created_at=stamp,
            metadata=metadata or {},
        )

    def get_trajectory_proposal(self, proposal_id: str) -> Optional[TrajectoryProposal]:
        """Fetch trajectory proposal by ID."""
        row = self.db.execute(
            "SELECT * FROM trajectory_proposals WHERE id=?",
            (proposal_id,)
        ).fetchone()
        if not row:
            return None

        payload = json.loads(row["proposal_data"] or "{}")
        nodes = {nid: PlanNode.from_dict(nd) for nid, nd in payload.get("nodes", {}).items()}
        edges = [PlanEdge.from_dict(ed) for ed in payload.get("edges", [])]

        return TrajectoryProposal(
            id=row["id"],
            task_id=row["task_id"],
            base_revision=row["base_revision"],
            proposed_revision=row["proposed_revision"],
            reason=row["reason"],
            diff_summary=row["diff_summary"],
            nodes=nodes,
            edges=edges,
            status=row["status"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def list_trajectory_proposals(self, task_id: str, status: Optional[str] = None) -> List[TrajectoryProposal]:
        """List proposals for a given task, optionally filtered by status."""
        task_id = validate_id(task_id)
        if status:
            rows = self.db.execute(
                "SELECT id FROM trajectory_proposals WHERE task_id=? AND status=? ORDER BY created_at ASC",
                (task_id, status)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT id FROM trajectory_proposals WHERE task_id=? ORDER BY created_at ASC",
                (task_id,)
            ).fetchall()

        proposals = []
        for r in rows:
            p = self.get_trajectory_proposal(r["id"])
            if p:
                proposals.append(p)
        return proposals

    def resolve_trajectory_proposal(
        self,
        proposal_id: str,
        decision: str,
        notes: str = ""
    ) -> TrajectoryProposal:
        """Resolve a trajectory proposal (approved or rejected)."""
        valid_decisions = {"approved", "rejected", "cancelled"}
        if decision not in valid_decisions:
            raise ValueError(f"Decisao '{decision}' invalida. Use uma de: {valid_decisions}")

        with self.transaction():
            p = self.get_trajectory_proposal(proposal_id)
            if not p:
                raise ValueError("Proposta nao encontrada")
            if p.status in ("activated", "cancelled"):
                raise ValueError(f"Proposta em status terminal '{p.status}' nao pode ser alterada")

            stamp = now()
            self.db.execute(
                "UPDATE trajectory_proposals SET status=?, resolved_at=? WHERE id=?",
                (decision, stamp, proposal_id)
            )

            self._record_trajectory_event(
                task_id=p.task_id,
                revision=p.base_revision,
                event_type=f"trajectory_proposal_{decision}",
                severity="info",
                details=f"Proposta {proposal_id} marcada como '{decision}'. Notas: {notes}",
            )

        return self.get_trajectory_proposal(proposal_id)  # type: ignore

    def activate_trajectory_proposal(
        self,
        proposal_id: str,
        expected_base_revision: Optional[int] = None
    ) -> PlanRevision:
        """Safely activate a trajectory proposal, enforcing optimistic fencing and worker isolation."""
        with self.transaction():
            proposal = self.get_trajectory_proposal(proposal_id)
            if not proposal:
                raise ValueError("Proposta nao encontrada")
            if proposal.status not in ("proposed", "approved"):
                raise ValueError(f"Proposta em status '{proposal.status}' nao pode ser ativada")

            task_id = proposal.task_id

            # 1. Optimistic fencing check against current active revision
            current_active = self.get_plan_revision(task_id)
            active_rev_num = current_active.revision if current_active else 0
            if expected_base_revision is not None and expected_base_revision != active_rev_num:
                raise StaleRevisionError(
                    f"Revisao base obsoleta: revisao ativa e {active_rev_num}, mas esperada {expected_base_revision}"
                )
            if proposal.base_revision != active_rev_num:
                raise StaleRevisionError(
                    f"A proposta foi formulada sobre revisao {proposal.base_revision}, mas a ativa e {active_rev_num}"
                )

            # 2. Process Safety Fencing: verify no active worker runs are lingering
            placeholders = ",".join("?" for _ in ACTIVE_RUN_STATES)
            active_runs = self.db.execute(
                f"SELECT id, status, pid FROM runs WHERE task_id=? AND status IN ({placeholders})",
                [task_id] + list(ACTIVE_RUN_STATES)
            ).fetchall()

            if active_runs:
                running_ids = [f"{r['id']} ({r['status']})" for r in active_runs]
                raise ActiveWorkerFencingError(
                    f"Ativacao bloqueada por seguranca de processos: existem {len(active_runs)} worker(s) ativos "
                    f"[{', '.join(running_ids)}]. Encerre ou reconcilie os processos antes de ativar nova trajetoria."
                )

            # 3. Create the new immutable plan revision
            new_rev = self.create_plan_revision(
                task_id=task_id,
                nodes=list(proposal.nodes.values()),
                edges=proposal.edges,
                summary=f"Ativacao da proposta {proposal_id}: {proposal.reason}",
                parent_revision=active_rev_num if active_rev_num > 0 else None,
                metadata={"activated_from_proposal": proposal_id},
            )

            # 4. Mark proposal as activated
            stamp = now()
            self.db.execute(
                "UPDATE trajectory_proposals SET status='activated', proposed_revision=?, resolved_at=? WHERE id=?",
                (new_rev.revision, stamp, proposal_id)
            )

            self._record_trajectory_event(
                task_id=task_id,
                revision=new_rev.revision,
                event_type="trajectory_activated",
                severity="info",
                details=f"Proposta {proposal_id} ativada gerando revisao {new_rev.revision}",
            )

            return new_rev

    def update_node_status(
        self,
        task_id: str,
        revision: int,
        node_id: str,
        status: str,
        deliverables: Optional[List[str]] = None,
    ) -> PlanNode:
        """Update node execution status and deliverables within a plan revision."""
        task_id = validate_id(task_id)
        valid_statuses = {"pending", "ready", "in_progress", "completed", "blocked", "skipped"}
        if status not in valid_statuses:
            raise DagValidationError(f"Status '{status}' invalido")

        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM plan_nodes WHERE task_id=? AND revision=? AND node_id=?",
                (task_id, revision, node_id)
            ).fetchone()
            if not row:
                raise ValueError(f"No '{node_id}' nao encontrado na revisao {revision}")

            deliv = deliverables if deliverables is not None else json.loads(row["deliverables"] or "[]")
            self.db.execute(
                "UPDATE plan_nodes SET status=?, deliverables=? WHERE task_id=? AND revision=? AND node_id=?",
                (status, json.dumps(deliv), task_id, revision, node_id)
            )

            self._record_trajectory_event(
                task_id=task_id,
                revision=revision,
                event_type="node_status_updated",
                severity="info",
                details=f"No '{node_id}' atualizado para status '{status}'",
            )

            updated_row = self.db.execute(
                "SELECT * FROM plan_nodes WHERE task_id=? AND revision=? AND node_id=?",
                (task_id, revision, node_id)
            ).fetchone()

            return PlanNode(
                node_id=updated_row["node_id"],
                title=updated_row["title"],
                description=updated_row["description"] or "",
                entry_criteria=json.loads(updated_row["entry_criteria"] or "[]"),
                exit_criteria=json.loads(updated_row["exit_criteria"] or "[]"),
                deliverables=json.loads(updated_row["deliverables"] or "[]"),
                status=updated_row["status"],
                assigned_role=updated_row["assigned_role"] or "worker",
                metadata=json.loads(updated_row["metadata"] or "{}"),
            )

    def _record_trajectory_event(
        self,
        task_id: str,
        revision: Optional[int],
        event_type: str,
        severity: str = "info",
        details: str = ""
    ) -> None:
        self.db.execute(
            """INSERT INTO trajectory_events(
                task_id, revision, event_type, severity, details, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, revision, event_type, severity, scrub(details), now())
        )

    def get_trajectory_events(self, task_id: str, revision: Optional[int] = None) -> List[TrajectoryEvent]:
        """Fetch audit events for trajectory decisions."""
        task_id = validate_id(task_id)
        if revision is not None:
            rows = self.db.execute(
                "SELECT * FROM trajectory_events WHERE task_id=? AND revision=? ORDER BY id ASC",
                (task_id, revision)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM trajectory_events WHERE task_id=? ORDER BY id ASC",
                (task_id,)
            ).fetchall()

        return [
            TrajectoryEvent(
                id=r["id"],
                task_id=r["task_id"],
                revision=r["revision"],
                event_type=r["event_type"],
                severity=r["severity"],
                details=r["details"] or "",
                created_at=r["created_at"],
            )
            for r in rows
        ]
