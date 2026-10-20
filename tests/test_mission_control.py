from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_coordinator import (
    Store,
    RunState,
    FlightPlan,
    FlightBudget,
    LandingCheck,
    MissionControl,
    MissionControlError,
    RadioSilenceError,
    BudgetExceededError,
    UnauthorizedPathError,
    LandingVerificationError,
    DeviationType,
    DriftStatus,
    is_path_allowed,
)
import ai_coordinator.cli as cli


class MissionControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "data"
        self.project = Path(self.temp_dir.name) / "project"
        self.project.mkdir()
        self.src_dir = self.project / "src"
        self.src_dir.mkdir()
        self.docs_dir = self.project / "docs"
        self.docs_dir.mkdir()
        self.store = Store(self.root)

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Fase 2A: Plano de Voo Formalizado (FlightPlan)
    # -------------------------------------------------------------------------
    def test_flight_plan_contract_validation(self):
        # Valid plan
        plan = FlightPlan(
            task_id="task-1234",
            goal="Construir sistema de autenticacao seguro",
            acceptance_criteria=["JWT implementado", "Rotas protegidas", "Testes passando"],
            allowed_paths=[str(self.src_dir), str(self.docs_dir)],
            budget=FlightBudget(max_steps=30, max_elapsed_seconds=1800, max_uncheckpointed_steps=10, max_worker_calls=8),
            mandatory_landing_checks=["unit_tests", "security_audit"],
        )
        plan.validate()
        data = plan.to_dict()
        reconstituted = FlightPlan.from_dict(data)
        self.assertEqual(reconstituted.task_id, "task-1234")
        self.assertEqual(reconstituted.goal, plan.goal)
        self.assertEqual(reconstituted.budget.max_uncheckpointed_steps, 10)

        # Invalid plans: empty goal
        with self.assertRaises(MissionControlError):
            FlightPlan(task_id="t1", goal="", acceptance_criteria=["c1"], allowed_paths=[str(self.project)]).validate()

        # Invalid plans: empty acceptance criteria
        with self.assertRaises(MissionControlError):
            FlightPlan(task_id="t1", goal="Goal", acceptance_criteria=[], allowed_paths=[str(self.project)]).validate()

        # Invalid plans: empty allowed_paths
        with self.assertRaises(MissionControlError):
            FlightPlan(task_id="t1", goal="Goal", acceptance_criteria=["c1"], allowed_paths=[]).validate()

        # Invalid budget: negative or zero steps
        with self.assertRaises(MissionControlError):
            FlightPlan(
                task_id="t1",
                goal="Goal",
                acceptance_criteria=["c1"],
                allowed_paths=[str(self.project)],
                budget=FlightBudget(max_steps=0),
            ).validate()

    def test_flight_plan_persistence_and_task_mirror(self):
        plan_dict = {
            "goal": "Implementar Mission Control",
            "acceptance_criteria": ["Crit 1", "Crit 2"],
            "allowed_paths": [str(self.project)],
            "budget": {"max_steps": 25, "max_uncheckpointed_steps": 5, "max_worker_calls": 5},
            "mandatory_landing_checks": ["ci_green"],
        }
        task = self.store.new(self.project, "Tarefa com plano de voo", flight_plan=plan_dict)
        task_id = task["id"]

        # Verify retrieval via get_flight_plan
        retrieved = self.store.get_flight_plan(task_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.goal, "Implementar Mission Control")
        self.assertEqual(retrieved.budget.max_uncheckpointed_steps, 5)
        self.assertEqual(retrieved.mandatory_landing_checks, ["ci_green"])

        # Verify task dict includes flight plan and landing checks
        task_data = self.store.task(task_id)
        self.assertIsNotNone(task_data["flight_plan"])
        self.assertEqual(task_data["flight_plan"]["budget"]["max_steps"], 25)

        # Verify state.json mirror
        state_path = self.root / "tasks" / task_id / "state.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(state.get("flight_plan"))
        self.assertEqual(state["flight_plan"]["mandatory_landing_checks"], ["ci_green"])

    # -------------------------------------------------------------------------
    # 2. Fase 2B: Telemetria e Silêncio de Rádio
    # -------------------------------------------------------------------------
    def test_radio_silence_detection_and_blocking(self):
        plan_dict = {
            "goal": "Monitoramento de telemetria",
            "acceptance_criteria": ["Telemetria ativa"],
            "allowed_paths": [str(self.project)],
            "budget": {"max_steps": 50, "max_uncheckpointed_steps": 5},
            "mandatory_landing_checks": [],
        }
        task = self.store.new(self.project, "Tarefa de telemetria", flight_plan=plan_dict)
        task_id = task["id"]

        # Steps 1 to 4: allowed, uncheckpointed steps increment
        for step in range(1, 5):
            res = self.store.record_step(task_id, f"action_{step}", details=f"Execucao {step}")
            self.assertEqual(res["step_count"], step)
            self.assertEqual(res["uncheckpointed_steps"], step)
            self.assertEqual(res["drift_status"], DriftStatus.NOMINAL)

        # Pre-flight clearance should still pass
        self.store.mission_control.check_clearance(task_id)

        # Step 5: hits the threshold (5 >= 5)
        res5 = self.store.record_step(task_id, "action_5", details="Quinto passo sem checkpoint")
        self.assertEqual(res5["step_count"], 5)
        self.assertEqual(res5["uncheckpointed_steps"], 5)
        self.assertEqual(res5["drift_status"], DriftStatus.BLOCKED)

        # Telemetry should contain the PROLONGED_SILENCE deviation event
        summary = self.store.telemetry_summary(task_id)
        events = summary["recent_events"]
        silence_events = [e for e in events if e["event_type"] == DeviationType.PROLONGED_SILENCE]
        self.assertEqual(len(silence_events), 1)
        self.assertEqual(silence_events[0]["severity"], "warning")

        # Now any attempt to get clearance or reserve a worker must be BLOCKED
        with self.assertRaises(RadioSilenceError) as ctx:
            self.store.mission_control.check_clearance(task_id)
        self.assertIn("Silencio de radio prolongado", str(ctx.exception))

        with self.assertRaises(RadioSilenceError) as ctx:
            self.store.reserve(task_id, "code", "novo prompt", 5)
        self.assertIn("Silencio de radio prolongado", str(ctx.exception))

    def test_radio_silence_cleared_on_checkpoint(self):
        plan_dict = {
            "goal": "Recuperacao de silencio",
            "acceptance_criteria": ["Retomada com sucesso"],
            "allowed_paths": [str(self.project)],
            "budget": {"max_steps": 50, "max_uncheckpointed_steps": 3},
            "mandatory_landing_checks": [],
        }
        task = self.store.new(self.project, "Tarefa recuperavel", flight_plan=plan_dict)
        task_id = task["id"]

        # Advance 3 steps without checkpoint -> blocked
        for s in range(3):
            self.store.record_step(task_id, f"step_{s}")

        with self.assertRaises(RadioSilenceError):
            self.store.mission_control.check_clearance(task_id)

        # Save a progress checkpoint
        updated = self.store.checkpoint(task_id, revision=1, patch={"summary": "Progresso registrado apos 3 passos"})
        self.assertEqual(updated["revision"], 2)

        # Verify uncheckpointed steps was reset to 0 and drift status is nominal
        summary = self.store.telemetry_summary(task_id)
        self.assertEqual(summary["uncheckpointed_steps"], 0)
        self.assertEqual(summary["drift_status"], DriftStatus.NOMINAL)

        # Pre-flight clearance succeeds and reservation is unblocked
        self.store.mission_control.check_clearance(task_id)
        run_id, folder, _ = self.store.reserve(task_id, "code", "executar apos checkpoint", 5)
        self.assertTrue(run_id)

    # -------------------------------------------------------------------------
    # Key Scenario: Replicar o caso de 82 passos sem checkpoint
    # -------------------------------------------------------------------------
    def test_scenario_82_steps_without_checkpoint_reproduction(self):
        """Reproduce the scenario where execution advances 82 steps without checkpoint.

        At step 82, prolonged silence deviation is deterministically raised,
        blocking autonomous execution until a checkpoint is recorded.
        """
        plan_dict = {
            "goal": "Replicar cenario de 82 passos sem checkpoint",
            "acceptance_criteria": ["Detecao exata de silencio no passo 82", "Bloqueio deterministico"],
            "allowed_paths": [str(self.project)],
            "budget": {
                "max_steps": 100,
                "max_uncheckpointed_steps": 82,
                "max_worker_calls": 20,
            },
            "mandatory_landing_checks": [],
        }
        task = self.store.new(self.project, "Cenario 82 passos", flight_plan=plan_dict)
        task_id = task["id"]

        # Run 81 steps: all nominal
        for step in range(1, 82):
            res = self.store.record_step(task_id, f"tool_call_{step}", details=f"Subtarefa iteracao {step}")
            self.assertEqual(res["step_count"], step)
            self.assertEqual(res["uncheckpointed_steps"], step)
            self.assertEqual(res["drift_status"], DriftStatus.NOMINAL)

        # Clearance is still granted at step 81
        self.store.mission_control.check_clearance(task_id)

        # Step 82: exactly hits the prolonged silence limit
        res_82 = self.store.record_step(task_id, "tool_call_82", details="82 passos sem checkpoint alcancados")
        self.assertEqual(res_82["step_count"], 82)
        self.assertEqual(res_82["uncheckpointed_steps"], 82)
        self.assertEqual(res_82["drift_status"], DriftStatus.BLOCKED)

        # Deviation event must exist in telemetry
        summary = self.store.telemetry_summary(task_id)
        silence_events = [e for e in summary["recent_events"] if e["event_type"] == DeviationType.PROLONGED_SILENCE]
        self.assertTrue(len(silence_events) >= 1)
        self.assertIn("82 passos sem checkpoint", silence_events[0]["details"])

        # Attempting step 83 or reserve must be strictly BLOCKED
        with self.assertRaises(RadioSilenceError) as ctx:
            self.store.mission_control.check_clearance(task_id)
        self.assertIn("82 passos sem checkpoint", str(ctx.exception))

        with self.assertRaises(RadioSilenceError):
            self.store.reserve(task_id, "code", "tentativa passo 83 bloqueada", 10)

        # Autonomous progress halted. Supervisor issues formal checkpoint:
        self.store.checkpoint(
            task_id,
            revision=1,
            patch={
                "summary": "Ponto de controle apos 82 passos de analise detalhada",
                "decisions": ["Consolidado resultado parcial dos 82 passos"],
            }
        )

        # Uncheckpointed counter reset, drift cleared
        summary_after = self.store.telemetry_summary(task_id)
        self.assertEqual(summary_after["uncheckpointed_steps"], 0)
        self.assertEqual(summary_after["step_count"], 82)
        self.assertEqual(summary_after["drift_status"], DriftStatus.NOMINAL)

        # Progression unblocked: step 83 now succeeds
        res_83 = self.store.record_step(task_id, "tool_call_83", details="Retomada autorizada")
        self.assertEqual(res_83["step_count"], 83)
        self.assertEqual(res_83["uncheckpointed_steps"], 1)
        self.assertEqual(res_83["drift_status"], DriftStatus.NOMINAL)

    # -------------------------------------------------------------------------
    # 3. Fase 2C: Controle de Trajetória e Guardrails
    # -------------------------------------------------------------------------
    def test_path_guardrails_unauthorized_access(self):
        plan_dict = {
            "goal": "Isolamento de diretorios autorizados",
            "acceptance_criteria": ["Restricao estrita aos paths declarados"],
            "allowed_paths": [str(self.src_dir), str(self.docs_dir)],
            "budget": {"max_steps": 20, "max_uncheckpointed_steps": 10},
            "mandatory_landing_checks": [],
        }
        task = self.store.new(self.project, "Guarda de caminhos", flight_plan=plan_dict)
        task_id = task["id"]

        # Step inside allowed path (src/app.py) -> succeeds
        allowed_file = self.src_dir / "app.py"
        res = self.store.record_step(task_id, "write_file", details="src file", paths=[str(allowed_file)])
        self.assertEqual(res["drift_status"], DriftStatus.NOMINAL)

        # Step targeting an outside path -> BLOCKED with UnauthorizedPathError
        unauthorized = Path("C:/Windows/System32/calc.exe") if os.name == "nt" else Path("/etc/shadow")
        with self.assertRaises(UnauthorizedPathError) as ctx:
            self.store.record_step(task_id, "access_forbidden", details="bad file", paths=[str(unauthorized)])
        self.assertIn("Desvio de rota", str(ctx.exception))

        # Verify drift event recorded
        summary = self.store.telemetry_summary(task_id)
        self.assertEqual(summary["drift_status"], DriftStatus.BLOCKED)
        path_events = [e for e in summary["recent_events"] if e["event_type"] == DeviationType.PATH_VIOLATION]
        self.assertEqual(len(path_events), 1)

    def test_budget_exceeded_guardrail(self):
        plan_dict = {
            "goal": "Limite orcamentario de passos",
            "acceptance_criteria": ["Orcamento respeitado"],
            "allowed_paths": [str(self.project)],
            "budget": {"max_steps": 3, "max_uncheckpointed_steps": 10},
            "mandatory_landing_checks": [],
        }
        task = self.store.new(self.project, "Tarefa com orcamento curto", flight_plan=plan_dict)
        task_id = task["id"]

        # Run 3 steps
        for i in range(3):
            self.store.record_step(task_id, f"step_{i}")

        # Step 4 exceeds max_steps=3 -> BudgetExceededError on clearance check
        with self.assertRaises(BudgetExceededError) as ctx:
            self.store.mission_control.check_clearance(task_id)
        self.assertIn("Orcamento de passos excedido", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 4. Fase 2D: Verificação de Pouso (Landing Verification)
    # -------------------------------------------------------------------------
    def test_landing_gate_blocks_complete_even_if_exit_code_zero(self):
        """Worker exit code 0 does NOT permit completing mission if mandatory landing checks fail."""
        plan_dict = {
            "goal": "Verificacao de pouso rigorosa",
            "acceptance_criteria": ["Testes unitarios verdes", "Auditoria de seguranca"],
            "allowed_paths": [str(self.project)],
            "budget": {"max_steps": 30, "max_uncheckpointed_steps": 10},
            "mandatory_landing_checks": ["unit_tests_pass", "security_audit_pass"],
        }
        task = self.store.new(self.project, "Tarefa com pouso validado", flight_plan=plan_dict)
        task_id = task["id"]

        # Worker runs and finishes with exit code 0
        run_id, _, _ = self.store.reserve(task_id, "code", "fazer codigo", 5)
        self.store.transition_run(run_id, to_status=RunState.STARTING, pid=1234, process_create_time=100)
        self.store.transition_run(run_id, to_status=RunState.RUNNING)
        self.store.transition_run(run_id, to_status=RunState.COMPLETE, exit_code=0)

        # Attempt to mark task as COMPLETE without recorded landing checks:
        with self.assertRaises(LandingVerificationError) as ctx:
            self.store.checkpoint(task_id, revision=1, patch={"summary": "Terminado"}, status="complete")
        self.assertIn("Pouso bloqueado deterministico", str(ctx.exception))
        self.assertIn("unit_tests_pass (status: not_recorded)", str(ctx.exception))
        self.assertIn("security_audit_pass (status: not_recorded)", str(ctx.exception))

        # Task remains active
        self.assertEqual(self.store.task(task_id)["status"], "active")

        # Record check 1 passed
        self.store.record_landing_check(task_id, "unit_tests_pass", status="passed", evidence="44/44 testes aprovados")

        # Still blocked by missing check 2
        with self.assertRaises(LandingVerificationError) as ctx:
            self.store.checkpoint(task_id, revision=1, patch={"summary": "Tentando pousar"}, status="complete")
        self.assertIn("security_audit_pass (status: not_recorded)", str(ctx.exception))

        # Record check 2 as FAILED
        self.store.record_landing_check(task_id, "security_audit_pass", status="failed", evidence="Falha de seguranca detectada")
        with self.assertRaises(LandingVerificationError) as ctx:
            self.store.checkpoint(task_id, revision=1, patch={"summary": "Tentando pousar"}, status="complete")
        self.assertIn("security_audit_pass (status: failed)", str(ctx.exception))

        # Remediate and record check 2 as PASSED
        self.store.record_landing_check(task_id, "security_audit_pass", status="passed", evidence="Vulnerabilidade mitigada")

        # Now verify_landing returns True
        passed, unmet = self.store.verify_landing(task_id)
        self.assertTrue(passed)
        self.assertEqual(unmet, [])

        # Checkpoint with status COMPLETE now SUCCEEDS!
        completed_task = self.store.checkpoint(task_id, revision=1, patch={"summary": "Pouso validado com sucesso"}, status="complete")
        self.assertEqual(completed_task["status"], "complete")
        self.assertEqual(completed_task["revision"], 2)

    def test_landing_check_requires_evidence_for_passed(self):
        task = self.store.new(self.project, "Validacao de evidencia")
        task_id = task["id"]

        with self.assertRaises(ValueError) as ctx:
            self.store.record_landing_check(task_id, "check_1", status="passed", evidence=None)
        self.assertIn("Evidencia obrigatoria", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            self.store.record_landing_check(task_id, "check_1", status="passed", evidence="   ")
        self.assertIn("Evidencia obrigatoria", str(ctx.exception))

    # -------------------------------------------------------------------------
    # 5. CLI Commands Integration
    # -------------------------------------------------------------------------
    def test_cli_flight_plan_and_landing_checks(self):
        task = self.store.new(self.project, "Tarefa CLI Test")
        task_id = task["id"]

        plan_file = self.root / "plan.json"
        plan_file.write_text(json.dumps({
            "goal": "Testar CLI Mission Control",
            "acceptance_criteria": ["CLI funcionando"],
            "allowed_paths": [str(self.project)],
            "budget": {"max_steps": 20, "max_uncheckpointed_steps": 10},
            "mandatory_landing_checks": ["cli_ok"],
        }), encoding="utf-8")

        # CLI: flight-plan --task ID --set plan.json
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", ["cli.py", "--root", str(self.root), "flight-plan", "--task", task_id, "--set", str(plan_file)]):
            cli.main()
            mock_emit.assert_called()
            self.assertEqual(mock_emit.call_args[0][0]["task_id"], task_id)

        # CLI: landing-check --task ID --check cli_ok --status passed --evidence "CLI validada"
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", ["cli.py", "--root", str(self.root), "landing-check", "--task", task_id, "--check", "cli_ok", "--status", "passed", "--evidence", "CLI validada"]):
            cli.main()
            mock_emit.assert_called()
            self.assertEqual(mock_emit.call_args[0][0]["status"], "passed")

        # CLI: landing-check --task ID --verify
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", ["cli.py", "--root", str(self.root), "landing-check", "--task", task_id, "--verify"]):
            cli.main()
            mock_emit.assert_called()
            self.assertTrue(mock_emit.call_args[0][0]["passed"])
            self.assertEqual(mock_emit.call_args[0][0]["unmet"], [])

        # CLI: step --task ID --action test_step
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", ["cli.py", "--root", str(self.root), "step", "--task", task_id, "--action", "test_step"]):
            cli.main()
            mock_emit.assert_called()
            self.assertEqual(mock_emit.call_args[0][0]["action"], "test_step")


if __name__ == "__main__":
    unittest.main()
