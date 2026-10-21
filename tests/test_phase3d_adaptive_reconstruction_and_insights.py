"""Acceptance test suite for Phase 3D: Adaptive Trajectory Reconstruction, Continuous Experience, and Insight Engine."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_coordinator import (
    Store,
    RunState,
    PlanNode,
    PlanEdge,
    PlanRevision,
    TrajectoryProposal,
    DagValidationError,
    CycleDetectedError,
    StaleRevisionError,
    ActiveWorkerFencingError,
    ProposalNotApprovedError,
    InvariantDefinition,
    InvariantEvidence,
    InvariantOracle,
    InvariantStatus,
    compute_sha256,
    ResourceLedger,
    ResourceRecord,
    ResourceState,
    ResourceType,
    ResourceAction,
    UnresolvedResourceError,
    ExperienceCategory,
    ExperienceOutcome,
    ExperienceVisibility,
    ExperienceAuthor,
    ExperienceRecord,
    ExperienceStore,
    InsightType,
    InsightStatus,
    InsightPresentation,
    Insight,
    InsightEngine,
    ReconstructionError,
    ReconnectionPointNotFoundError,
    AlternativeRoutePreview,
    calculate_reconnection_points,
    calculate_upstream_prerequisites,
    splice_alternative_route,
    TrajectoryReconstructor,
)
from ai_coordinator import cli


class Phase3DAdaptiveReconstructionAndInsightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "coordinator_data"
        self.proj_dir = Path(self.tmp.name) / "proj"
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root)
        self.task_id = "task-3d-001"

        # Initialize base task
        self.store.new(self.proj_dir, "Missao Fase 3D Reconstrucao e Insights")
        self.store.db.execute(
            "UPDATE tasks SET id=? WHERE project=?",
            (self.task_id, str(self.proj_dir.resolve())),
        )
        self.store.db.commit()

        # Create base linear revision R1: n1 -> n2 -> n3
        self.nodes_r1 = [
            PlanNode(node_id="n1", title="Setup Environment", status="completed"),
            PlanNode(node_id="n2", title="Execute Data Migration", status="pending"),
            PlanNode(node_id="n3", title="Verify Invariants and Land", status="pending"),
        ]
        self.edges_r1 = [
            PlanEdge(from_node="n1", to_node="n2"),
            PlanEdge(from_node="n2", to_node="n3"),
        ]
        self.r1 = self.store.create_plan_revision(
            self.task_id,
            nodes=self.nodes_r1,
            edges=self.edges_r1,
            summary="R1 Base Plan",
        )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    # -------------------------------------------------------------------------
    # Scenario 1: Alternative route preview preserves dependencies
    # -------------------------------------------------------------------------
    def test_alternative_route_preview_preserves_upstream_and_downstream_dependencies(self):
        reconstructor = TrajectoryReconstructor(self.store)

        # Replacement nodes for failed node n2
        replacement = [
            PlanNode(node_id="alt_n2a", title="Batch Chunk Extraction"),
            PlanNode(node_id="alt_n2b", title="Incremental Ingestion"),
        ]

        preview = reconstructor.preview_alternative_route(
            task_id=self.task_id,
            base_revision=1,
            failed_node_id="n2",
            replacement_nodes=replacement,
        )

        self.assertEqual(preview.task_id, self.task_id)
        self.assertEqual(preview.base_revision, 1)
        self.assertEqual(preview.failed_node_id, "n2")
        self.assertEqual(preview.upstream_nodes, ["n1"])
        self.assertEqual(preview.reconnection_points, ["n3"])
        self.assertEqual(preview.replacement_node_ids, ["alt_n2a", "alt_n2b"])
        self.assertEqual(sorted(preview.unaffected_node_ids), ["n1", "n3"])
        self.assertEqual(preview.total_nodes_count, 4)  # n1, alt_n2a, alt_n2b, n3
        self.assertTrue(preview.is_valid_dag)

        # Verify topological sort order: n1 -> alt_n2a -> alt_n2b -> n3
        order = preview.topological_order
        self.assertLess(order.index("n1"), order.index("alt_n2a"))
        self.assertLess(order.index("alt_n2a"), order.index("alt_n2b"))
        self.assertLess(order.index("alt_n2b"), order.index("n3"))

    # -------------------------------------------------------------------------
    # Scenario 2: Splicing with custom reconnection point
    # -------------------------------------------------------------------------
    def test_custom_reconnection_point_splicing(self):
        # Create DAG with branching: n1 -> n2 -> n3; n1 -> n2 -> n4
        nodes = [
            PlanNode(node_id="n1", title="Init"),
            PlanNode(node_id="n2", title="Branch Point"),
            PlanNode(node_id="n3", title="Path A"),
            PlanNode(node_id="n4", title="Path B"),
        ]
        edges = [
            PlanEdge(from_node="n1", to_node="n2"),
            PlanEdge(from_node="n2", to_node="n3"),
            PlanEdge(from_node="n2", to_node="n4"),
        ]
        rev = self.store.create_plan_revision(self.task_id, nodes, edges, "Branching DAG")

        reconstructor = TrajectoryReconstructor(self.store)
        rep = [PlanNode(node_id="alt_bypass", title="Direct Bypass to Path B")]

        preview = reconstructor.preview_alternative_route(
            task_id=self.task_id,
            base_revision=rev.revision,
            failed_node_id="n2",
            replacement_nodes=rep,
            reconnection_node_id="n4",
        )

        self.assertEqual(preview.reconnection_points, ["n4"])
        self.assertIn("alt_bypass", preview.topological_order)
        self.assertIn("n4", preview.topological_order)

    # -------------------------------------------------------------------------
    # Scenario 3: Invalid reconnection point strictly rejected
    # -------------------------------------------------------------------------
    def test_invalid_reconnection_point_rejected(self):
        reconstructor = TrajectoryReconstructor(self.store)
        rep = [PlanNode(node_id="alt_x", title="Step")]

        # Non-existent node
        with self.assertRaises(ReconnectionPointNotFoundError):
            reconstructor.preview_alternative_route(
                task_id=self.task_id,
                base_revision=1,
                failed_node_id="n2",
                replacement_nodes=rep,
                reconnection_node_id="non_existent_node",
            )

        # Self-reconnection to the failed node
        with self.assertRaises(ReconnectionPointNotFoundError):
            reconstructor.preview_alternative_route(
                task_id=self.task_id,
                base_revision=1,
                failed_node_id="n2",
                replacement_nodes=rep,
                reconnection_node_id="n2",
            )

    # -------------------------------------------------------------------------
    # Scenario 4: Reconstruction strictly rejects introduced cycles
    # -------------------------------------------------------------------------
    def test_reconstruction_cycle_detection(self):
        reconstructor = TrajectoryReconstructor(self.store)
        # Attempt to pass replacement edge that loops back to upstream node n1
        rep = [PlanNode(node_id="alt_bad", title="Bad Step")]
        rep_edges = [PlanEdge(from_node="alt_bad", to_node="n1")]

        with self.assertRaises(CycleDetectedError):
            reconstructor.preview_alternative_route(
                task_id=self.task_id,
                base_revision=1,
                failed_node_id="n2",
                replacement_nodes=rep,
                replacement_edges=rep_edges,
            )

    # -------------------------------------------------------------------------
    # Scenario 5: Proposal creation is unactivated and safe (no DAG mutation)
    # -------------------------------------------------------------------------
    def test_propose_alternative_route_pure_read_only_no_active_dag_mutation(self):
        reconstructor = TrajectoryReconstructor(self.store)
        rep = [PlanNode(node_id="alt_safe", title="Safe Alternative")]

        proposal = reconstructor.propose_alternative_route(
            task_id=self.task_id,
            base_revision=1,
            failed_node_id="n2",
            replacement_nodes=rep,
            reason="Contorno seguro de erro de timeout",
        )

        self.assertIsInstance(proposal, TrajectoryProposal)
        self.assertEqual(proposal.status, "proposed")
        self.assertEqual(proposal.base_revision, 1)

        # Active revision must remain exactly R1
        current_rev = self.store.get_plan_revision(self.task_id)
        self.assertEqual(current_rev.revision, 1)
        self.assertEqual(set(current_rev.nodes.keys()), {"n1", "n2", "n3"})

    # -------------------------------------------------------------------------
    # Scenario 6: Activation safety fencing gates
    # -------------------------------------------------------------------------
    def test_activation_safety_fencing_gates(self):
        reconstructor = TrajectoryReconstructor(self.store)
        rep = [PlanNode(node_id="alt_gate", title="Gated Step")]
        proposal = reconstructor.propose_alternative_route(
            task_id=self.task_id,
            base_revision=1,
            failed_node_id="n2",
            replacement_nodes=rep,
            reason="Test Gate",
        )

        # Gate 1: Unapproved proposal activation rejected
        with self.assertRaises(ProposalNotApprovedError):
            self.store.activate_trajectory_proposal(proposal.id)

        # Approve proposal
        self.store.resolve_trajectory_proposal(proposal.id, "approved")

        # Gate 2: Active worker running blocks activation
        self.store.db.execute(
            """INSERT INTO runs(
                id, task_id, category, status, started, attempts, input_path, revision
            ) VALUES ('run-active-1', ?, 'architecture', 'running', '2026-10-21T00:00:00Z', '[]', 'in.json', 1)""",
            (self.task_id,),
        )
        self.store.db.commit()

        with self.assertRaises(ActiveWorkerFencingError):
            self.store.activate_trajectory_proposal(proposal.id)

        # Mark run as complete
        self.store.db.execute("UPDATE runs SET status='complete' WHERE id='run-active-1'")
        self.store.db.commit()

        # Gate 3: Uncertain resource in ledger blocks activation
        self.store.db.execute(
            """INSERT INTO resource_ledger(
                id, task_id, plan_revision, resource_type, canonical_id,
                action, idempotency_key, status, created_at, updated_at
            ) VALUES ('res-1', ?, 1, 'file', 'D:/app/file.txt', 'create_file', 'key-1', 'uncertain', '2026-10-21T00:00:00Z', '2026-10-21T00:00:00Z')""",
            (self.task_id,),
        )
        self.store.db.commit()

        with self.assertRaises(UnresolvedResourceError):
            self.store.activate_trajectory_proposal(proposal.id)

        # Reconcile resource to clean state
        self.store.db.execute("UPDATE resource_ledger SET status='retained' WHERE id='res-1'")
        self.store.db.commit()

        # Gate 4: Invariant verification gate
        self.store.db.execute(
            """INSERT INTO invariants(id, task_id, description, required, verifier_type, created_at)
            VALUES ('inv_safety', ?, 'Must pass safety invariant', 1, 'trusted_test_runner', '2026-10-21T00:00:00Z')""",
            (self.task_id,),
        )
        self.store.db.commit()

        # Evaluation missing -> blocked
        with self.assertRaises(Exception):
            self.store.activate_trajectory_proposal(proposal.id)

        # Insert passing evaluation for active revision 1
        self.store.db.execute(
            """INSERT INTO invariant_evaluations(
                id, task_id, plan_revision, invariant_id, status, verified, evaluated_at
            ) VALUES ('eval-1', ?, 1, 'inv_safety', 'pass', 1, '2026-10-21T00:00:00Z')""",
            (self.task_id,),
        )
        self.store.db.commit()

        # Now all gates pass: activation succeeds!
        new_rev = self.store.activate_trajectory_proposal(proposal.id)
        self.assertEqual(new_rev.revision, 2)
        self.assertEqual(new_rev.parent_revision, 1)
        self.assertIn("alt_gate", new_rev.nodes)
        self.assertNotIn("n2", new_rev.nodes)

    # -------------------------------------------------------------------------
    # Scenario 7: Experience capture and deterministic retrieval
    # -------------------------------------------------------------------------
    def test_experience_capture_and_deterministic_search(self):
        exp_store = self.store.experience_store

        rec1 = exp_store.record_experience(
            project_id=str(self.proj_dir),
            problem_pattern="SQLite locked during concurrent worker checkpoint writes",
            solution_pattern="Enable PRAGMA busy_timeout=20000 and WAL mode",
            category=ExperienceCategory.TOOL_FIX,
            outcome=ExperienceOutcome.SUCCESS,
            context_tags=["sqlite", "concurrency", "lock"],
            confidence="high",
            author=ExperienceAuthor.AGENT,
        )

        rec2 = exp_store.record_experience(
            project_id=str(self.proj_dir),
            problem_pattern="Memory leak in long-lived streaming connection",
            solution_pattern="Use bounded ring buffer chunking with 4KB chunks",
            category=ExperienceCategory.ARCHITECTURE_DECISION,
            outcome=ExperienceOutcome.SUCCESS,
            context_tags=["streaming", "memory", "buffer"],
            confidence="high",
            author=ExperienceAuthor.AGENT,
        )

        # Search by tags
        results = exp_store.find_experiences(
            project_id=str(self.proj_dir),
            tags=["sqlite"],
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, rec1.id)

        # Search by query text
        results = exp_store.find_experiences(
            project_id=str(self.proj_dir),
            query_text="concurrent worker locked",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, rec1.id)

    # -------------------------------------------------------------------------
    # Scenario 8: Experience privacy filtering
    # -------------------------------------------------------------------------
    def test_experience_privacy_filtering(self):
        exp_store = self.store.experience_store

        # Private human note
        rec_private = exp_store.record_experience(
            project_id=str(self.proj_dir),
            problem_pattern="Refatoracao de arquitetura de tokens",
            solution_pattern="Decisao privada sobre custos e chave secreta",
            category=ExperienceCategory.USER_PREFERENCE,
            visibility=ExperienceVisibility.PRIVATE_HUMAN,
            author=ExperienceAuthor.HUMAN,
        )

        # Public project note
        rec_shared = exp_store.record_experience(
            project_id=str(self.proj_dir),
            problem_pattern="Refatoracao de endpoints de streaming",
            solution_pattern="Uso de iteradores assincronos",
            category=ExperienceCategory.ARCHITECTURE_DECISION,
            visibility=ExperienceVisibility.PROJECT_SHARED,
            author=ExperienceAuthor.AGENT,
        )

        # When caller is agent: private record is strictly hidden
        agent_results = exp_store.find_experiences(
            project_id=str(self.proj_dir),
            caller_role="agent",
        )
        self.assertEqual(len(agent_results), 1)
        self.assertEqual(agent_results[0].id, rec_shared.id)

        # When queried by ID as agent -> None
        self.assertIsNone(exp_store.get_experience(rec_private.id, caller_role="agent"))

        # When caller is human: private record is returned
        human_results = exp_store.find_experiences(
            project_id=str(self.proj_dir),
            caller_role="human",
        )
        self.assertEqual(len(human_results), 2)
        self.assertIsNotNone(exp_store.get_experience(rec_private.id, caller_role="human"))

    # -------------------------------------------------------------------------
    # Scenario 9: Cross-project experience scope bounding
    # -------------------------------------------------------------------------
    def test_cross_project_experience_scope_bounding(self):
        exp_store = self.store.experience_store

        other_proj = str(Path(self.tmp.name) / "other_project")
        rec_other = exp_store.record_experience(
            project_id=other_proj,
            problem_pattern="Data processing pipeline failure in project B",
            solution_pattern="Reconnection retry with backoff",
            visibility=ExperienceVisibility.PROJECT_SHARED,
        )

        # Unauthorized caller cannot see other_proj
        results = exp_store.find_experiences(
            project_id=str(self.proj_dir),
            caller_role="agent",
            caller_authorized_projects=[str(self.proj_dir)],
        )
        self.assertEqual(len(results), 0)

        # With authorization -> other_proj is returned
        results_auth = exp_store.find_experiences(
            project_id=str(self.proj_dir),
            caller_role="agent",
            caller_authorized_projects=[str(self.proj_dir), other_proj],
        )
        self.assertEqual(len(results_auth), 1)
        self.assertEqual(results_auth[0].id, rec_other.id)

    # -------------------------------------------------------------------------
    # Scenario 10: Insight Engine - Active exploration with 3 canonical routes
    # -------------------------------------------------------------------------
    def test_insight_engine_explore_generates_three_routes_and_incubates(self):
        engine = self.store.insight_engine

        # Record a past experience to ensure learning is integrated
        self.store.record_experience(
            project_id=str(self.proj_dir),
            problem_pattern="Estrutura de dados para roteamento adaptativo",
            solution_pattern="Separar contratos em modulos puros desacoplados",
            category=ExperienceCategory.ARCHITECTURE_DECISION,
        )

        insight = engine.explore(
            project_id=str(self.proj_dir),
            topic_or_goal="Melhorar arquitetura do sistema",
            task_id=self.task_id,
        )

        self.assertIsInstance(insight, Insight)
        self.assertEqual(insight.type, InsightType.EXPLORATION)
        self.assertEqual(insight.status, InsightStatus.INCUBATING)
        self.assertEqual(len(insight.alternatives), 3)

        # Verify the 3 canonical routes
        routes = {a["route_key"]: a for a in insight.alternatives}
        self.assertIn("A", routes)
        self.assertIn("B", routes)
        self.assertIn("C", routes)

        self.assertIn("Pragmática", routes["A"]["name"])
        self.assertIn("Estrutural", routes["B"]["name"])
        self.assertIn("Investigativa", routes["C"]["name"])

        self.assertIsNotNone(insight.suggested_first_step)
        self.assertIn("provenance", insight.to_dict())
        self.assertTrue(insight.provenance.get("generated_deterministically"))

    # -------------------------------------------------------------------------
    # Scenario 11: Insight Engine - Cross-project connection without false equivalence
    # -------------------------------------------------------------------------
    def test_insight_engine_connect_cross_project(self):
        engine = self.store.insight_engine
        source_proj = "d:/data-pipeline"

        # Record solution in source project
        self.store.record_experience(
            project_id=source_proj,
            problem_pattern="Reconstrucao de fluxo apos falha de processo",
            solution_pattern="Splicing de DAG mantendo invariantes upstream",
            category=ExperienceCategory.ARCHITECTURE_DECISION,
        )

        insight = engine.connect(
            source_project_id=source_proj,
            target_project_id=str(self.proj_dir),
            topic="Reconstrucao de fluxo de coordenacao",
            caller_authorized_projects=[source_proj, str(self.proj_dir)],
        )

        self.assertIsNotNone(insight)
        self.assertEqual(insight.type, InsightType.CROSS_PROJECT_CONNECTION)
        self.assertFalse(insight.provenance["equivalence_assumed"])
        self.assertEqual(insight.provenance["source_project"], source_proj)

    # -------------------------------------------------------------------------
    # Scenario 12: Insight Engine - Reevaluation on failure
    # -------------------------------------------------------------------------
    def test_insight_engine_reevaluate_on_failure(self):
        engine = self.store.insight_engine

        # Record known fix
        self.store.record_experience(
            project_id=str(self.proj_dir),
            problem_pattern="Timeout de conexao com agente de terminal",
            solution_pattern="Aumentar timeout de handshake para 30s",
            category=ExperienceCategory.TOOL_FIX,
        )

        insight = engine.reevaluate(
            task_id=self.task_id,
            project_id=str(self.proj_dir),
            current_issue="Timeout de conexao com agente de terminal",
            observed_error="ChildProcessTimeoutError",
        )

        self.assertEqual(insight.type, InsightType.RE_EVALUATION)
        self.assertEqual(insight.presentation, InsightPresentation.NOW)
        self.assertGreater(len(insight.alternatives), 0)

    # -------------------------------------------------------------------------
    # Scenario 13: Incubator lifecycle and promotion to TrajectoryProposal
    # -------------------------------------------------------------------------
    def test_insight_incubator_lifecycle_and_promotion_to_proposal(self):
        engine = self.store.insight_engine

        insight = engine.explore(
            project_id=str(self.proj_dir),
            topic_or_goal="Refatorar componente de roteamento",
            task_id=self.task_id,
        )

        # Incubator listing
        incubating = engine.list_incubator(project_id=str(self.proj_dir))
        self.assertEqual(len(incubating), 1)
        self.assertEqual(incubating[0].id, insight.id)

        # Promote Route A to TrajectoryProposal
        proposal = engine.promote_to_trajectory_proposal(
            store=self.store,
            insight_id=insight.id,
            task_id=self.task_id,
            base_revision=1,
            route_choice="A",
        )

        self.assertIsInstance(proposal, TrajectoryProposal)
        self.assertEqual(proposal.status, "proposed")

        # Insight status is now promoted
        updated_ins = engine.get_insight(insight.id)
        self.assertEqual(updated_ins.status, InsightStatus.PROMOTED)

        # Proposal requires explicit approval before activation
        with self.assertRaises(ProposalNotApprovedError):
            self.store.activate_trajectory_proposal(proposal.id)

    # -------------------------------------------------------------------------
    # Scenario 14: CLI integration tests
    # -------------------------------------------------------------------------
    def test_cli_integration_experience_insight_and_route_reconstruct(self):
        from unittest.mock import patch
        import io

        # 1. CLI experience record and find
        with patch("sys.argv", [
            "ai-coordinator", "--root", str(self.root),
            "experience", "--project", str(self.proj_dir),
            "--problem", "Erro de conexao de rede",
            "--solution", "Adicionar retry com jitter",
            "--category", "tool_fix",
        ]):
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = cli.main()
            self.assertEqual(code, 0)
            data = json.loads(stdout.getvalue())
            self.assertIn("id", data)

        with patch("sys.argv", [
            "ai-coordinator", "--root", str(self.root),
            "experience", "--project", str(self.proj_dir),
            "--find", "conexao",
        ]):
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = cli.main()
            self.assertEqual(code, 0)
            data = json.loads(stdout.getvalue())
            self.assertEqual(len(data), 1)

        # 2. CLI insight explore and list
        with patch("sys.argv", [
            "ai-coordinator", "--root", str(self.root),
            "insight", "--project", str(self.proj_dir),
            "--explore", "Implementar cache distribuido",
        ]):
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = cli.main()
            self.assertEqual(code, 0)
            data = json.loads(stdout.getvalue())
            self.assertEqual(data["type"], "exploration")
            self.assertEqual(len(data["alternatives"]), 3)

        # 3. CLI route-reconstruct preview
        rep_json = json.dumps([{"node_id": "cli_alt_1", "title": "Passo Alternativo CLI"}])
        with patch("sys.argv", [
            "ai-coordinator", "--root", str(self.root),
            "route-reconstruct", "--task", self.task_id,
            "--failed-node", "n2",
            "--replacement-nodes", rep_json,
            "--preview",
        ]):
            stdout = io.StringIO()
            with patch("sys.stdout", stdout):
                code = cli.main()
            self.assertEqual(code, 0)
            data = json.loads(stdout.getvalue())
            self.assertTrue(data["is_valid_dag"])
            self.assertEqual(data["reconnection_points"], ["n3"])


if __name__ == "__main__":
    unittest.main()
