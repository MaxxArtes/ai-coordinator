"""Atomic SQLite state persistence, optimistic fencing, and conservative reconciliation for AI Coordinator."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
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
    ProposalNotApprovedError,
    UnauthorizedOverrideError,
    PlanNode,
    PlanEdge,
    PlanRevision,
    TrajectoryProposal,
    TrajectoryEvent,
    validate_dag,
    get_ready_nodes,
)
from .invariants import (
    InvariantDefinition,
    InvariantEvaluation,
    InvariantStatus,
    InvariantVerificationError,
    InvariantOracle,
    compute_sha256,
    generate_verification_token,
)
from .resource_ledger import (
    ResourceRecord,
    ResourceEvent,
    ResourceState,
    ResourceType,
    ResourceAction,
    ResourceLedger,
    UnresolvedResourceError,
)
from .experience import (
    ExperienceRecord,
    ExperienceStore,
    ExperienceCategory,
    ExperienceOutcome,
    ExperienceVisibility,
    ExperienceAuthor,
    normalize_project_id,
    project_id_candidates,
)
from .insights import (
    Insight,
    InsightEngine,
    InsightType,
    InsightStatus,
    InsightPresentation,
    AutonomyLevel,
)
from .knowledge import UnifiedKnowledgeCore
from .knowledge_learning import KnowledgeLearning
from .reconstruction import (
    TrajectoryReconstructor,
    AlternativeRoutePreview,
    calculate_reconnection_points,
    calculate_upstream_prerequisites,
    splice_alternative_route,
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

UNCERTAIN_RUN_STATES: Set[str] = {
    RunState.IDENTITY_UNKNOWN,
}

TERMINAL_RUN_STATES: Set[str] = {
    RunState.COMPLETE,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.INTERRUPTED,
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
        RunState.CANCEL_REQUESTED,
        RunState.CANCELLED,
    },
    RunState.INTERRUPTED: set(),
    RunState.COMPLETE: set(),
    RunState.FAILED: set(),
    RunState.CANCELLED: set(),
}


class Store:
    """Atomic SQLite state persistence with optimistic revision fencing and conservative reconciliation."""

    def __init__(self, root: Union[str, Path], supervisor_key: Optional[str] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "coordination.sqlite", timeout=20.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=20000")
        self._init_schema()
        self.mission_control = MissionControl(self)
        self.resource_ledger = ResourceLedger(self.db, backup_dir=self.root / "backups")
        self.experience_store = ExperienceStore(self.db)
        self.knowledge_core = UnifiedKnowledgeCore(self.db)
        self.knowledge_learning = KnowledgeLearning(self.knowledge_core)
        self.insight_engine = InsightEngine(self.db, self.experience_store)
        self.reconstructor = TrajectoryReconstructor(self)
        self.supervisor_secret = secrets.token_bytes(32)
        self.supervisor_key = str(supervisor_key).strip() if supervisor_key else secrets.token_hex(32)
        self.admin_token = secrets.token_hex(32)
        self.oracle = InvariantOracle(run_lookup=self.get_run_for_task, secret=self.supervisor_secret)

    def issue_admin_override_token(self, supervisor_key: str, justification: str) -> str:
        """Issue an authentic supervisor override token for audited administrative operations.
        Requires genuine supervisor authentication key.
        """
        if not supervisor_key or not hmac.compare_digest(str(supervisor_key).strip(), self.supervisor_key):
            raise UnauthorizedOverrideError("Emissao de token administrativo negada: chave de supervisor ausente ou invalida")
        if not justification or not justification.strip():
            raise ValueError("Justificativa formal obrigatoria para emissao de token administrativo")
        return self.admin_token

    def issue_evidence_token(
        self,
        task_id: str,
        plan_revision: int,
        invariant_id: str,
        source: str,
        run_id: Optional[str] = None,
        artifact_sha256: Optional[str] = None,
        exit_code: int = 0,
        nonce: Optional[str] = None,
    ) -> str:
        """Issue an authentic HMAC-SHA256 verification token signed by the supervisor secret."""
        return self.oracle.issue_evidence_token(
            task_id=task_id,
            plan_revision=plan_revision,
            invariant_id=invariant_id,
            source=source,
            run_id=run_id,
            artifact_sha256=artifact_sha256,
            exit_code=exit_code,
            nonce=nonce,
        )

    def get_run_for_task(self, task_id: str, run_id: str) -> Optional[Dict[str, Any]]:
        """Lookup execution run record for a specific task."""
        row = self.db.execute("SELECT * FROM runs WHERE id=? AND task_id=?", (run_id, task_id)).fetchone()
        return dict(row) if row else None

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

          CREATE TABLE IF NOT EXISTS trajectory_predictions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL,
            current_node TEXT,
            status TEXT NOT NULL,
            confidence TEXT NOT NULL,
            prediction_data TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            created_at TEXT NOT NULL
          );

          CREATE TABLE IF NOT EXISTS invariants (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            description TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            verifier_type TEXT NOT NULL,
            target_artifact TEXT,
            require_authenticity_proof INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
          );

          CREATE TABLE IF NOT EXISTS invariant_evaluations (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL,
            invariant_id TEXT NOT NULL,
            status TEXT NOT NULL,
            evidence TEXT,
            verified INTEGER NOT NULL DEFAULT 0,
            waiver_authorized INTEGER NOT NULL DEFAULT 0,
            waiver_reason TEXT,
            evaluated_at TEXT NOT NULL,
            details TEXT
          );

          CREATE TABLE IF NOT EXISTS resource_ledger (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL,
            run_id TEXT,
            resource_type TEXT NOT NULL,
            canonical_id TEXT NOT NULL,
            action TEXT NOT NULL,
            pre_hash TEXT,
            post_hash TEXT,
            compensable INTEGER NOT NULL DEFAULT 1,
            compensation_action TEXT,
            requires_authorization INTEGER NOT NULL DEFAULT 0,
            idempotency_key TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT,
            UNIQUE(task_id, idempotency_key)
          );

          CREATE TABLE IF NOT EXISTS resource_ledger_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resource_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            plan_revision INTEGER NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            event_type TEXT NOT NULL,
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
          CREATE INDEX IF NOT EXISTS idx_trajectory_predictions_task_rev ON trajectory_predictions(task_id, plan_revision);
          CREATE INDEX IF NOT EXISTS idx_invariants_task ON invariants(task_id);
          CREATE INDEX IF NOT EXISTS idx_invariant_evals_task_rev ON invariant_evaluations(task_id, plan_revision);
          CREATE INDEX IF NOT EXISTS idx_resource_ledger_task_rev ON resource_ledger(task_id, plan_revision);
          CREATE INDEX IF NOT EXISTS idx_resource_ledger_status ON resource_ledger(task_id, status);
          CREATE INDEX IF NOT EXISTS idx_resource_events_res ON resource_ledger_events(resource_id);

          CREATE TABLE IF NOT EXISTS experience_records (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            task_id TEXT,
            category TEXT NOT NULL,
            problem_pattern TEXT NOT NULL,
            solution_pattern TEXT NOT NULL,
            outcome TEXT NOT NULL,
            context_tags TEXT NOT NULL DEFAULT '[]',
            confidence TEXT NOT NULL DEFAULT 'medium',
            author TEXT NOT NULL DEFAULT 'agent',
            visibility TEXT NOT NULL DEFAULT 'project_shared',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
          );

          CREATE TABLE IF NOT EXISTS insights (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            task_id TEXT,
            type TEXT NOT NULL,
            author TEXT NOT NULL,
            origin TEXT NOT NULL,
            visibility TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'incubating',
            content TEXT NOT NULL,
            alternatives TEXT NOT NULL DEFAULT '[]',
            suggested_first_step TEXT,
            related_tasks TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            presentation TEXT NOT NULL DEFAULT 'contextual',
            provenance TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
          );

          CREATE INDEX IF NOT EXISTS idx_experience_project ON experience_records(project_id, category);
          CREATE INDEX IF NOT EXISTS idx_experience_visibility ON experience_records(visibility);
          CREATE INDEX IF NOT EXISTS idx_insights_project ON insights(project_id, status);
          CREATE INDEX IF NOT EXISTS idx_insights_visibility ON insights(visibility);
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

        inv_columns = {r[1] for r in self.db.execute("PRAGMA table_info(invariants)")}
        if "require_authenticity_proof" not in inv_columns:
            try:
                self.db.execute("ALTER TABLE invariants ADD COLUMN require_authenticity_proof INTEGER NOT NULL DEFAULT 0")
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
            task_data = self.task(task_id)
            contextual = self.insight_engine.get_contextual_insights(
                project_id=normalize_project_id(task_data["project"]) or task_data["project"],
                task_id=task_id,
                caller_role="agent",
            )
            return {
                "task": task_data,
                "recent_runs": self.runs(task_id),
                "contextual_insights": [i.to_dict() for i in contextual],
            }

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
        expected_base_revision: Optional[int] = None,
        require_approval: bool = True,
        enforce_invariants: bool = True,
        enforce_clean_resources: bool = True,
        caller_role: Optional[str] = None,
        admin_override_reason: Optional[str] = None,
        admin_token: Optional[str] = None,
    ) -> PlanRevision:
        """Safely activate a trajectory proposal, enforcing approval, optimistic fencing, worker isolation, clean resources, and invariant verification."""
        with self.transaction():
            # Administrative override check: disabling safety fencing requires authentic supervisor admin token
            if not require_approval or not enforce_invariants or not enforce_clean_resources:
                if not admin_token or not hmac.compare_digest(str(admin_token).strip(), self.admin_token):
                    raise UnauthorizedOverrideError(
                        "Desativacao de barreiras de seguranca de ativacao rejeitada: token administrativo de supervisor ausente ou invalido. "
                        "A autodeclaracao de 'caller_role' nao confere autoridade administrativa."
                    )
                if not admin_override_reason or not admin_override_reason.strip():
                    raise UnauthorizedOverrideError(
                        "Desativacao de barreiras de seguranca de ativacao rejeitada: justificativa formal obrigatoria ausente ('admin_override_reason')."
                    )

            proposal = self.get_trajectory_proposal(proposal_id)
            if not proposal:
                raise ValueError("Proposta nao encontrada")

            task_id = proposal.task_id

            # 1. Require formal approval
            if require_approval and proposal.status != "approved":
                raise ProposalNotApprovedError(
                    f"Proposta em status '{proposal.status}' nao pode ser ativada. "
                    "A proposta deve ser formalmente aprovada ('approved') antes da ativacao."
                )
            elif proposal.status not in ("proposed", "approved"):
                raise ValueError(f"Proposta em status '{proposal.status}' nao pode ser ativada")

            # 2. Optimistic fencing check against current active revision
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

            # 3. Process Safety Fencing: verify no active worker runs OR identity_unknown runs are lingering
            blocking_states = ACTIVE_RUN_STATES | UNCERTAIN_RUN_STATES
            placeholders = ",".join("?" for _ in blocking_states)
            lingering_runs = self.db.execute(
                f"SELECT id, status, pid, process_create_time FROM runs WHERE task_id=? AND status IN ({placeholders})",
                [task_id] + list(blocking_states)
            ).fetchall()

            if lingering_runs:
                running_ids = [f"{r['id']} ({r['status']})" for r in lingering_runs]
                raise ActiveWorkerFencingError(
                    f"Ativacao bloqueada por seguranca de processos: existem {len(lingering_runs)} worker(s) ativos ou com identidade incerta "
                    f"[{', '.join(running_ids)}]. Encerre ou reconcilie os processos antes de ativar nova trajetoria."
                )

            # 3b. Operating System Process Neutralization Check:
            # Verify that no worker processes with PIDs belonging to this task are still running in the OS
            runs_with_pids = self.db.execute(
                "SELECT id, status, pid, process_create_time FROM runs WHERE task_id=? AND pid IS NOT NULL AND pid > 0",
                (task_id,)
            ).fetchall()
            for r in runs_with_pids:
                pid = r["pid"]
                c_time = r["process_create_time"]
                try:
                    is_alive = safe_process_alive(pid, expected_create_time_ns=c_time)
                except Exception as exc:
                    raise ActiveWorkerFencingError(
                        f"Ativacao bloqueada por falha na consulta de status do processo no SO: "
                        f"o worker run '{r['id']}' (PID {pid}) nao pode ser verificado com seguranca ({exc}). "
                        "Ativacao abortada preventivamente para evitar execucoes fantasmas (fail-closed)."
                    )
                if is_alive:
                    raise ActiveWorkerFencingError(
                        f"Ativacao bloqueada por presenca de processo ativo no SO: o worker run '{r['id']}' "
                        f"(PID {pid}, status={r['status']}) ainda esta ativo no sistema operacional. "
                        "Neutralize e encerre o processo fisicamente antes de ativar nova trajetoria."
                    )

            # 4. Resource Ledger Cleanliness Fencing (Scenario 12)
            if enforce_clean_resources:
                uncertain_count = self.db.execute(
                    "SELECT COUNT(*) FROM resource_ledger WHERE task_id=? AND status=?",
                    (task_id, ResourceState.UNCERTAIN),
                ).fetchone()[0]
                if uncertain_count > 0:
                    raise UnresolvedResourceError(
                        f"Ativacao bloqueada: existem {uncertain_count} recurso(s) em estado incerto ('uncertain') "
                        f"na tarefa {task_id}. Reconcilie os recursos antes de ativar nova trajetoria."
                    )

            # 5. Invariant Oracle Verification Fencing (Scenario 12)
            if enforce_invariants:
                req_invariants = self.db.execute(
                    "SELECT id, description, verifier_type, target_artifact, require_authenticity_proof FROM invariants WHERE task_id=? AND required=1",
                    (task_id,),
                ).fetchall()
                for inv in req_invariants:
                    inv_id = inv["id"]
                    eval_row = self.db.execute(
                        "SELECT status, verified, evidence FROM invariant_evaluations WHERE task_id=? AND plan_revision=? AND invariant_id=? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
                        (task_id, active_rev_num, inv_id),
                    ).fetchone()
                    if not eval_row:
                        raise InvariantVerificationError(
                            f"Ativacao bloqueada por oraculo de invariantes: invariante obrigatorio '{inv_id}' "
                            f"nao possui avaliacao para a revisao ativa {active_rev_num}."
                        )
                    if eval_row["status"] not in (InvariantStatus.PASS, InvariantStatus.NOT_APPLICABLE) or not eval_row["verified"]:
                        raise InvariantVerificationError(
                            f"Ativacao bloqueada por oraculo de invariantes: invariante obrigatorio '{inv_id}' "
                            f"possui status '{eval_row['status']}' (verificado={bool(eval_row['verified'])})."
                        )

                    # Revalidate physical artifact integrity at activation time
                    ev_json = eval_row["evidence"]
                    ev_dict = json.loads(ev_json) if ev_json else {}
                    target_artifact = ev_dict.get("artifact_path") or inv["target_artifact"]
                    expected_sha = ev_dict.get("artifact_sha256")
                    if target_artifact:
                        p_art = Path(target_artifact)
                        if not p_art.is_file():
                            raise InvariantVerificationError(
                                f"Ativacao bloqueada por integridade de artefatos: artefato verificado '{target_artifact}' "
                                "nao existe mais no disco."
                            )
                        if expected_sha:
                            curr_sha = compute_sha256(p_art)
                            if curr_sha.lower() != expected_sha.strip().lower():
                                raise InvariantVerificationError(
                                    f"Ativacao bloqueada: integridade do artefato '{target_artifact}' foi violada apos a avaliacao. "
                                    f"Hash esperado {expected_sha}, hash atual no disco {curr_sha}."
                                )

                    # Revalidate cryptographic provenance at activation time
                    inv_requires_auth = bool(inv["require_authenticity_proof"]) if "require_authenticity_proof" in inv.keys() else False
                    if inv_requires_auth or ev_dict.get("verification_token"):
                        token = ev_dict.get("verification_token")
                        if not token:
                            raise InvariantVerificationError(
                                f"Ativacao bloqueada por proveniencia de invariantes: o invariante obrigatorio '{inv_id}' "
                                "exige comprovacao formal, mas a avaliacao ativa nao contem token criptografico HMAC do supervisor."
                            )
                        expected_token = generate_verification_token(
                            task_id=task_id,
                            plan_revision=active_rev_num,
                            invariant_id=inv_id,
                            source=ev_dict.get("source", inv["verifier_type"]),
                            run_id=ev_dict.get("run_id"),
                            artifact_sha256=ev_dict.get("artifact_sha256"),
                            exit_code=int(ev_dict.get("exit_code", 0)),
                            nonce=ev_dict.get("nonce"),
                            secret=self.supervisor_secret,
                        )
                        if not hmac.compare_digest(str(token).strip(), expected_token):
                            raise InvariantVerificationError(
                                f"Ativacao bloqueada por adulteracao de avaliacao: o token HMAC da avaliacao do invariante '{inv_id}' "
                                "nao coincide com a chave do supervisor. Avaliacao rejeitada no gate de ativacao."
                            )

            # 6. Create the new immutable plan revision
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

    def record_trajectory_prediction(self, prediction: Any) -> str:
        """Persist a trajectory prediction and corresponding audit event."""
        pred_dict = prediction.to_dict() if hasattr(prediction, "to_dict") else dict(prediction)
        task_id = validate_id(pred_dict.get("task_id", ""))
        stamp = now()
        pred_id = pred_dict.get("id") or uuid.uuid4().hex[:16]
        rev = int(pred_dict.get("plan_revision", 1))
        status = str(pred_dict.get("status", "nominal"))
        confidence = str(pred_dict.get("confidence", "unknown"))
        rec = str(pred_dict.get("recommendation", "continue"))
        curr_node = pred_dict.get("current_node")

        severity = "warning" if status == "at_risk" else ("error" if status == "deviated" else "info")
        reason = str(pred_dict.get("reason", ""))

        with self.transaction():
            self.db.execute(
                """INSERT OR REPLACE INTO trajectory_predictions(
                    id, task_id, plan_revision, current_node, status, confidence,
                    prediction_data, recommendation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pred_id,
                    task_id,
                    rev,
                    curr_node,
                    status,
                    confidence,
                    json.dumps(pred_dict),
                    rec,
                    stamp,
                )
            )

            self._record_trajectory_event(
                task_id=task_id,
                revision=rev,
                event_type="trajectory_prediction",
                severity=severity,
                details=f"Predicao {status} ({confidence}): {reason}",
            )

        return pred_id

    def get_trajectory_prediction(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """Fetch trajectory prediction by ID."""
        row = self.db.execute(
            "SELECT * FROM trajectory_predictions WHERE id=?",
            (prediction_id,)
        ).fetchone()
        if not row:
            return None
        return json.loads(row["prediction_data"] or "{}")

    def list_trajectory_predictions(
        self,
        task_id: str,
        revision: Optional[int] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """List historical trajectory predictions for task and optional revision."""
        task_id = validate_id(task_id)
        if revision is not None:
            rows = self.db.execute(
                "SELECT prediction_data FROM trajectory_predictions WHERE task_id=? AND plan_revision=? ORDER BY created_at DESC LIMIT ?",
                (task_id, revision, limit)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT prediction_data FROM trajectory_predictions WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
                (task_id, limit)
            ).fetchall()
        return [json.loads(r["prediction_data"]) for r in rows]

    def get_latest_trajectory_prediction(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the most recent trajectory prediction for task."""
        task_id = validate_id(task_id)
        row = self.db.execute(
            "SELECT prediction_data FROM trajectory_predictions WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,)
        ).fetchone()
        if not row:
            return None
        return json.loads(row["prediction_data"])

    # -------------------------------------------------------------------------
    # Invariant Oracle Storage Methods
    # -------------------------------------------------------------------------
    def create_invariant(
        self,
        task_id: str,
        description: str,
        required: bool = True,
        verifier_type: str = "trusted_test_runner",
        target_artifact: Optional[str] = None,
        require_authenticity_proof: bool = False,
        invariant_id: Optional[str] = None,
    ) -> InvariantDefinition:
        """Register an invariant constraint for a task."""
        task_id = validate_id(task_id)
        inv_id = invariant_id or f"inv-{uuid.uuid4().hex[:8]}"
        stamp = now()
        with self.transaction():
            self.db.execute(
                """
                INSERT OR REPLACE INTO invariants (
                    id, task_id, description, required, verifier_type, target_artifact, require_authenticity_proof, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inv_id,
                    task_id,
                    description,
                    1 if required else 0,
                    verifier_type,
                    target_artifact,
                    1 if require_authenticity_proof else 0,
                    stamp,
                ),
            )
        return InvariantDefinition(
            id=inv_id,
            task_id=task_id,
            description=description,
            required=required,
            verifier_type=verifier_type,
            target_artifact=target_artifact,
            require_authenticity_proof=require_authenticity_proof,
            created_at=stamp,
        )

    register_invariant = create_invariant

    def get_invariants(self, task_id: str) -> List[InvariantDefinition]:
        """Fetch all invariants registered for a task."""
        task_id = validate_id(task_id)
        rows = self.db.execute(
            "SELECT * FROM invariants WHERE task_id=? ORDER BY created_at ASC",
            (task_id,),
        ).fetchall()
        return [
            InvariantDefinition(
                id=r["id"],
                task_id=r["task_id"],
                description=r["description"],
                required=bool(r["required"]),
                verifier_type=r["verifier_type"],
                target_artifact=r["target_artifact"],
                require_authenticity_proof=bool(r["require_authenticity_proof"]) if "require_authenticity_proof" in r.keys() else False,
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def record_invariant_evaluation(self, evaluation: InvariantEvaluation) -> InvariantEvaluation:
        """Persist an invariant evaluation result in SQLite."""
        # Enforce provenance: prevent unauthorized direct insertion of verified=True without authentic token
        inv_row = self.db.execute(
            "SELECT require_authenticity_proof, verifier_type FROM invariants WHERE task_id=? AND id=?",
            (evaluation.task_id, evaluation.invariant_id),
        ).fetchone()
        if inv_row:
            req_auth = bool(inv_row["require_authenticity_proof"]) if "require_authenticity_proof" in inv_row.keys() else False
            ev = evaluation.evidence or {}
            tok = ev.get("verification_token")
            if req_auth and evaluation.verified and not tok:
                raise InvariantVerificationError(
                    f"Tentativa de registrar avaliacao 'verified=True' para invariante '{evaluation.invariant_id}' "
                    "que exige comprovacao formal (require_authenticity_proof), porem sem token HMAC emitido pelo supervisor."
                )
            if tok is not None and evaluation.verified:
                expected_token = generate_verification_token(
                    task_id=evaluation.task_id,
                    plan_revision=evaluation.plan_revision,
                    invariant_id=evaluation.invariant_id,
                    source=ev.get("source", inv_row["verifier_type"]),
                    run_id=ev.get("run_id"),
                    artifact_sha256=ev.get("artifact_sha256"),
                    exit_code=int(ev.get("exit_code", 0)),
                    nonce=ev.get("nonce"),
                    secret=self.supervisor_secret,
                )
                if not hmac.compare_digest(str(tok).strip(), expected_token):
                    raise InvariantVerificationError(
                        f"Tentativa de registrar avaliacao com token HMAC adulterado ou forjado para invariante '{evaluation.invariant_id}'"
                    )

        ev_json = json.dumps(evaluation.evidence) if evaluation.evidence else None
        with self.transaction():
            self.db.execute(
                """
                INSERT OR REPLACE INTO invariant_evaluations (
                    id, task_id, plan_revision, invariant_id, status, evidence,
                    verified, waiver_authorized, waiver_reason, evaluated_at, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation.id,
                    evaluation.task_id,
                    evaluation.plan_revision,
                    evaluation.invariant_id,
                    evaluation.status,
                    ev_json,
                    1 if evaluation.verified else 0,
                    1 if evaluation.waiver_authorized else 0,
                    evaluation.waiver_reason,
                    evaluation.evaluated_at,
                    evaluation.details,
                ),
            )
        return evaluation

    def get_invariant_evaluations(
        self, task_id: str, plan_revision: Optional[int] = None
    ) -> List[InvariantEvaluation]:
        """Fetch invariant evaluations for a task and optional plan revision."""
        task_id = validate_id(task_id)
        if plan_revision is not None:
            rows = self.db.execute(
                "SELECT * FROM invariant_evaluations WHERE task_id=? AND plan_revision=? ORDER BY evaluated_at ASC, rowid ASC",
                (task_id, plan_revision),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM invariant_evaluations WHERE task_id=? ORDER BY evaluated_at ASC, rowid ASC",
                (task_id,),
            ).fetchall()

        results: List[InvariantEvaluation] = []
        for r in rows:
            ev_data = json.loads(r["evidence"]) if r["evidence"] else None
            results.append(
                InvariantEvaluation(
                    id=r["id"],
                    task_id=r["task_id"],
                    plan_revision=int(r["plan_revision"]),
                    invariant_id=r["invariant_id"],
                    status=r["status"],
                    evidence=ev_data,
                    verified=bool(r["verified"]),
                    waiver_authorized=bool(r["waiver_authorized"]),
                    waiver_reason=r["waiver_reason"],
                    evaluated_at=r["evaluated_at"],
                    details=r["details"],
                )
            )
        return results

    # -------------------------------------------------------------------------
    # Resource Ledger Storage Methods
    # -------------------------------------------------------------------------
    def record_resource_intent(
        self,
        task_id: str,
        plan_revision: int,
        resource_type: str,
        raw_id: str,
        action: str,
        idempotency_key: str,
        run_id: Optional[str] = None,
        pre_hash: Optional[str] = None,
        compensable: bool = True,
        compensation_action: Optional[str] = None,
        requires_authorization: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ResourceRecord:
        """Phase 1: Record intent prior to executing any mutation or external action."""
        with self.transaction():
            return self.resource_ledger.record_intent(
                task_id=task_id,
                plan_revision=plan_revision,
                resource_type=resource_type,
                raw_id=raw_id,
                action=action,
                idempotency_key=idempotency_key,
                run_id=run_id,
                pre_hash=pre_hash,
                compensable=compensable,
                compensation_action=compensation_action,
                requires_authorization=requires_authorization,
                metadata=metadata,
            )

    def confirm_resource_applied(
        self,
        resource_id: str,
        post_hash: Optional[str] = None,
    ) -> ResourceRecord:
        """Phase 2: Confirm operation completed and record physical verification."""
        with self.transaction():
            return self.resource_ledger.confirm_applied(resource_id=resource_id, post_hash=post_hash)

    def reconcile_resources(self, task_id: str) -> List[ResourceRecord]:
        """Detect and reconcile any resources stuck in 'intent_recorded'."""
        with self.transaction():
            return self.resource_ledger.reconcile_resources(task_id=task_id)

    def prepare_resource_compensation(
        self,
        resource_id: str,
        authorized: bool = False,
    ) -> ResourceRecord:
        """Prepare resource compensation, checking authorization for irreversible actions."""
        with self.transaction():
            return self.resource_ledger.prepare_compensation(resource_id=resource_id, authorized=authorized)

    def apply_resource_compensation_step(
        self,
        resource_id: str,
        authorized: bool = False,
    ) -> ResourceRecord:
        """Execute compensation step idempotently and with third-party modification guards."""
        with self.transaction():
            return self.resource_ledger.apply_compensation_step(resource_id=resource_id, authorized=authorized)

    def retain_resource(self, resource_id: str) -> ResourceRecord:
        """Mark a resource as retained for future revisions without compensation."""
        with self.transaction():
            return self.resource_ledger.retain_resource(resource_id=resource_id)

    def list_resources(
        self,
        task_id: str,
        plan_revision: Optional[int] = None,
        status: Optional[str] = None,
    ) -> List[ResourceRecord]:
        """List tracked resources matching task, revision, and status filters."""
        return self.resource_ledger.list_resources(task_id=task_id, plan_revision=plan_revision, status=status)

    def get_resource_events(self, resource_id: str) -> List[ResourceEvent]:
        """Retrieve audit event ledger for a specific resource."""
        return self.resource_ledger.get_events(resource_id=resource_id)

    # -------------------------------------------------------------------------
    # Experience and Continuous Learning Methods
    # -------------------------------------------------------------------------
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
        """Record an auditable experience / lesson learned."""
        return self.experience_store.record_experience(
            project_id=project_id,
            problem_pattern=problem_pattern,
            solution_pattern=solution_pattern,
            category=category,
            outcome=outcome,
            task_id=task_id,
            context_tags=context_tags,
            confidence=confidence,
            author=author,
            visibility=visibility,
            metadata=metadata,
            record_id=record_id,
        )

    def get_experience(
        self,
        record_id: str,
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
    ) -> Optional[ExperienceRecord]:
        """Fetch experience record with privacy gating and scope bounding."""
        return self.experience_store.get_experience(
            record_id,
            caller_role=caller_role,
            caller_authorized_projects=caller_authorized_projects,
        )

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
        """Deterministic experience search with privacy gating and token ranking."""
        return self.experience_store.find_experiences(
            project_id=project_id,
            query_text=query_text,
            tags=tags,
            category=category,
            outcome=outcome,
            author_filter=author_filter,
            caller_role=caller_role,
            caller_authorized_projects=caller_authorized_projects,
            limit=limit,
        )

    # -------------------------------------------------------------------------
    # Insight Engine and Incubator Methods
    # -------------------------------------------------------------------------
    def record_insight(self, insight: Insight) -> Insight:
        """Persist an insight into the incubator."""
        return self.insight_engine.record_insight(insight)

    def get_insight(self, insight_id: str, caller_role: str = "agent") -> Optional[Insight]:
        """Fetch insight by ID with privacy gating."""
        return self.insight_engine.get_insight(insight_id, caller_role=caller_role)

    def list_incubator(
        self,
        project_id: Optional[str] = None,
        status: Optional[str] = InsightStatus.INCUBATING,
        presentation: Optional[str] = None,
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Insight]:
        """List incubating insights respecting privacy and project authorization."""
        return self.insight_engine.list_incubator(
            project_id=project_id,
            status=status,
            presentation=presentation,
            caller_role=caller_role,
            caller_authorized_projects=caller_authorized_projects,
            limit=limit,
        )

    def update_insight_status(
        self,
        insight_id: str,
        new_status: str,
        reason: Optional[str] = None,
        caller_role: str = "human",
    ) -> Insight:
        """Update incubation status of an insight."""
        return self.insight_engine.update_insight_status(
            insight_id=insight_id,
            new_status=new_status,
            reason=reason,
            caller_role=caller_role,
        )

    def get_contextual_insights(
        self,
        project_id: str,
        task_id: Optional[str] = None,
        caller_role: str = "agent",
        caller_authorized_projects: Optional[List[str]] = None,
    ) -> List[Insight]:
        """Fetch incubating insights relevant for contextual suggestion."""
        return self.insight_engine.get_contextual_insights(
            project_id=project_id,
            task_id=task_id,
            caller_role=caller_role,
            caller_authorized_projects=caller_authorized_projects,
        )

    def evaluate_incubator_relevance(
        self,
        project_id: str,
        since_timestamp: Optional[str] = None,
        caller_role: str = "human",
        caller_authorized_projects: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Evaluate which incubating ideas in project have gained relevance since last work."""
        return self.insight_engine.evaluate_incubator_relevance(
            project_id=project_id,
            since_timestamp=since_timestamp,
            caller_role=caller_role,
            caller_authorized_projects=caller_authorized_projects,
        )

    def set_insight_autonomy_level(self, level: str) -> None:
        """Set autonomy level on the insight engine."""
        self.insight_engine.set_autonomy_level(level)

    def promote_insight_to_proposal(
        self,
        insight_id: str,
        task_id: str,
        base_revision: int,
        route_choice: str = "A",
        attach_to_node_id: Optional[str] = None,
        caller_role: str = "human",
    ) -> TrajectoryProposal:
        """Promote an incubating insight into a trajectory proposal with collision safety."""
        return self.insight_engine.promote_to_trajectory_proposal(
            store=self,
            insight_id=insight_id,
            task_id=task_id,
            base_revision=base_revision,
            route_choice=route_choice,
            attach_to_node_id=attach_to_node_id,
            caller_role=caller_role,
        )

    # -------------------------------------------------------------------------
    # Alternative Route Reconstruction Methods
    # -------------------------------------------------------------------------
    def preview_alternative_route(
        self,
        task_id: str,
        base_revision: int,
        failed_node_id: str,
        replacement_nodes: List[Union[PlanNode, Dict[str, Any]]],
        replacement_edges: Optional[List[Union[PlanEdge, Dict[str, Any]]]] = None,
        reconnection_node_id: Optional[str] = None,
    ) -> AlternativeRoutePreview:
        """Dry-run preview of an alternative route without database mutation."""
        return self.reconstructor.preview_alternative_route(
            task_id=task_id,
            base_revision=base_revision,
            failed_node_id=failed_node_id,
            replacement_nodes=replacement_nodes,
            replacement_edges=replacement_edges,
            reconnection_node_id=reconnection_node_id,
        )

    def propose_alternative_route(
        self,
        task_id: str,
        base_revision: int,
        failed_node_id: str,
        replacement_nodes: List[Union[PlanNode, Dict[str, Any]]],
        reason: str,
        replacement_edges: Optional[List[Union[PlanEdge, Dict[str, Any]]]] = None,
        reconnection_node_id: Optional[str] = None,
        diff_summary: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TrajectoryProposal:
        """Formulate and record an unactivated alternative trajectory proposal."""
        return self.reconstructor.propose_alternative_route(
            task_id=task_id,
            base_revision=base_revision,
            failed_node_id=failed_node_id,
            replacement_nodes=replacement_nodes,
            reason=reason,
            replacement_edges=replacement_edges,
            reconnection_node_id=reconnection_node_id,
            diff_summary=diff_summary,
            metadata=metadata,
        )

