from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from ai_coordinator import (
    Store,
    PlanNode,
    PlanEdge,
    PlanRevision,
    StaleRevisionError,
    DagValidationError,
    FlightBudget,
    FlightPlan,
    MissionControl,
    TrajectoryStatus,
    ConfidenceLevel,
    TokenUsageQuality,
    Recommendation,
    TelemetrySnapshot,
    BudgetProjection,
    TrajectoryPrediction,
    AlertDeduplicator,
    TrajectoryPredictor,
)
import ai_coordinator.cli as cli


class TrajectoryPredictorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "data"
        self.project = Path(self.temp_dir.name) / "project"
        self.project.mkdir()
        self.store = Store(self.root)
        self.task = self.store.new(self.project, "Missao com Trajectory Predictor")
        self.task_id = self.task["id"]

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    def _setup_linear_dag(self, node_estimates: dict[str, int] | None = None) -> PlanRevision:
        """Helper to create a linear DAG: step-1 -> step-2 -> step-3."""
        estimates = node_estimates or {}
        nodes = {
            "step-1": PlanNode("step-1", "Passo 1", metadata={"estimated_steps": estimates.get("step-1", 5)} if "step-1" in estimates else {}),
            "step-2": PlanNode("step-2", "Passo 2", metadata={"estimated_steps": estimates.get("step-2", 8)} if "step-2" in estimates else {}),
            "step-3": PlanNode("step-3", "Passo 3", metadata={"estimated_steps": estimates.get("step-3", 12)} if "step-3" in estimates else {}),
        }
        edges = [
            PlanEdge("step-1", "step-2"),
            PlanEdge("step-2", "step-3"),
        ]
        return self.store.create_plan_revision(self.task_id, nodes, edges, summary="Plano Linear")

    def _insert_run(self, status: str = "complete", usage: str = "") -> str:
        """Insert a mock run row for testing telemetry normalization."""
        run_id = uuid.uuid4().hex[:16]
        self.store.db.execute(
            """INSERT INTO runs(id, task_id, category, status, client, model, started, ended, attempts, input_path, usage)
               VALUES (?, ?, 'code', ?, 'opencode', 'model-1', '2026-10-20T00:00:00Z', '2026-10-20T00:01:00Z', '[]', '', ?)""",
            (run_id, self.task_id, status, usage),
        )
        return run_id

    # -------------------------------------------------------------------------
    # 3B.1: Normalização de telemetria e qualidade de dados
    # -------------------------------------------------------------------------
    def test_telemetry_normalization_complete_partial_missing_tokens(self):
        rev = self._setup_linear_dag()
        predictor = TrajectoryPredictor(self.store)

        # Case A: No runs recorded yet -> missing tokens
        snap_a = predictor.normalize_telemetry(self.task_id, rev)
        self.assertEqual(snap_a.total_runs, 0)
        self.assertEqual(snap_a.token_usage_quality, TokenUsageQuality.MISSING)
        self.assertIsNone(snap_a.token_usage_total)

        # Case B: Run 1 has token usage, Run 2 does not -> partial tokens
        self._insert_run(status="complete", usage=json.dumps({"total_tokens": 1500}))
        self._insert_run(status="complete", usage="")
        snap_b = predictor.normalize_telemetry(self.task_id, rev)
        self.assertEqual(snap_b.total_runs, 2)
        self.assertEqual(snap_b.token_usage_quality, TokenUsageQuality.PARTIAL)
        self.assertEqual(snap_b.token_usage_total, 1500)

        # Case C: All runs have usage -> complete tokens
        runs = self.store.runs(self.task_id)
        for r in runs:
            self.store.update_run(r["id"], usage=json.dumps({"total_tokens": 1200}))

        snap_c = predictor.normalize_telemetry(self.task_id, rev)
        self.assertEqual(snap_c.token_usage_quality, TokenUsageQuality.COMPLETE)
        self.assertEqual(snap_c.token_usage_total, 2400)

    # -------------------------------------------------------------------------
    # 3B.2: Seleção de horizonte móvel na DAG
    # -------------------------------------------------------------------------
    def test_moving_horizon_selection_topological(self):
        nodes = {
            "a": PlanNode("a", "Node A"),
            "b": PlanNode("b", "Node B"),
            "c": PlanNode("c", "Node C"),
            "d": PlanNode("d", "Node D"),
            "e": PlanNode("e", "Node E"),
        }
        edges = [
            PlanEdge("a", "b"),
            PlanEdge("b", "c"),
            PlanEdge("c", "d"),
            PlanEdge("d", "e"),
        ]
        rev = self.store.create_plan_revision(self.task_id, nodes, edges, summary="DAG 5 passos")
        predictor = TrajectoryPredictor(self.store)

        # From start with horizon 3 -> ["a", "b", "c"]
        horizon = predictor.select_horizon_nodes(rev, horizon_depth=3)
        self.assertEqual(horizon, ["a", "b", "c"])

        # Mark "a" as completed, next horizon should be ["b", "c", "d"]
        rev.nodes["a"].status = "completed"
        horizon_after_a = predictor.select_horizon_nodes(rev, current_node_id="b", horizon_depth=3)
        self.assertEqual(horizon_after_a, ["b", "c", "d"])

        # Invalid current node raises error
        with self.assertRaises(DagValidationError):
            predictor.select_horizon_nodes(rev, current_node_id="ghost-node")

    # -------------------------------------------------------------------------
    # 3B.3: Projeção de orçamento e alerta antecipado antes do teto (Cenário dos 82 passos)
    # -------------------------------------------------------------------------
    def test_early_warning_budget_exhaustion_before_threshold(self):
        """Simulates 82-step ceiling: at step 75, a 10-step horizon flags risk BEFORE hitting 82."""
        rev = self._setup_linear_dag(node_estimates={"step-1": 10})
        budget = FlightBudget(max_steps=82, max_uncheckpointed_steps=20)
        flight_plan = FlightPlan(
            task_id=self.task_id,
            goal="Plano de voo 82 passos",
            acceptance_criteria=["Todos os passos executados dentro do limite"],
            allowed_paths=[str(self.project)],
            budget=budget,
        )

        # Advance step count to 75
        self.store.db.execute("UPDATE tasks SET step_count=75 WHERE id=?", (self.task_id,))

        predictor = TrajectoryPredictor(self.store)
        pred = predictor.predict(self.task_id, flight_plan=flight_plan)

        # 75 consumed + 10 estimated = 85 > 82 limit -> AT_RISK
        self.assertEqual(pred.status, TrajectoryStatus.AT_RISK)
        self.assertIn("excedem o orcamento maximo", pred.reason)
        self.assertEqual(pred.remaining_steps, 7)  # 82 - 75 = 7
        self.assertEqual(pred.estimated_required_steps, 10)
        self.assertEqual(pred.recommendation, Recommendation.REPLAN_PROPOSAL)
        self.assertEqual(pred.confidence, ConfidenceLevel.HIGH)

    def test_radio_silence_exhaustion_early_warning(self):
        """Warns before radio silence blocks execution: at 8 uncheckpointed steps out of 10."""
        rev = self._setup_linear_dag(node_estimates={"step-1": 3})
        budget = FlightBudget(max_steps=100, max_uncheckpointed_steps=10)
        flight_plan = FlightPlan(
            task_id=self.task_id,
            goal="Plano de voo silencio",
            acceptance_criteria=["Checkpoints regulares"],
            allowed_paths=[str(self.project)],
            budget=budget,
        )

        # Advance uncheckpointed steps to 8
        self.store.db.execute("UPDATE tasks SET uncheckpointed_steps=8 WHERE id=?", (self.task_id,))

        predictor = TrajectoryPredictor(self.store)
        pred = predictor.predict(self.task_id, flight_plan=flight_plan)

        # 8 uncheckpointed + 3 projected = 11 >= 10 limit -> AT_RISK
        self.assertEqual(pred.status, TrajectoryStatus.AT_RISK)
        self.assertIn("Silencio de radio", pred.reason)
        self.assertEqual(pred.recommendation, Recommendation.CHECKPOINT_AND_REVIEW)

    def test_high_failure_rate_early_warning(self):
        rev = self._setup_linear_dag(node_estimates={"step-1": 2})
        # Record 4 runs, 3 of which failed (75% failure rate)
        self._insert_run(status="failed")
        self._insert_run(status="failed")
        self._insert_run(status="failed")
        self._insert_run(status="complete")

        predictor = TrajectoryPredictor(self.store)
        pred = predictor.predict(self.task_id)

        self.assertEqual(pred.status, TrajectoryStatus.AT_RISK)
        self.assertIn("falhas observada elevada", pred.reason)

    # -------------------------------------------------------------------------
    # 3B.3 / 3B.4: Dados insuficientes não geram falsas garantias nem palpites arbitrários
    # -------------------------------------------------------------------------
    def test_insufficient_data_does_not_guess_or_hallucinate(self):
        """When no declared node estimates or completed nodes exist, status is INSUFFICIENT_DATA."""
        # Nodes without declared estimates
        nodes = {
            "a": PlanNode("a", "Node A"),
            "b": PlanNode("b", "Node B"),
        }
        edges = [PlanEdge("a", "b")]
        self.store.create_plan_revision(self.task_id, nodes, edges, summary="Plano sem estimativas")

        predictor = TrajectoryPredictor(self.store)
        pred = predictor.predict(self.task_id)

        self.assertEqual(pred.status, TrajectoryStatus.INSUFFICIENT_DATA)
        self.assertEqual(pred.confidence, ConfidenceLevel.UNKNOWN)
        self.assertIsNone(pred.estimated_required_steps)
        self.assertEqual(pred.recommendation, Recommendation.GATHER_DATA)
        self.assertFalse(pred.requires_human_approval)

    # -------------------------------------------------------------------------
    # 3B.4: Deduplicação e histerese de alertas
    # -------------------------------------------------------------------------
    def test_alert_deduplication_hysteresis(self):
        """Ensures that repeated checks in the same risk state do not spam storage."""
        rev = self._setup_linear_dag(node_estimates={"step-1": 15})
        budget = FlightBudget(max_steps=20)
        flight_plan = FlightPlan(
            task_id=self.task_id,
            goal="Plano de voo deduplicacao",
            acceptance_criteria=["Alertas controlados"],
            allowed_paths=[str(self.project)],
            budget=budget,
        )
        self.store.db.execute("UPDATE tasks SET step_count=10 WHERE id=?", (self.task_id,))

        dedup = AlertDeduplicator()
        predictor = TrajectoryPredictor(self.store, deduplicator=dedup)

        # First call -> emits
        pred1 = predictor.predict(self.task_id, flight_plan=flight_plan)
        saved_id1 = predictor.record_prediction(pred1)
        self.assertIsNotNone(saved_id1)

        # Subsequent identical checks -> suppressed
        for _ in range(5):
            pred_dup = predictor.predict(self.task_id, flight_plan=flight_plan)
            res = predictor.record_prediction(pred_dup)
            self.assertIsNone(res)

        self.assertEqual(dedup.suppression_count(self.task_id, rev.revision), 5)

        # Transition to DEVIATED -> emits again
        self.store.db.execute("UPDATE tasks SET drift_status='deviated' WHERE id=?", (self.task_id,))
        pred_dev = predictor.predict(self.task_id, flight_plan=flight_plan)
        saved_id2 = predictor.record_prediction(pred_dev)
        self.assertIsNotNone(saved_id2)
        self.assertNotEqual(saved_id1, saved_id2)

    # -------------------------------------------------------------------------
    # 3B.5: Rejeição de revisões obsoletas e persistência SQLite
    # -------------------------------------------------------------------------
    def test_stale_revision_rejected(self):
        """A prediction queried against an obsolete plan revision is strictly rejected."""
        rev1 = self._setup_linear_dag()
        # Create revision 2
        nodes2 = dict(rev1.nodes)
        nodes2["step-4"] = PlanNode("step-4", "Passo 4")
        edges2 = list(rev1.edges) + [PlanEdge("step-3", "step-4")]
        rev2 = self.store.create_plan_revision(self.task_id, nodes2, edges2, summary="Revisao 2")
        self.assertEqual(rev2.revision, 2)

        predictor = TrajectoryPredictor(self.store)

        # Requesting prediction for old revision 1 must raise StaleRevisionError
        with self.assertRaises(StaleRevisionError):
            predictor.predict(self.task_id, plan_revision=1)

        # Requesting for active revision 2 succeeds
        pred = predictor.predict(self.task_id, plan_revision=2)
        self.assertEqual(pred.plan_revision, 2)

    def test_sqlite_persistence_and_retrieval(self):
        self._setup_linear_dag(node_estimates={"step-1": 2, "step-2": 3, "step-3": 4})
        predictor = TrajectoryPredictor(self.store)

        pred = predictor.predict(self.task_id)
        pred_id = predictor.record_prediction(pred, force=True)
        self.assertIsNotNone(pred_id)

        # Fetch by ID
        saved = self.store.get_trajectory_prediction(pred_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["id"], pred.id)
        self.assertEqual(saved["task_id"], self.task_id)
        self.assertEqual(saved["status"], pred.status)

        # List predictions
        history = self.store.list_trajectory_predictions(self.task_id)
        self.assertGreaterEqual(len(history), 1)

        # Fetch latest
        latest = self.store.get_latest_trajectory_prediction(self.task_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], pred.id)

        # Verify audit event in trajectory_events
        events = self.store.get_trajectory_events(self.task_id)
        pred_events = [e for e in events if e.event_type == "trajectory_prediction"]
        self.assertGreaterEqual(len(pred_events), 1)

    # -------------------------------------------------------------------------
    # Imutabilidade da DAG: O preditor nunca altera o plano nem executa comandos
    # -------------------------------------------------------------------------
    def test_pure_read_only_predictor_no_dag_mutation(self):
        rev_before = self._setup_linear_dag()
        predictor = TrajectoryPredictor(self.store)

        # Perform multiple predictions
        for _ in range(3):
            predictor.predict(self.task_id)

        rev_after = self.store.get_plan_revision(self.task_id)
        self.assertEqual(rev_before.revision, rev_after.revision)
        self.assertEqual(len(rev_before.nodes), len(rev_after.nodes))
        self.assertEqual(len(rev_before.edges), len(rev_after.edges))
        for nid, node in rev_before.nodes.items():
            self.assertEqual(node.status, rev_after.nodes[nid].status)

    # -------------------------------------------------------------------------
    # Integração CLI
    # -------------------------------------------------------------------------
    def test_cli_integration_trajectory_predict(self):
        self._setup_linear_dag(node_estimates={"step-1": 2, "step-2": 3})

        with patch("sys.argv", ["coordinator.py", "--root", str(self.root), "trajectory-predict", "--task", self.task_id, "--save"]):
            from io import StringIO
            import sys
            saved_stdout = sys.stdout
            try:
                sys.stdout = StringIO()
                cli.main()
                output = sys.stdout.getvalue().strip()
                parsed = json.loads(output)
                self.assertEqual(parsed["task_id"], self.task_id)
                self.assertIn("saved_id", parsed)
                self.assertIn("status", parsed)
            finally:
                sys.stdout = saved_stdout

        with patch("sys.argv", ["coordinator.py", "--root", str(self.root), "trajectory-history", "--task", self.task_id]):
            saved_stdout = sys.stdout
            try:
                sys.stdout = StringIO()
                cli.main()
                output = sys.stdout.getvalue().strip()
                history = json.loads(output)
                self.assertIsInstance(history, list)
                self.assertGreaterEqual(len(history), 1)
            finally:
                sys.stdout = saved_stdout


if __name__ == "__main__":
    unittest.main()
