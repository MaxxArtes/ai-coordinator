"""Deterministic alternative route reconstruction and adaptive DAG reconnection.

Computes safe replacement routes for failed, deviated, or blocked nodes,
preserves upstream and downstream dependencies, identifies convergence/reconnection
points, and produces unactivated TrajectoryProposals without in-place DAG mutation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from .dag import (
    DagError,
    DagValidationError,
    CycleDetectedError,
    PlanEdge,
    PlanNode,
    PlanRevision,
    TrajectoryProposal,
    validate_dag,
)
from .experience import ExperienceStore
from .invariants import InvariantStatus
from .resource_ledger import ResourceState

now = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ReconstructionError(DagError):
    """Base exception for alternative route reconstruction failures."""


class ReconnectionPointNotFoundError(ReconstructionError):
    """Raised when the specified or calculated reconnection point is invalid."""


@dataclass
class AlternativeRoutePreview:
    """Dry-run projection of an alternative route splicing."""
    task_id: str
    base_revision: int
    failed_node_id: str
    reconnection_points: List[str]
    upstream_nodes: List[str]
    replacement_node_ids: List[str]
    unaffected_node_ids: List[str]
    total_nodes_count: int
    is_valid_dag: bool
    topological_order: List[str]
    diff_summary: str
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def calculate_reconnection_points(
    base_revision: PlanRevision,
    failed_node_id: str,
) -> List[str]:
    """Find immediate downstream successors of the failed node in the existing DAG.
    
    If the failed node has successors, those nodes represent the canonical
    reconnection points where alternative routes must splice back into the plan.
    """
    if failed_node_id not in base_revision.nodes:
        raise DagValidationError(
            f"No alvo '{failed_node_id}' nao existe na revisao {base_revision.revision}"
        )

    successors = [
        edge.to_node
        for edge in base_revision.edges
        if edge.from_node == failed_node_id
    ]
    return sorted(set(successors))


def calculate_upstream_prerequisites(
    base_revision: PlanRevision,
    failed_node_id: str,
) -> List[str]:
    """Find immediate upstream predecessors feeding into the failed node."""
    if failed_node_id not in base_revision.nodes:
        raise DagValidationError(
            f"No alvo '{failed_node_id}' nao existe na revisao {base_revision.revision}"
        )

    predecessors = [
        edge.from_node
        for edge in base_revision.edges
        if edge.to_node == failed_node_id
    ]
    return sorted(set(predecessors))


def splice_alternative_route(
    base_revision: PlanRevision,
    failed_node_id: str,
    replacement_nodes: List[PlanNode],
    replacement_edges: Optional[List[PlanEdge]] = None,
    reconnection_node_id: Optional[str] = None,
) -> Tuple[Dict[str, PlanNode], List[PlanEdge], str, List[str]]:
    """Splice an alternative route into a DAG, preserving dependency invariants.
    
    Guarantees:
    1. Upstream predecessors of failed_node_id are safely rewired to replacement entries.
    2. Replacement exits are rewired to the reconnection point(s) or downstream successors.
    3. All unaffected nodes and their dependencies are preserved intact.
    4. The spliced DAG is strictly checked for cycles and dangling edges via validate_dag.
    5. Returns (new_nodes, new_edges, diff_summary, reconnection_points).
    """
    if failed_node_id not in base_revision.nodes:
        raise DagValidationError(
            f"No alvo '{failed_node_id}' nao encontrado na revisao {base_revision.revision}"
        )

    if not replacement_nodes:
        raise DagValidationError("Pelo menos um no alternativo deve ser fornecido para reconstrucao")

    # 1. Identify upstream predecessors and downstream successors
    upstream = calculate_upstream_prerequisites(base_revision, failed_node_id)
    natural_reconnections = calculate_reconnection_points(base_revision, failed_node_id)

    if reconnection_node_id:
        if reconnection_node_id not in base_revision.nodes:
            raise ReconnectionPointNotFoundError(
                f"Ponto de reconexao especificado '{reconnection_node_id}' nao existe no plano"
            )
        if reconnection_node_id == failed_node_id:
            raise ReconnectionPointNotFoundError(
                f"O ponto de reconexao nao pode ser o proprio no com falha '{failed_node_id}'"
            )
        reconnection_points = [reconnection_node_id]
    else:
        reconnection_points = natural_reconnections

    # 2. Build the new nodes dictionary
    new_nodes: Dict[str, PlanNode] = {}
    for nid, node in base_revision.nodes.items():
        if nid != failed_node_id:
            # Preserve existing node
            new_nodes[nid] = PlanNode.from_dict(node.to_dict())

    # Add replacement nodes
    rep_node_ids = set()
    for rep_node in replacement_nodes:
        rep_node.validate()
        if rep_node.node_id in new_nodes:
            raise DagValidationError(
                f"Colisao de identificador: no de substituicao '{rep_node.node_id}' ja existe no plano"
            )
        new_nodes[rep_node.node_id] = rep_node
        rep_node_ids.add(rep_node.node_id)

    # 3. Determine entry and exit nodes of the replacement subgraph
    rep_edges: List[PlanEdge] = replacement_edges or []
    # If no replacement edges provided and multiple replacement nodes, connect them in a linear sequence
    if not rep_edges and len(replacement_nodes) > 1:
        rep_edges = [
            PlanEdge(from_node=replacement_nodes[i].node_id, to_node=replacement_nodes[i + 1].node_id)
            for i in range(len(replacement_nodes) - 1)
        ]

    rep_in_degrees: Dict[str, int] = {n.node_id: 0 for n in replacement_nodes}
    rep_out_degrees: Dict[str, int] = {n.node_id: 0 for n in replacement_nodes}
    for e in rep_edges:
        if e.from_node in rep_out_degrees:
            rep_out_degrees[e.from_node] += 1
        if e.to_node in rep_in_degrees:
            rep_in_degrees[e.to_node] += 1

    entry_nodes = [nid for nid, deg in rep_in_degrees.items() if deg == 0]
    exit_nodes = [nid for nid, deg in rep_out_degrees.items() if deg == 0]

    # 4. Construct new edges list
    new_edges: List[PlanEdge] = []

    # Preserve all existing edges that do NOT touch failed_node_id
    for edge in base_revision.edges:
        if edge.from_node == failed_node_id or edge.to_node == failed_node_id:
            continue
        new_edges.append(PlanEdge.from_dict(edge.to_dict()))

    # Add internal replacement edges
    for e in rep_edges:
        e.validate()
        new_edges.append(PlanEdge.from_dict(e.to_dict()))

    # Wire upstream predecessors to entry nodes of replacement
    for up_node in upstream:
        for entry_node in entry_nodes:
            new_edges.append(PlanEdge(from_node=up_node, to_node=entry_node))

    # Wire exit nodes of replacement to reconnection points
    for exit_node in exit_nodes:
        for rec_node in reconnection_points:
            new_edges.append(PlanEdge(from_node=exit_node, to_node=rec_node))

    # 5. Strict DAG Validation (cycle detection and topological order check)
    validate_dag(new_nodes, new_edges)

    # 6. Build diff summary
    rep_names = ", ".join(f"'{n.node_id}'" for n in replacement_nodes)
    rec_names = ", ".join(f"'{n}'" for n in reconnection_points) if reconnection_points else "(fim da DAG)"
    diff_summary = (
        f"Reconstrucao de rota: substituido '{failed_node_id}' por {len(replacement_nodes)} no(s) [{rep_names}]. "
        f"Dependencias a montante [{', '.join(upstream) or 'nenhuma'}] religadas as entradas [{', '.join(entry_nodes)}]. "
        f"Ponto de reconexao estabelecido em [{rec_names}]."
    )

    return new_nodes, new_edges, diff_summary, reconnection_points


class TrajectoryReconstructor:
    """Safe trajectory reconstructor and alternative route planner."""

    def __init__(self, store: Any):
        self.store = store
        self.experience_store = getattr(store, "experience_store", None) or ExperienceStore(store.db)

    def preview_alternative_route(
        self,
        task_id: str,
        base_revision: int,
        failed_node_id: str,
        replacement_nodes: List[Union[PlanNode, Dict[str, Any]]],
        replacement_edges: Optional[List[Union[PlanEdge, Dict[str, Any]]]] = None,
        reconnection_node_id: Optional[str] = None,
    ) -> AlternativeRoutePreview:
        """Calculate dry-run preview of an alternative route without database mutation."""
        base_rev = self.store.get_plan_revision(task_id, revision=base_revision)
        if not base_rev:
            raise DagValidationError(
                f"Revisao base {base_revision} nao encontrada para a tarefa '{task_id}'"
            )

        # Normalize nodes and edges
        rep_nodes: List[PlanNode] = [
            n if isinstance(n, PlanNode) else PlanNode.from_dict(n)
            for n in replacement_nodes
        ]
        rep_edges: Optional[List[PlanEdge]] = None
        if replacement_edges is not None:
            rep_edges = [
                e if isinstance(e, PlanEdge) else PlanEdge.from_dict(e)
                for e in replacement_edges
            ]

        # Splice and validate
        new_nodes, new_edges, diff_summary, reconnection_points = splice_alternative_route(
            base_revision=base_rev,
            failed_node_id=failed_node_id,
            replacement_nodes=rep_nodes,
            replacement_edges=rep_edges,
            reconnection_node_id=reconnection_node_id,
        )

        topological_order = validate_dag(new_nodes, new_edges)
        upstream = calculate_upstream_prerequisites(base_rev, failed_node_id)
        rep_ids = [n.node_id for n in rep_nodes]
        unaffected = [nid for nid in new_nodes if nid not in rep_ids]

        warnings: List[str] = []

        # Check resource ledger for uncertain resources
        try:
            uncertain_count = self.store.db.execute(
                "SELECT COUNT(*) FROM resource_ledger WHERE task_id=? AND status=?",
                (task_id, ResourceState.UNCERTAIN),
            ).fetchone()[0]
            if uncertain_count > 0:
                warnings.append(
                    f"Atencao: existem {uncertain_count} recurso(s) em estado incerto ('uncertain'). "
                    "A ativacao da proposta sera bloqueada ate que sejam reconciliados."
                )
        except Exception:
            pass

        # Check invariants
        try:
            req_invariants = self.store.db.execute(
                "SELECT id FROM invariants WHERE task_id=? AND required=1",
                (task_id,),
            ).fetchall()
            for inv in req_invariants:
                inv_id = inv["id"]
                eval_row = self.store.db.execute(
                    "SELECT status, verified FROM invariant_evaluations WHERE task_id=? AND plan_revision=? AND invariant_id=? ORDER BY evaluated_at DESC LIMIT 1",
                    (task_id, base_revision, inv_id),
                ).fetchone()
                if not eval_row or eval_row["status"] not in (InvariantStatus.PASS, InvariantStatus.NOT_APPLICABLE) or not eval_row["verified"]:
                    warnings.append(
                        f"Atencao: invariante obrigatorio '{inv_id}' nao possui verificacao satisfatoria na revisao {base_revision}."
                    )
        except Exception:
            pass

        return AlternativeRoutePreview(
            task_id=task_id,
            base_revision=base_revision,
            failed_node_id=failed_node_id,
            reconnection_points=reconnection_points,
            upstream_nodes=upstream,
            replacement_node_ids=rep_ids,
            unaffected_node_ids=unaffected,
            total_nodes_count=len(new_nodes),
            is_valid_dag=True,
            topological_order=topological_order,
            diff_summary=diff_summary,
            warnings=warnings,
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
        """Create an unactivated candidate trajectory proposal with alternative route.
        
        Guarantees:
        1. Does not alter active revision or mutate executing worker state.
        2. Strictly validates DAG aciclicity and dependency continuity.
        3. Records proposal in SQLite with status='proposed'.
        4. Requires explicit human review and approval prior to activation.
        """
        base_rev = self.store.get_plan_revision(task_id, revision=base_revision)
        if not base_rev:
            raise DagValidationError(
                f"Revisao base {base_revision} nao encontrada para a tarefa '{task_id}'"
            )

        rep_nodes: List[PlanNode] = [
            n if isinstance(n, PlanNode) else PlanNode.from_dict(n)
            for n in replacement_nodes
        ]
        rep_edges: Optional[List[PlanEdge]] = None
        if replacement_edges is not None:
            rep_edges = [
                e if isinstance(e, PlanEdge) else PlanEdge.from_dict(e)
                for e in replacement_edges
            ]

        new_nodes, new_edges, auto_diff, recon_points = splice_alternative_route(
            base_revision=base_rev,
            failed_node_id=failed_node_id,
            replacement_nodes=rep_nodes,
            replacement_edges=rep_edges,
            reconnection_node_id=reconnection_node_id,
        )

        final_diff = diff_summary or auto_diff
        meta = dict(metadata or {})
        meta["failed_node_id"] = failed_node_id
        meta["reconnection_points"] = recon_points
        meta["replacement_nodes"] = [n.node_id for n in rep_nodes]

        proposal = self.store.propose_trajectory(
            task_id=task_id,
            base_revision=base_revision,
            proposed_nodes=new_nodes,
            proposed_edges=new_edges,
            reason=reason,
            diff_summary=final_diff,
            metadata=meta,
        )

        return proposal
