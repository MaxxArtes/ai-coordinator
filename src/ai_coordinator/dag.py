from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class DagError(Exception):
    """Base exception for Directed Acyclic Graph validation and operations."""
    pass


class CycleDetectedError(DagError):
    """Raised when a cycle is detected in the dependency graph."""
    pass


class DagValidationError(DagError):
    """Raised when nodes or edges violate structural constraints."""
    pass


class StaleRevisionError(DagError):
    """Raised when an operation attempts to mutate or branch from a stale revision."""
    pass


class ActiveWorkerFencingError(DagError):
    """Raised when trajectory activation is rejected due to active running workers."""
    pass


class ProposalNotApprovedError(DagError):
    """Raised when an unapproved trajectory proposal is submitted for activation."""
    pass


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class PlanNode:
    """A discrete unit of work within a plan revision."""
    node_id: str
    title: str
    description: str = ""
    entry_criteria: List[str] = field(default_factory=list)
    exit_criteria: List[str] = field(default_factory=list)
    deliverables: List[str] = field(default_factory=list)
    status: str = "pending"  # pending, ready, in_progress, completed, blocked, skipped
    assigned_role: str = "worker"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.node_id or not isinstance(self.node_id, str):
            raise DagValidationError("node_id deve ser uma string nao vazia")
        if not self.title or not isinstance(self.title, str):
            raise DagValidationError(f"title do node '{self.node_id}' deve ser uma string nao vazia")
        valid_statuses = {"pending", "ready", "in_progress", "completed", "blocked", "skipped"}
        if self.status not in valid_statuses:
            raise DagValidationError(f"status '{self.status}' invalido para o node '{self.node_id}'")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "title": self.title,
            "description": self.description,
            "entry_criteria": list(self.entry_criteria),
            "exit_criteria": list(self.exit_criteria),
            "deliverables": list(self.deliverables),
            "status": self.status,
            "assigned_role": self.assigned_role,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PlanNode:
        return cls(
            node_id=str(data.get("node_id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            entry_criteria=list(data.get("entry_criteria", [])),
            exit_criteria=list(data.get("exit_criteria", [])),
            deliverables=list(data.get("deliverables", [])),
            status=str(data.get("status", "pending")),
            assigned_role=str(data.get("assigned_role", "worker")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class PlanEdge:
    """A directed dependency between two plan nodes (to_node depends on from_node)."""
    from_node: str
    to_node: str
    edge_type: str = "depends_on"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.from_node or not isinstance(self.from_node, str):
            raise DagValidationError("from_node deve ser uma string nao vazia")
        if not self.to_node or not isinstance(self.to_node, str):
            raise DagValidationError("to_node deve ser uma string nao vazia")
        if self.from_node == self.to_node:
            raise CycleDetectedError(f"Auto-dependencia detectada no no '{self.from_node}'")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_node": self.from_node,
            "to_node": self.to_node,
            "edge_type": self.edge_type,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PlanEdge:
        return cls(
            from_node=str(data.get("from_node", "")),
            to_node=str(data.get("to_node", "")),
            edge_type=str(data.get("edge_type", "depends_on")),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class PlanRevision:
    """An immutable versioned state of the Directed Acyclic Graph."""
    task_id: str
    revision: int
    parent_revision: Optional[int]
    summary: str
    status: str = "active"  # active, superseded, archived
    created_at: str = ""
    nodes: Dict[str, PlanNode] = field(default_factory=dict)
    edges: List[PlanEdge] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        """Validate structural integrity and absence of cycles, returning topological sort."""
        if not self.task_id:
            raise DagValidationError("task_id nao informado")
        if self.revision < 1:
            raise DagValidationError("revision deve ser >= 1")
        for node in self.nodes.values():
            node.validate()
        for edge in self.edges:
            edge.validate()
        return validate_dag(self.nodes, self.edges)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision": self.revision,
            "parent_revision": self.parent_revision,
            "summary": self.summary,
            "status": self.status,
            "created_at": self.created_at,
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
            "edges": [edge.to_dict() for edge in self.edges],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PlanRevision:
        raw_nodes = data.get("nodes", {})
        nodes: Dict[str, PlanNode] = {}
        if isinstance(raw_nodes, dict):
            for k, v in raw_nodes.items():
                node_dict = dict(v)
                if "node_id" not in node_dict:
                    node_dict["node_id"] = k
                nodes[k] = PlanNode.from_dict(node_dict)
        elif isinstance(raw_nodes, list):
            for v in raw_nodes:
                node = PlanNode.from_dict(v)
                nodes[node.node_id] = node

        raw_edges = data.get("edges", [])
        edges = [PlanEdge.from_dict(e) for e in raw_edges]

        return cls(
            task_id=str(data.get("task_id", "")),
            revision=int(data.get("revision", 1)),
            parent_revision=data.get("parent_revision"),
            summary=str(data.get("summary", "")),
            status=str(data.get("status", "active")),
            created_at=str(data.get("created_at", "")),
            nodes=nodes,
            edges=edges,
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class TrajectoryProposal:
    """An unactivated candidate route or revision proposal for a task DAG."""
    id: str
    task_id: str
    base_revision: int
    proposed_revision: Optional[int]
    reason: str
    diff_summary: str
    nodes: Dict[str, PlanNode] = field(default_factory=dict)
    edges: List[PlanEdge] = field(default_factory=list)
    status: str = "proposed"  # proposed, approved, rejected, activated, superseded
    created_at: str = ""
    resolved_at: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> List[str]:
        if not self.id:
            raise DagValidationError("id da proposta obrigatorio")
        if not self.task_id:
            raise DagValidationError("task_id obrigatorio")
        if self.base_revision < 1:
            raise DagValidationError("base_revision deve ser >= 1")
        if not self.reason:
            raise DagValidationError("reason obrigatoria para proposta de trajetoria")
        for node in self.nodes.values():
            node.validate()
        for edge in self.edges:
            edge.validate()
        return validate_dag(self.nodes, self.edges)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "base_revision": self.base_revision,
            "proposed_revision": self.proposed_revision,
            "reason": self.reason,
            "diff_summary": self.diff_summary,
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
            "edges": [edge.to_dict() for edge in self.edges],
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TrajectoryProposal:
        raw_nodes = data.get("nodes", {})
        nodes: Dict[str, PlanNode] = {}
        if isinstance(raw_nodes, dict):
            for k, v in raw_nodes.items():
                node_dict = dict(v)
                if "node_id" not in node_dict:
                    node_dict["node_id"] = k
                nodes[k] = PlanNode.from_dict(node_dict)
        elif isinstance(raw_nodes, list):
            for v in raw_nodes:
                node = PlanNode.from_dict(v)
                nodes[node.node_id] = node

        raw_edges = data.get("edges", [])
        edges = [PlanEdge.from_dict(e) for e in raw_edges]

        return cls(
            id=str(data.get("id", "")),
            task_id=str(data.get("task_id", "")),
            base_revision=int(data.get("base_revision", 1)),
            proposed_revision=data.get("proposed_revision"),
            reason=str(data.get("reason", "")),
            diff_summary=str(data.get("diff_summary", "")),
            nodes=nodes,
            edges=edges,
            status=str(data.get("status", "proposed")),
            created_at=str(data.get("created_at", "")),
            resolved_at=data.get("resolved_at"),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class TrajectoryEvent:
    """An auditable record of a trajectory decision, proposal or revision event."""
    id: Optional[int]
    task_id: str
    revision: Optional[int]
    event_type: str
    severity: str = "info"  # info, warning, error
    details: str = ""
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "revision": self.revision,
            "event_type": self.event_type,
            "severity": self.severity,
            "details": self.details,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# DAG Algorithms: Validation, Cycle Detection and Topological Sort
# ---------------------------------------------------------------------------

def validate_dag(nodes: Dict[str, PlanNode], edges: List[PlanEdge]) -> List[str]:
    """Validate DAG constraints and return a topological sort order.
    
    Guarantees:
    1. No dangling edges (from_node and to_node must both exist in nodes).
    2. No self-loops (from_node != to_node).
    3. Strict cycle detection via Kahn's algorithm and DFS cycle path reporting.
    """
    node_keys = set(nodes.keys())

    # 1. Check dangling edges
    for edge in edges:
        if edge.from_node not in node_keys:
            raise DagValidationError(
                f"Aresta invalida: nó de origem '{edge.from_node}' nao existe na definicao do plano"
            )
        if edge.to_node not in node_keys:
            raise DagValidationError(
                f"Aresta invalida: nó de destino '{edge.to_node}' nao existe na definicao do plano"
            )
        if edge.from_node == edge.to_node:
            raise CycleDetectedError(
                f"Ciclo imediato detectado: nó '{edge.from_node}' depende de si mesmo"
            )

    # 2. Build adjacency list and in-degree table
    adj: Dict[str, List[str]] = {nid: [] for nid in node_keys}
    in_degree: Dict[str, int] = {nid: 0 for nid in node_keys}

    for edge in edges:
        adj[edge.from_node].append(edge.to_node)
        in_degree[edge.to_node] += 1

    # 3. Kahn's Algorithm
    zero_in = [nid for nid, deg in in_degree.items() if deg == 0]
    sorted_nodes: List[str] = []

    while zero_in:
        curr = zero_in.pop(0)
        sorted_nodes.append(curr)
        for neighbor in adj[curr]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                zero_in.append(neighbor)

    if len(sorted_nodes) < len(node_keys):
        # A cycle exists! Extract cycle path for deterministic, actionable error reporting
        cycle_path = _find_cycle_path(adj, node_keys)
        if cycle_path:
            formatted_cycle = " -> ".join(cycle_path)
            raise CycleDetectedError(f"Ciclo detectado na DAG: {formatted_cycle}")
        else:
            raise CycleDetectedError("Ciclo detectado na DAG de tarefas do plano")

    return sorted_nodes


def _find_cycle_path(adj: Dict[str, List[str]], node_keys: Set[str]) -> Optional[List[str]]:
    """Helper to trace an exact directed cycle path using 3-color DFS."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in node_keys}
    parent = {nid: None for nid in node_keys}
    cycle_nodes: List[str] = []

    def dfs(u: str) -> bool:
        color[u] = GREY
        for v in adj.get(u, []):
            if color[v] == GREY:
                # Cycle detected! Reconstruct path
                cycle_nodes.append(v)
                curr = u
                while curr != v and curr is not None:
                    cycle_nodes.append(curr)
                    curr = parent[curr]
                cycle_nodes.append(v)
                cycle_nodes.reverse()
                return True
            elif color[v] == WHITE:
                parent[v] = u
                if dfs(v):
                    return True
        color[u] = BLACK
        return False

    for node in sorted(node_keys):
        if color[node] == WHITE:
            if dfs(node):
                return cycle_nodes
    return None


def get_ready_nodes(nodes: Dict[str, PlanNode], edges: List[PlanEdge]) -> List[str]:
    """Find all nodes that are currently pending and have all prerequisites completed."""
    # Build incoming edges map
    prerequisites: Dict[str, Set[str]] = {nid: set() for nid in nodes}
    for edge in edges:
        prerequisites[edge.to_node].add(edge.from_node)

    ready: List[str] = []
    for nid, node in nodes.items():
        if node.status != "pending":
            continue
        reqs = prerequisites[nid]
        if all(nodes[req].status == "completed" for req in reqs):
            ready.append(nid)
    return sorted(ready)
