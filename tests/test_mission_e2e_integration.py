from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

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
)
from ai_coordinator.streaming import (
    StreamingScrubber,
    SafeStreamConsumer,
)
from ai_coordinator.process import (
    ProcessSupervisor,
    safe_process_alive,
)


class MissionE2EIntegrationTests(unittest.TestCase):
    """End-to-End integration test covering the entire mission lifecycle:
    1. FlightPlan creation and pre-flight clearance
    2. Supervised worker execution with streaming log scrubbing
    3. Prolonged radio silence detection and autonomous blocking
    4. Supervisor progress checkpoint and nominal unblocking
    5. Safe cancellation and conservative reconciliation
    6. Mandatory landing gate verification (blocking on exit 0 without evidence)
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "coordinator_data"
        self.project = Path(self.temp_dir.name) / "target_project"
        self.project.mkdir()
        self.src_dir = self.project / "src"
        self.src_dir.mkdir()
        self.docs_dir = self.project / "docs"
        self.docs_dir.mkdir()
        self.store = Store(self.root)

    def tearDown(self):
        self.store.db.close()
        self.temp_dir.cleanup()

    def test_full_mission_e2e_operational_flight_lifecycle(self):
        # ---------------------------------------------------------------------
        # 1. Criacao da missao com FlightPlan rigoroso e liberacao de pre-voo
        # ---------------------------------------------------------------------
        plan = FlightPlan(
            task_id="task-e2e-001",
            goal="Construir pipeline com isolamento, telemetria e barreira de pouso",
            acceptance_criteria=[
                "Logs sem vazamento de tokens",
                "Bloqueio deterministico por silencio de radio",
                "Cancelamento seguro sem zumbis",
                "Verificacao obrigatoria de pouso",
            ],
            allowed_paths=[str(self.src_dir), str(self.docs_dir)],
            budget=FlightBudget(
                max_steps=50,
                max_uncheckpointed_steps=5,
                max_worker_calls=5,
                max_elapsed_seconds=1800,
            ),
            mandatory_landing_checks=["unit_tests", "security_audit"],
        )
        plan.validate()

        task = self.store.new(
            self.project,
            "Missao E2E Completa",
            flight_plan=plan.to_dict(),
        )
        task_id = task["id"]
        self.assertEqual(task["status"], "active")

        # Pre-flight clearance concedida inicialmente sem excecoes
        self.store.mission_control.check_clearance(task_id)

        # ---------------------------------------------------------------------
        # 2. Execucao supervisionada com captura de fluxo e sanitizacao de segredos
        # ---------------------------------------------------------------------
        run_id_1, folder_1, remaining_1 = self.store.reserve(
            task_id,
            "code",
            "Processar geracao de tokens",
            5,
        )
        self.assertTrue(run_id_1)
        self.assertTrue(folder_1.exists())

        # Worker emite logs e token secreto dividido
        worker_code = """import sys, time
