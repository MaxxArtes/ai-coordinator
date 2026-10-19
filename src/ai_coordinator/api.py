"""Stable Python API for the shared AI coordinator."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from . import runtime
from .cli import Store, bootstrap as _bootstrap, export_context as _export_context, run_worker as _run_worker
from .data import initialize_data_dir


class Coordinator:
    """Manage shared tasks, checkpoints, routes, and bounded worker calls.

    The default data directory is per-user. Pass ``root`` to use an explicit
    location, or set ``AI_COORDINATOR_HOME`` before constructing the instance.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = initialize_data_dir(root)
        self.config = json.loads((self.root / "routes.json").read_text(encoding="utf-8-sig"))
        self.store = Store(self.root)

    def close(self) -> None:
        self.store.db.close()

    def __enter__(self) -> "Coordinator":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def new_task(
        self,
        project: str | Path,
        goal: str,
        ai_memory_workstream_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store.new(str(project), goal, ai_memory_workstream_id)

    def active_tasks(self, project: str | Path) -> list[dict[str, Any]]:
        return self.store.context(str(project))

    def bootstrap(self, project: str | Path) -> dict[str, Any]:
        return _bootstrap(self.store, str(project))

    def resume(self, task_id: str) -> dict[str, Any]:
        return {"task": self.store.task(task_id), "recent_runs": self.store.runs(task_id)}

    def checkpoint(
        self,
        task_id: str,
        revision: int,
        patch: dict[str, Any],
        status: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in {"active", "complete", "blocked"}:
            raise ValueError("Status deve ser active, complete ou blocked")
        return self.store.checkpoint(task_id, revision, patch, status)

    def routes(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.config["routes"]))

    def plan(self, category: str, refresh: bool = False) -> dict[str, Any]:
        if category not in self.config["routes"]:
            raise ValueError("Categoria desconhecida; consulte routes")
        return runtime.route_plan(self.store, self.config, category, refresh)

    def delegate(
        self,
        task_id: str,
        category: str,
        input_text: str,
        *,
        max_attempts: int = 1,
        response_format: str | None = None,
        exclude_clients: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a bounded worker call; its output is a proposal for review."""
        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts deve ser 1 ou 2")
        if response_format is not None and response_format not in {"text", "json", "python", "code"}:
            raise ValueError("Formato deve ser text, json, python ou code")
        config = json.loads(json.dumps(self.config))
        if category not in config["routes"]:
            raise ValueError("Categoria desconhecida; consulte routes")
        config["disabled_worker_clients"] = list(set(
            config.get("disabled_worker_clients", []) + (exclude_clients or [])
        ))
        if response_format:
            for candidate in config["routes"][category]["candidates"]:
                candidate["response_format"] = response_format
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", prefix="worker-input-",
            dir=self.root, delete=False,
        ) as temporary:
            temporary.write(input_text)
            input_path = Path(temporary.name)
        try:
            return _run_worker(
                self.store, config, task_id, category, str(input_path), max_attempts
            )
        finally:
            input_path.unlink(missing_ok=True)

    def export_context(self, task_id: str, destination: str | Path) -> dict[str, Any]:
        return _export_context(self.store, self.config, task_id, str(destination))


def open_coordinator(root: str | Path | None = None) -> Coordinator:
    """Open a coordinator instance, optionally at an explicit data directory."""
    return Coordinator(root)
