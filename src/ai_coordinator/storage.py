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

          CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);
          CREATE INDEX IF NOT EXISTS idx_runs_task_status ON runs(task_id, status);
          CREATE INDEX IF NOT EXISTS idx_landing_checks_task ON landing_checks(task_id);
          CREATE INDEX IF NOT EXISTS idx_telemetry_events_task ON telemetry_events(task_id);
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