sys.stdout.write("Etapa 1 iniciada com sucesso\\n")
sys.stdout.write("Token gerado: sk-secrettoken9876543210abcdef\\n")
sys.stdout.write("Etapa 1 finalizada\\n")
sys.stdout.flush()
"""
        proc_1 = ProcessSupervisor.spawn(
            [sys.executable, "-c", worker_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            role="worker",
        )
        self.assertIsNotNone(proc_1.pid)
        self.assertGreater(proc_1.pid, 0)

        # Transicao atomica para STARTING e RUNNING com identidade do SO
        self.store.transition_run(
            run_id_1,
            to_status=RunState.STARTING,
            expected_status=RunState.RESERVED,
            pid=proc_1.pid,
            process_create_time=proc_1.identity.create_time_ns,
        )
        self.store.transition_run(
            run_id_1,
            to_status=RunState.RUNNING,
            expected_status=RunState.STARTING,
        )

        # Consumo seguro dos fluxos (stdout / stderr) com scrubber
        out_log = folder_1 / "stdout.log"
        err_log = folder_1 / "stderr.log"
        consumer = SafeStreamConsumer(stdout_path=out_log, stderr_path=err_log)
        consume_res = consumer.consume(proc_1.proc, timeout_seconds=10.0)
        proc_1.proc.stdout.close()
        proc_1.proc.stderr.close()

        self.assertEqual(consume_res.returncode, 0)
        self.store.confirm_exit(run_id_1, exit_code=0, output_path=str(out_log))

        # Confirmar que o segredo foi higienizado nos logs em disco
        scrubbed_content = out_log.read_text(encoding="utf-8")
        self.assertIn("[REDACTED]", scrubbed_content)
        self.assertNotIn("sk-secrettoken9876543210abcdef", scrubbed_content)
        self.assertIn("Etapa 1 finalizada", scrubbed_content)

        # ---------------------------------------------------------------------
        # 3. Disparo de silencio de radio prolongado e bloqueio autonomo
        # ---------------------------------------------------------------------
        # O passo 1 foi a propria reserva (reserve_code, uncheckpointed=1).
        # Passos adicionais 2 a 4 dentro do caminho autorizado (src/) -> nominais
        for step_idx in range(1, 4):
            target_file = self.src_dir / f"module_{step_idx}.py"
            step_res = self.store.record_step(
                task_id,
                f"edit_module_{step_idx}",
                details=f"Alteracao no modulo {step_idx}",
                paths=[str(target_file)],
            )
            self.assertEqual(step_res["step_count"], step_idx + 1)
            self.assertEqual(step_res["uncheckpointed_steps"], step_idx + 1)
            self.assertEqual(step_res["drift_status"], DriftStatus.NOMINAL)

        # Pre-flight clearance ainda concedida com 4 passos sem checkpoint
        self.store.mission_control.check_clearance(task_id)

        # Passo 5 no total: atinge exatamente o limite de max_uncheckpointed_steps=5
        res_5 = self.store.record_step(
            task_id,
            "edit_module_4",
            details="Quinto passo consecutivo sem checkpoint",
            paths=[str(self.src_dir / "module_4.py")],
        )
        self.assertEqual(res_5["step_count"], 5)
        self.assertEqual(res_5["uncheckpointed_steps"], 5)
        self.assertEqual(res_5["drift_status"], DriftStatus.BLOCKED)

        # Telemetria registra o evento de silencio de radio prolongado
        summary = self.store.telemetry_summary(task_id)
        silence_events = [e for e in summary["recent_events"] if e["event_type"] == DeviationType.PROLONGED_SILENCE]
        self.assertEqual(len(silence_events), 1)
        self.assertEqual(silence_events[0]["severity"], "warning")

        # Bloqueio deterministico: pre-flight clearance e reservas sao barradas
        with self.assertRaises(RadioSilenceError) as ctx:
            self.store.mission_control.check_clearance(task_id)
        self.assertIn("Silencio de radio prolongado", str(ctx.exception))

        with self.assertRaises(RadioSilenceError) as ctx:
            self.store.reserve(task_id, "code", "tentativa bloqueada", 5)
        self.assertIn("Silencio de radio prolongado", str(ctx.exception))

        # ---------------------------------------------------------------------
        # 4. Checkpoint formal do supervisor e desbloqueio nominal
        # ---------------------------------------------------------------------
        updated_task = self.store.checkpoint(
            task_id,
            revision=1,
            patch={
                "summary": "Ponto de controle apos 5 passos de execucao",
                "decisions": ["Arquitetura validada nos modulos 1 a 5"],
            },
        )
        self.assertEqual(updated_task["revision"], 2)

        # Telemetria reiniciada para uncheckpointed=0 e status NOMINAL
        summary_after = self.store.telemetry_summary(task_id)
        self.assertEqual(summary_after["uncheckpointed_steps"], 0)
        self.assertEqual(summary_after["drift_status"], DriftStatus.NOMINAL)

        # Pre-flight clearance concedida novamente sem excecoes
        self.store.mission_control.check_clearance(task_id)

        # ---------------------------------------------------------------------
        # 5. Cancelamento seguro e reconciliacao conservadora
        # ---------------------------------------------------------------------
        run_id_2, folder_2, _ = self.store.reserve(
            task_id,
            "code",
            "Execucao que sera cancelada",
            5,
        )
        proc_2 = ProcessSupervisor.spawn(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            role="worker",
        )
        self.store.transition_run(
            run_id_2,
            to_status=RunState.STARTING,
            expected_status=RunState.RESERVED,
            pid=proc_2.pid,
            process_create_time=proc_2.identity.create_time_ns,
        )
        self.store.transition_run(
            run_id_2,
            to_status=RunState.RUNNING,
            expected_status=RunState.STARTING,
        )

        # Operador solicita cancelamento formal
        cancel_info = self.store.request_cancel(run_id_2)
        self.assertEqual(cancel_info["status"], RunState.CANCEL_REQUESTED)

        # Encerramento seguro do processo sem deixar zumbis
        term_res = proc_2.terminate(timeout_seconds=2.0)
        self.assertTrue(term_res.success)
        self.assertFalse(proc_2.is_alive())

        # Reconciliacao conservadora detecta processo encerrado apos pedido de cancelamento
        reconciled = self.store.reconcile(task_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["id"], run_id_2)
        self.assertEqual(reconciled[0]["status"], RunState.CANCELLED)
        self.assertIsNotNone(reconciled[0]["ended"])

        # ---------------------------------------------------------------------
        # 6. Barreira de pouso (Landing Gate): bloqueia conclusao ate evidencias
        # ---------------------------------------------------------------------
        # Mesmo com o worker 1 encerrado com exit code 0, tentar concluir a missao
        # sem evidencias dos checks obrigatorios DEVE FALHAR deterministicamente!
        with self.assertRaises(LandingVerificationError) as ctx:
            self.store.checkpoint(
                task_id,
                revision=2,
                patch={"summary": "Tentativa prematura de concluir missao"},
                status="complete",
            )
        self.assertIn("Pouso bloqueado deterministico", str(ctx.exception))
        self.assertIn("unit_tests (status: not_recorded)", str(ctx.exception))
        self.assertIn("security_audit (status: not_recorded)", str(ctx.exception))

        # A tarefa permanece ativa
        self.assertEqual(self.store.task(task_id)["status"], "active")

        # Registrar aprovacao do check 1 (unit_tests) com evidencia
        self.store.record_landing_check(
            task_id,
            check_name="unit_tests",
            status="passed",
            evidence="55/55 testes unitarios aprovados na matriz de CI",
        )

        # Tentar concluir novamente: ainda bloqueado pelo check 2 (security_audit)
        with self.assertRaises(LandingVerificationError) as ctx:
            self.store.checkpoint(
                task_id,
                revision=2,
                patch={"summary": "Tentativa ainda incompleta"},
                status="complete",
            )
        self.assertIn("security_audit (status: not_recorded)", str(ctx.exception))

        # Registrar falha temporaria no check 2
        self.store.record_landing_check(
            task_id,
            check_name="security_audit",
            status="failed",
            evidence="Alerta de vulnerabilidade na checagem de permissoes",
        )

        with self.assertRaises(LandingVerificationError) as ctx:
            self.store.checkpoint(
                task_id,
                revision=2,
                patch={"summary": "Tentativa com check reprovado"},
                status="complete",
            )
        self.assertIn("security_audit (status: failed)", str(ctx.exception))

        # Corrigir e registrar check 2 aprovado com evidencia formal
        self.store.record_landing_check(
            task_id,
            check_name="security_audit",
            status="passed",
            evidence="Correcao aplicada; auditoria limpa sem apontamentos",
        )

        # Barreira de pouso agora confirma liberacao total
        passed, unmet = self.store.verify_landing(task_id)
        self.assertTrue(passed)
        self.assertEqual(unmet, [])

        # Conclusao formal da missao agora e aceita
        completed_task = self.store.checkpoint(
            task_id,
            revision=2,
            patch={"summary": "Missao concluida com sucesso e todos os checks validados"},
            status="complete",
        )
        self.assertEqual(completed_task["status"], "complete")
        self.assertEqual(completed_task["revision"], 3)

        # Verificacao do espelho no disco (state.json)
        state_path = self.root / "tasks" / task_id / "state.json"
        self.assertTrue(state_path.exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "complete")
        self.assertIsNotNone(state.get("flight_plan"))
        self.assertEqual(len(state.get("landing_checks", [])), 2)

    def test_e2e_82_steps_radio_silence_threshold(self):
        """Validacao end-to-end do cenario exato de 82 passos sem checkpoint."""
        plan = FlightPlan(
            task_id="task-82-steps",
            goal="Executar analise profunda de 82 passos",
            acceptance_criteria=["Parada exata no passo 82 sem violacao previa"],
            allowed_paths=[str(self.project)],
            budget=FlightBudget(
                max_steps=120,
                max_uncheckpointed_steps=82,
                max_worker_calls=15,
            ),
            mandatory_landing_checks=[],
        )
        task = self.store.new(self.project, "Analise de 82 passos", flight_plan=plan.to_dict())
        task_id = task["id"]

        # Executa 81 passos
        for i in range(1, 82):
            res = self.store.record_step(task_id, f"step_{i}", details=f"Execucao do passo {i}")
            self.assertEqual(res["drift_status"], DriftStatus.NOMINAL)

        # No passo 81 a liberacao ainda esta ativa
        self.store.mission_control.check_clearance(task_id)

        # Passo 82 atinge exatamente o teto
        res_82 = self.store.record_step(task_id, "step_82", details="Atingido passo 82")
        self.assertEqual(res_82["drift_status"], DriftStatus.BLOCKED)

        # Bloqueio confirmado
        with self.assertRaises(RadioSilenceError):
            self.store.mission_control.check_clearance(task_id)

        # Checkpoint restaura a normalidade
        self.store.checkpoint(task_id, revision=1, patch={"summary": "Checkpoint no passo 82"})
        self.store.mission_control.check_clearance(task_id)

    def test_e2e_path_violation_and_budget_guardrails(self):
        """Validacao end-to-end de desvios de rota (paths nao autorizados) e orcamento de passos."""
        plan = FlightPlan(
            task_id="task-guardrails",
            goal="Testar protecao de diretorios e teto orcamentario",
            acceptance_criteria=["Bloqueio em caminhos estranhos", "Bloqueio ao esgotar orcamento"],
            allowed_paths=[str(self.src_dir)],
            budget=FlightBudget(max_steps=3, max_uncheckpointed_steps=10),
            mandatory_landing_checks=[],
        )
        task = self.store.new(self.project, "Guarda de rota e orcamento", flight_plan=plan.to_dict())
        task_id = task["id"]

        # Acesso dentro do permitido
        res1 = self.store.record_step(task_id, "touch_src", paths=[str(self.src_dir / "ok.py")])
        self.assertEqual(res1["drift_status"], DriftStatus.NOMINAL)

        # Tentativa de acesso fora do caminho permitido dispara UnauthorizedPathError imediatamente
        forbidden_file = Path("C:/Windows/notepad.exe") if os.name == "nt" else Path("/etc/passwd")
        with self.assertRaises(UnauthorizedPathError):
            self.store.record_step(task_id, "touch_forbidden", paths=[str(forbidden_file)])

        # Estado da tarefa entra em BLOCKED
        summary = self.store.telemetry_summary(task_id)
        self.assertEqual(summary["drift_status"], DriftStatus.BLOCKED)

        # Re-estabelece checkpoint para limpar desvio de rota
        self.store.checkpoint(task_id, revision=1, patch={"summary": "Resolvido desvio de rota"})
        self.assertEqual(self.store.telemetry_summary(task_id)["drift_status"], DriftStatus.NOMINAL)

        # Executa passos ate esgotar orcamento (max_steps=3, ja foram feitos 2 passos)
        self.store.record_step(task_id, "step_3", paths=[str(self.src_dir / "part2.py")])
        
        # Proxima liberacao de pre-voo deve falhar por orcamento excedido (3 >= 3)
        with self.assertRaises(BudgetExceededError):
            self.store.mission_control.check_clearance(task_id)


if __name__ == "__main__":
    unittest.main()
