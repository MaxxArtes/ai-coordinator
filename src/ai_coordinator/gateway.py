"""Read-only, single-project Gateway G0 for local MCP stdio."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import __version__
from .api import mission_reader


GATEWAY_CONTRACTS = {
    "capabilities": [],
    "version": [],
    "contracts": [],
    "missions_list": [],
    "missions_get": ["mission_id"],
    "tasks_get": ["task_id"],
    "dag_get": ["mission_id"],
    "dag_ready": ["mission_id"],
}


def create_gateway_server(project: str | Path, root: str | Path | None = None):
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError("O Gateway G0 requer a dependencia ai-coordinator[mcp]") from exc

    reader = mission_reader(project, root)
    server = MCPServer(
        "ai-coordinator-g0", version=__version__,
        instructions="Consultas somente leitura no projeto vinculado pelo operador.",
    )

    @server.tool()
    def capabilities() -> dict[str, Any]:
        """Discover the operations enabled on this read-only gateway."""
        return {"phase": "G0", "read_only": True, "operations": list(GATEWAY_CONTRACTS)}

    @server.tool()
    def version() -> dict[str, str]:
        """Return the Coordinator version implementing the Gateway."""
        return {"version": __version__}

    @server.tool()
    def contracts() -> dict[str, list[str]]:
        """Return each operation and its required arguments."""
        return {name: list(fields) for name, fields in GATEWAY_CONTRACTS.items()}

    @server.tool()
    def missions_list() -> list[dict[str, str]]:
        """List missions in the bound project."""
        return reader.list()

    @server.tool()
    def missions_get(mission_id: str) -> dict[str, Any]:
        """Read one mission and its tasks in the bound project."""
        return reader.get(mission_id)

    @server.tool()
    def tasks_get(task_id: str) -> dict[str, Any]:
        """Read a task summary in the bound project."""
        return reader.task(task_id)

    @server.tool()
    def dag_get(mission_id: str) -> dict[str, Any] | None:
        """Return the current DAG revision for one mission in the bound project."""
        return reader.get(mission_id)["dag"]

    @server.tool()
    def dag_ready(mission_id: str) -> list[str]:
        """List ready nodes as advice without dispatching them."""
        dag = reader.get(mission_id)["dag"]
        return dag["ready_node_ids"] if dag is not None else []

    return server
