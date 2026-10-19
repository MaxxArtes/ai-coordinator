"""Optional MCP server exposing task state and explicitly requested workers."""
from __future__ import annotations

import sys
from typing import Any

from . import __version__
from .api import Coordinator


def create_server():
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError(
            "O servidor MCP requer a dependencia opcional; instale ai-coordinator[mcp]"
        ) from exc

    server = MCPServer(
        "ai-coordinator",
        version=__version__,
        instructions=(
            "Estado de tarefas local e compartilhado entre agentes. Leia o projeto e a "
            "tarefa pertinente antes de retomar. Delegar chama um modelo externo e pode "
            "consumir quota; respostas de workers sao propostas para revisao."
        ),
    )

    @server.tool()
    def bootstrap_project(project_path: str) -> dict[str, Any]:
        """Return shared protocol and active task summaries for a project directory."""
        with Coordinator() as coordinator:
            return coordinator.bootstrap(project_path)

    @server.tool()
    def list_active_tasks(project_path: str) -> list[dict[str, Any]]:
        """List active coordinator tasks for a project directory."""
        with Coordinator() as coordinator:
            return coordinator.active_tasks(project_path)

    @server.tool()
    def create_task(
        project_path: str,
        goal: str,
        ai_memory_workstream_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a task and optionally link its ai-memory workstream UUID."""
        with Coordinator() as coordinator:
            return coordinator.new_task(project_path, goal, ai_memory_workstream_id)

    @server.tool()
    def resume_task(task_id: str) -> dict[str, Any]:
        """Read one task checkpoint and its recent worker runs."""
        with Coordinator() as coordinator:
            return coordinator.resume(task_id)

    @server.tool()
    def save_checkpoint(
        task_id: str,
        revision: int,
        patch: dict[str, Any],
        status: str | None = None,
    ) -> dict[str, Any]:
        """Save a bounded checkpoint; revision must match the latest read."""
        if status is not None and status not in {"active", "complete", "blocked"}:
            raise ValueError("Status deve ser active, complete ou blocked")
        with Coordinator() as coordinator:
            return coordinator.checkpoint(task_id, revision, patch, status)

    @server.tool()
    def list_routes() -> dict[str, Any]:
        """List configured worker routes without calling external models."""
        with Coordinator() as coordinator:
            return coordinator.routes()

    @server.tool()
    def plan_worker(category: str, refresh: bool = False) -> dict[str, Any]:
        """Inspect one worker route. Refresh checks local client catalogs only."""
        with Coordinator() as coordinator:
            return coordinator.plan(category, refresh)

    @server.tool()
    def delegate_to_worker(
        task_id: str,
        category: str,
        input_text: str,
        max_attempts: int = 1,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        """Call configured external model workers; this may consume quota or balance."""
        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts deve ser 1 ou 2")
        if response_format is not None and response_format not in {"text", "json", "python", "code"}:
            raise ValueError("Formato deve ser text, json, python ou code")
        with Coordinator() as coordinator:
            return coordinator.delegate(
                task_id, category, input_text, max_attempts=max_attempts,
                response_format=response_format,
            )

    return server


def main() -> None:
    try:
        create_server().run()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
