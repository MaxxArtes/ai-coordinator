from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_coordinator import (
    Store,
    RunState,
    PlanNode,
    PlanEdge,
    PlanRevision,
    TrajectoryProposal,
    TrajectoryEvent,
    DagError,
    CycleDetectedError,
    DagValidationError,
    StaleRevisionError,
    ActiveWorkerFencingError,
    validate_dag,
    get_ready_nodes,
)
import ai_coordinator.cli as cli


class DagVersioningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "data"
        self.project = Path(self.temp_dir.name) / "project"
        self.project.mkdir()
        self.store = Store(self.root)
        self.task = self.store.new(self.project, "Tarefa com DAG Versionada")
        self.task_id = self.task["id"]

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Estrutura de dados e algoritmos da DAG (Validação, Ciclos e Topologia)
    # -------------------------------------------------------------------------
    def test_dag_validation_and_topological_sort(self):
        nodes = {
            "a": PlanNode("a", "Task A", deliverables=["a.txt"]),
            "b": PlanNode("b", "Task B", entry_criteria=["a_ready"], deliverables=["b.txt"]),
            "c": PlanNode("c", "Task C", deliverables=["c.txt"]),
            "d": PlanNode("d", "Task D", exit_criteria=["d_tested"]),
        }
        edges = [
            PlanEdge("a", "b"),
            PlanEdge("b", "d"),
            PlanEdge("c", "d"),
        ]

        order = validate_dag(nodes, edges)
        self.assertEqual(len(order), 4)
        self.assertLess(order.index("a"), order.index("b"))
        self.assertLess(order.index("b"), order.index("d"))
        self.assertLess(order.index("c"), order.index("d"))

    def test_cycle_detection_direct_and_multistep(self):
        # Self loop
        nodes_self = {"x": PlanNode("x", "Task X")}
        edges_self = [PlanEdge("x", "x")]
        with self.assertRaises(CycleDetectedError) as ctx:
            validate_dag(nodes_self, edges_self)
        self.assertIn("Ciclo imediato detectado", str(ctx.exception))

        # 3-step cycle: A -> B -> C -> A
        nodes_cycle = {
            "a": PlanNode("a", "Node A"),
            "b": PlanNode("b", "Node B"),
            "c": PlanNode("c", "Node C"),
        }
        edges_cycle = [
            PlanEdge("a", "b"),
            PlanEdge("b", "c"),
            PlanEdge("c", "a"),
        ]
        with self.assertRaises(CycleDetectedError) as ctx:
            validate_dag(nodes_cycle, edges_cycle)
        self.assertIn("Ciclo detectado na DAG", str(ctx.exception))
        # Exact cycle path must be reported
        self.assertIn("a -> b -> c -> a", str(ctx.exception))

    def test_dangling_edges_rejected(self):
        nodes = {"a": PlanNode("a", "Task A")}
        # to_node "b" does not exist
        edges = [PlanEdge("a", "b")]
        with self.assertRaises(DagValidationError) as ctx:
            validate_dag(nodes, edges)
        self.assertIn("nó de destino 'b' nao existe", str(ctx.exception))

        # from_node "ghost" does not exist
        edges_ghost = [PlanEdge("ghost", "a")]
        with self.assertRaises(DagValidationError) as ctx:
            validate_dag(nodes, edges_ghost)
        self.assertIn("nó de origem 'ghost' nao existe", str(ctx.exception))

    def test_get_ready_nodes_based_on_dependencies(self):
        nodes = {
            "step1": PlanNode("step1", "Preparar ambiente", status="completed"),
            "step2": PlanNode("step2", "Construir modulo", status="pending"),
            "step3": PlanNode("step3", "Gerar documentacao", status="pending"),
            "step4": PlanNode("step4", "Testes de integracao", status="pending"),
        }
        edges = [
            PlanEdge("step1", "step2"),
            PlanEdge("step1", "step3"),
            PlanEdge("step2", "step4"),
            PlanEdge("step3", "step4"),
        ]

        ready = get_ready_nodes(nodes, edges)
        # step2 and step3 both depend only on step1 which is completed -> both ready
        self.assertEqual(ready, ["step2", "step3"])

        # Once step2 is completed, step4 is still waiting on step3
        nodes["step2"].status = "completed"
        ready_after = get_ready_nodes(nodes, edges)
        self.assertEqual(ready_after, ["step3"])

        # When step3 also completes, step4 becomes ready
        nodes["step3"].status = "completed"
        ready_final = get_ready_nodes(nodes, edges)
        self.assertEqual(ready_final, ["step4"])

    # -------------------------------------------------------------------------
    # 2. Persistência de Revisões Imutáveis no SQLite
    # -------------------------------------------------------------------------
    def test_create_and_retrieve_plan_revision(self):
        nodes = [
            {"node_id": "setup", "title": "Setup Inicial", "deliverables": ["config.json"]},
            {"node_id": "impl", "title": "Implementacao", "deliverables": ["code.py"]},
        ]
        edges = [{"from_node": "setup", "to_node": "impl"}]

        rev1 = self.store.create_plan_revision(
            self.task_id,
            nodes=nodes,
            edges=edges,
            summary="Revisao 1 inicial do plano",
        )
        self.assertEqual(rev1.revision, 1)
        self.assertIsNone(rev1.parent_revision)
        self.assertEqual(rev1.status, "active")
        self.assertEqual(len(rev1.nodes), 2)
        self.assertEqual(len(rev1.edges), 1)

        # Retrieve revision 1 explicitly and as latest
        retrieved = self.store.get_plan_revision(self.task_id, revision=1)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.revision, 1)
        self.assertEqual(retrieved.nodes["setup"].deliverables, ["config.json"])

        latest = self.store.get_plan_revision(self.task_id)
        self.assertEqual(latest.revision, 1)

        # Verify audit event
        events = self.store.get_trajectory_events(self.task_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "plan_revision_created")

    def test_immutable_revision_history_and_superseding(self):
        # Create revision 1
        self.store.create_plan_revision(
            self.task_id,
            nodes=[{"node_id": "n1", "title": "Node 1"}],
            edges=[],
            summary="Plano inicial v1",
        )

        # Create revision 2 branching from revision 1
        rev2 = self.store.create_plan_revision(
            self.task_id,
            nodes=[
                {"node_id": "n1", "title": "Node 1 Modificado"},
                {"node_id": "n2", "title": "Node 2 Novo"},
            ],
            edges=[{"from_node": "n1", "to_node": "n2"}],
            summary="Plano expandido v2",
            parent_revision=1,
        )
        self.assertEqual(rev2.revision, 2)
        self.assertEqual(rev2.parent_revision, 1)
        self.assertEqual(rev2.status, "active")

        # History must contain both revisions
        history = self.store.list_plan_revisions(self.task_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["revision"], 1)
        self.assertEqual(history[0]["status"], "superseded")
        self.assertEqual(history[1]["revision"], 2)
        self.assertEqual(history[1]["status"], "active")

        # Prior revision 1 remains completely intact and retrievable
        old_rev1 = self.store.get_plan_revision(self.task_id, revision=1)
        self.assertEqual(len(old_rev1.nodes), 1)
        self.assertEqual(old_rev1.nodes["n1"].title, "Node 1")

    def test_stale_revision_rejected_by_fencing(self):
        # Create rev 1 and rev 2
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "n1", "title": "N1"}], edges=[], summary="R1")
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "n1", "title": "N1"}], edges=[], summary="R2", parent_revision=1)

        # Attempt to branch from stale revision 1 must be rejected
        with self.assertRaises(StaleRevisionError) as ctx:
            self.store.create_plan_revision(
                self.task_id,
                nodes=[{"node_id": "n1", "title": "N1"}],
                edges=[],
                summary="Tentativa em revisao velha",
                parent_revision=1,
            )
        self.assertIn("Revisao base obsoleta", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 3. Propostas de Trajetória Desacopladas (Trajectory Proposals)
    # -------------------------------------------------------------------------
    def test_trajectory_proposal_lifecycle_decoupled_from_activation(self):
        # Setup base revision 1
        self.store.create_plan_revision(
            self.task_id,
            nodes=[
                {"node_id": "task_a", "title": "Tarefa A"},
                {"node_id": "task_b", "title": "Tarefa B"},
            ],
            edges=[{"from_node": "task_a", "to_node": "task_b"}],
            summary="Revisao 1",
        )

        # Propose an alternative route (split Task B into B1 and B2)
        proposed_nodes = [
            {"node_id": "task_a", "title": "Tarefa A"},
            {"node_id": "task_b1", "title": "Tarefa B1 Especializada"},
            {"node_id": "task_b2", "title": "Tarefa B2 Especializada"},
        ]
        proposed_edges = [
            {"from_node": "task_a", "to_node": "task_b1"},
            {"from_node": "task_a", "to_node": "task_b2"},
        ]

        prop = self.store.propose_trajectory(
            task_id=self.task_id,
            base_revision=1,
            proposed_nodes=proposed_nodes,
            proposed_edges=proposed_edges,
            reason="Dividir tarefa B para paralelismo sem risco",
            diff_summary="Substitui task_b por task_b1 e task_b2",
        )
        self.assertTrue(prop.id)
        self.assertEqual(prop.status, "proposed")
        self.assertEqual(prop.base_revision, 1)
        self.assertIsNone(prop.proposed_revision)

        # CRITICAL TEST: Active plan revision remains completely UNTOUCHED
        active_rev = self.store.get_plan_revision(self.task_id)
        self.assertEqual(active_rev.revision, 1)
        self.assertIn("task_b", active_rev.nodes)
        self.assertNotIn("task_b1", active_rev.nodes)

        # Review proposal: approve
        approved_prop = self.store.resolve_trajectory_proposal(prop.id, "approved", notes="Revisao aprovada pelo co-piloto")
        self.assertEqual(approved_prop.status, "approved")
        self.assertIsNotNone(approved_prop.resolved_at)

        # Active plan STILL remains at revision 1 until explicit activation!
        self.assertEqual(self.store.get_plan_revision(self.task_id).revision, 1)

    def test_proposal_cycle_rejection(self):
        self.store.create_plan_revision(
            self.task_id,
            nodes=[{"node_id": "a", "title": "A"}],
            edges=[],
            summary="R1",
        )

        with self.assertRaises(CycleDetectedError):
            self.store.propose_trajectory(
                self.task_id,
                base_revision=1,
                proposed_nodes=[{"node_id": "x", "title": "X"}, {"node_id": "y", "title": "Y"}],
                proposed_edges=[{"from_node": "x", "to_node": "y"}, {"from_node": "y", "to_node": "x"}],
                reason="Proposta ciclica invalida",
            )

    # -------------------------------------------------------------------------
    # 4. Ativação Segura e Barreira contra Workers Ativos (Process Safety Fencing)
    # -------------------------------------------------------------------------
    def test_activation_blocked_if_workers_active(self):
        self.store.create_plan_revision(
            self.task_id,
            nodes=[{"node_id": "a", "title": "A"}],
            edges=[],
            summary="R1",
        )
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Adicionar B",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # Start a worker run in task
        run_id, _, _ = self.store.reserve(self.task_id, "code", "worker prompt", 5)
        self.store.transition_run(run_id, to_status=RunState.STARTING, pid=9999, process_create_time=123)
        self.store.transition_run(run_id, to_status=RunState.RUNNING)

        # Attempting activation with active worker must be strictly BLOCKED!
        with self.assertRaises(ActiveWorkerFencingError) as ctx:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("Ativacao bloqueada por seguranca de processos", str(ctx.exception))
        self.assertIn(run_id, str(ctx.exception))

        # Active revision must still be 1
        self.assertEqual(self.store.get_plan_revision(self.task_id).revision, 1)

        # Conclude worker safely
        self.store.confirm_exit(run_id, exit_code=0)

        # Now activation succeeds!
        new_rev = self.store.activate_trajectory_proposal(prop.id)
        self.assertEqual(new_rev.revision, 2)
        self.assertEqual(new_rev.parent_revision, 1)
        self.assertEqual(len(new_rev.nodes), 2)
        self.assertIn("b", new_rev.nodes)

        # Proposal status marked as activated
        activated_prop = self.store.get_trajectory_proposal(prop.id)
        self.assertEqual(activated_prop.status, "activated")
        self.assertEqual(activated_prop.proposed_revision, 2)

    def test_activation_with_stale_base_revision_rejected(self):
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="R1")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Proposta baseada em R1",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # Another revision (R2) is committed in the meantime
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A2"}], edges=[], summary="R2", parent_revision=1)

        # Attempting to activate proposal formulated over R1 must fail fencing
        with self.assertRaises(StaleRevisionError) as ctx:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("A proposta foi formulada sobre revisao 1, mas a ativa e 2", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 5. Atualização de Nós e Auditoria de Eventos
    # -------------------------------------------------------------------------
    def test_node_status_update_and_audit_ledger(self):
        rev = self.store.create_plan_revision(
            self.task_id,
            nodes=[{"node_id": "n1", "title": "Modulo Core"}],
            edges=[],
            summary="R1",
        )
        self.assertEqual(rev.nodes["n1"].status, "pending")

        # Update node status to in_progress
        updated = self.store.update_node_status(self.task_id, revision=1, node_id="n1", status="in_progress")
        self.assertEqual(updated.status, "in_progress")

        # Complete node with deliverables
        completed = self.store.update_node_status(
            self.task_id,
            revision=1,
            node_id="n1",
            status="completed",
            deliverables=["core.py", "test_core.py"],
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.deliverables, ["core.py", "test_core.py"])

        # Audit events must record both updates
        events = self.store.get_trajectory_events(self.task_id, revision=1)
        event_types = [e.event_type for e in events]
        self.assertIn("plan_revision_created", event_types)
        self.assertEqual(event_types.count("node_status_updated"), 2)

    # -------------------------------------------------------------------------
    # 6. CLI Integration: dag-revision e dag-proposal
    # -------------------------------------------------------------------------
    def test_cli_dag_revision_and_proposal(self):
        plan_file = self.root / "plan_input.json"
        plan_file.write_text(json.dumps({
            "summary": "Plano inicial CLI",
            "nodes": [
                {"node_id": "step_cli", "title": "Passo CLI"}
            ],
            "edges": []
        }), encoding="utf-8")

        # CLI: dag-revision --task ID --set plan_input.json
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "dag-revision", "--task", self.task_id, "--set", str(plan_file)
        ]):
            cli.main()
            mock_emit.assert_called()
            self.assertEqual(mock_emit.call_args[0][0]["revision"], 1)

        # CLI: dag-revision --task ID --history
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "dag-revision", "--task", self.task_id, "--history"
        ]):
            cli.main()
            mock_emit.assert_called()
            self.assertEqual(len(mock_emit.call_args[0][0]), 1)

        # CLI: dag-proposal propose
        prop_file = self.root / "prop_input.json"
        prop_file.write_text(json.dumps({
            "reason": "Nova rota proposta via CLI",
            "nodes": [
                {"node_id": "step_cli", "title": "Passo CLI"},
                {"node_id": "step_cli2", "title": "Passo CLI 2"}
            ],
            "edges": [{"from_node": "step_cli", "to_node": "step_cli2"}]
        }), encoding="utf-8")

        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "dag-proposal", "--task", self.task_id,
            "--propose", str(prop_file), "--base-revision", "1"
        ]):
            cli.main()
            mock_emit.assert_called()
            prop_id = mock_emit.call_args[0][0]["id"]
            self.assertEqual(mock_emit.call_args[0][0]["status"], "proposed")

        # CLI: dag-proposal resolve approved
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "dag-proposal", "--proposal", prop_id, "--resolve", "approved"
        ]):
            cli.main()
            mock_emit.assert_called()
            self.assertEqual(mock_emit.call_args[0][0]["status"], "approved")

        # CLI: dag-proposal activate
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "dag-proposal", "--proposal", prop_id, "--activate"
        ]):
            cli.main()
            mock_emit.assert_called()
            self.assertEqual(mock_emit.call_args[0][0]["revision"], 2)


if __name__ == "__main__":
    unittest.main()
