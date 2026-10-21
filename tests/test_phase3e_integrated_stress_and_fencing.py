"""Tests for Phase 3E.1: Integrated Mission, Stress Fencing, Evidence Authenticity, and Failure Recovery."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ai_coordinator import (
    Store,
    FlightPlan,
    FlightBudget,
    RunState,
    ACTIVE_RUN_STATES,
    UNCERTAIN_RUN_STATES,
    TERMINAL_RUN_STATES,
    DagError,
    CycleDetectedError,
    StaleRevisionError,
    ActiveWorkerFencingError,
    ProposalNotApprovedError,
    UnauthorizedOverrideError,
    InvariantVerificationError,
    PlanNode,
    PlanEdge,
    PlanRevision,
    TrajectoryProposal,
    TrajectoryReconstructor,
    TrajectoryStatus,
    TrajectoryPredictor,
    TelemetrySnapshot,
    InvariantDefinition,
    InvariantEvidence,
    InvariantEvaluation,
    InvariantStatus,
    InvariantOracle,
    compute_sha256,
    generate_verification_token,
    ResourceState,
    ResourceType,
    ResourceAction,
    ResourceLedgerError,
    ResourceConflictError,
    UnresolvedResourceError,
    ExperienceStore,
    ExperienceRecord,
    ExperienceCategory,
    ExperienceOutcome,
    ExperienceVisibility,
    ExperienceAuthor,
    InsightEngine,
    InsightPresentation,
    splice_alternative_route,
)


class Phase3E1IntegratedStressAndFencingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "coordinator_data"
        self.proj_dir = Path(self.tmp.name) / "primary_project"
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root)
        self.task_id = "task-3e1-mission"

        self.store.new(self.proj_dir, "Missao Integrada 3E.1")
        self.store.db.execute(
            "UPDATE tasks SET id=? WHERE project=?",
            (self.task_id, str(self.proj_dir.resolve())),
        )
        self.store.db.commit()

    def tearDown(self):
        try:
            self.store.db.close()
        except Exception:
            pass
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Scenario 1: Missão Integrada Completa com Desvio, Reconstrução e Recuperação
    # -------------------------------------------------------------------------
    def test_end_to_end_mission_with_deviation_reconstruction_and_recovery(self):
        """Validates the full 7-step lifecycle:
        1. Experience retrieval with RBAC
        2. DAG execution until real deviation flagged by TrajectoryPredictor
        3. Alternative generation via Insight Engine and reconstructor
        4. DAG proposal registration without automatic mutation
        5. Fencing checks (Oracle veto, uncertain resources, lingering workers)
        6. Resolution of blockers, formal approval, and activation verifying Join Point
        7. Storing outcome in Experience Store to guide future decisions.
        """
        # Step 1: Seed and retrieve previous solution in ExperienceStore respecting RBAC
        self.store.record_experience(
            project_id=str(self.proj_dir),
            problem_pattern="timeout na etapa de compilacao rust",
            solution_pattern="adicionar flag incremental e cache local sccache",
            category=ExperienceCategory.TOOL_FIX,
            outcome=ExperienceOutcome.SUCCESS,
            author=ExperienceAuthor.AGENT,
            visibility=ExperienceVisibility.PROJECT_SHARED,
        )
        # Agent authorized for primary_project can retrieve it
        priors = self.store.find_experiences(
            project_id=str(self.proj_dir),
            query_text="timeout compilacao",
            caller_role="agent",
            caller_authorized_projects=[str(self.proj_dir)],
        )
        self.assertEqual(len(priors), 1)
        self.assertIn("sccache", priors[0].solution_pattern)

        # Step 2: Establish initial DAG Revision 1 (build -> test -> deploy)
        initial_nodes = [
            {"node_id": "build", "title": "Compilar Codigo", "status": "completed"},
            {"node_id": "test", "title": "Executar Testes", "status": "in_progress"},
            {"node_id": "deploy", "title": "Deploy Producao", "status": "pending"},
        ]
        initial_edges = [
            {"from_node": "build", "to_node": "test"},
            {"from_node": "test", "to_node": "deploy"},
        ]
        r1 = self.store.create_plan_revision(
            self.task_id,
            nodes=initial_nodes,
            edges=initial_edges,
            summary="R1: Pipeline padrao inicial",
        )
        self.assertEqual(r1.revision, 1)

        # Register mandatory invariant for the mission
        self.store.create_invariant(
            task_id=self.task_id,
            description="Todos os testes de integracao aprovados com codigo 0",
            invariant_id="integration_suite",
            required=True,
            verifier_type="trusted_test_runner",
        )

        # Step 2b: Trajectory Predictor detects real risk (budget ceiling warning)
        self.store.db.execute("UPDATE tasks SET step_count=75 WHERE id=?", (self.task_id,))
        budget = FlightBudget(max_steps=82, max_uncheckpointed_steps=10)
        flight_plan = FlightPlan(
            task_id=self.task_id,
            goal="Pipeline com orcamento restrito",
            acceptance_criteria=["Conclusao com orcamento"],
            allowed_paths=[str(self.proj_dir)],
            budget=budget,
        )
        predictor = TrajectoryPredictor(self.store)
        prediction = predictor.predict(self.task_id, flight_plan=flight_plan)
        self.assertEqual(prediction.status, TrajectoryStatus.AT_RISK)
        self.assertIn("excedem o orcamento maximo", prediction.reason)

        # Step 3: Produce alternative route via InsightEngine and reconstructor
        # Explore alternatives given the budget/timeout risk
        insight = self.store.insight_engine.explore(
            project_id=str(self.proj_dir),
            topic_or_goal="Otimizacao de pipeline sob restricao severa de passos",
            task_id=self.task_id,
            presentation=InsightPresentation.CONTEXTUAL,
        )
        self.assertIsNotNone(insight)

        # Reconstruct alternative route replacing 'test' with 'test_fast' and reconnecting to 'deploy'
        reconstructor = TrajectoryReconstructor(self.store)
        preview = reconstructor.preview_alternative_route(
            task_id=self.task_id,
            base_revision=1,
            failed_node_id="test",
            replacement_nodes=[PlanNode(node_id="test_fast", title="Testes Otimizados em Paralelo")],
            reconnection_node_id="deploy",
        )
        self.assertTrue(preview.is_valid_dag)
        self.assertIn("deploy", preview.reconnection_points)

        # Step 4: Propose trajectory in DAG without automatic activation
        proposal = reconstructor.propose_alternative_route(
            task_id=self.task_id,
            base_revision=1,
            failed_node_id="test",
            replacement_nodes=[PlanNode(node_id="test_fast", title="Testes Otimizados em Paralelo")],
            reconnection_node_id="deploy",
            reason="Rota alternativa com testes paralelos para evitar estouro de passos",
        )
        self.assertEqual(proposal.status, "proposed")
        # Ensure active revision is still 1!
        self.assertEqual(self.store.get_plan_revision(self.task_id).revision, 1)

        # Step 5: Test Fencing and Gates
        # 5a: Unapproved proposal activation rejected
        with self.assertRaises(ProposalNotApprovedError):
            self.store.activate_trajectory_proposal(proposal.id)

        # Approve proposal formally
        self.store.resolve_trajectory_proposal(proposal.id, "approved")

        # 5b: Gate blocked by missing/unverified Invariant Oracle
        with self.assertRaises(Exception) as ctx_inv:
            self.store.activate_trajectory_proposal(proposal.id)
        self.assertIn("oraculo de invariantes", str(ctx_inv.exception).lower())

        # Record a failing invariant evaluation -> still blocked
        fail_eval = InvariantEvaluation(
            id="eval-fail-1",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="integration_suite",
            status=InvariantStatus.FAIL,
            verified=True,
            details="Falha detectada nos testes",
        )
        self.store.record_invariant_evaluation(fail_eval)
        with self.assertRaises(Exception) as ctx_fail:
            self.store.activate_trajectory_proposal(proposal.id)
        self.assertIn("bloqueada por oraculo de invariantes", str(ctx_fail.exception).lower())

        # 5c: Record a passing invariant evaluation
        pass_eval = InvariantEvaluation(
            id="eval-pass-1",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="integration_suite",
            status=InvariantStatus.PASS,
            verified=True,
            details="Todos os testes aprovados com sucesso",
        )
        self.store.record_invariant_evaluation(pass_eval)

        # 5d: Gate blocked by resource in 'uncertain' state in Resource Ledger
        test_file = self.proj_dir / "artifact.txt"
        test_file.write_text("conteudo inicial", encoding="utf-8")
        rec = self.store.resource_ledger.record_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(test_file),
            action=ResourceAction.MODIFY,
            idempotency_key="intent-artifact-1",
        )
        # Transition resource directly to UNCERTAIN to simulate crash/unconfirmed write
        self.store.db.execute(
            "UPDATE resource_ledger SET status=? WHERE id=?",
            (ResourceState.UNCERTAIN, rec.id),
        )
        with self.assertRaises(UnresolvedResourceError):
            self.store.activate_trajectory_proposal(proposal.id)

        # Compensate the uncertain resource using its automatic backup
        test_file.write_text("conteudo corrompido durante desvio", encoding="utf-8")
        comp_rec = self.store.resource_ledger.compensate(rec.id)
        self.assertEqual(comp_rec.status, ResourceState.COMPENSATED)
        self.assertEqual(test_file.read_text(encoding="utf-8"), "conteudo inicial")

        # 5e: Gate blocked by lingering worker in 'identity_unknown'
        run_id, _, _ = self.store.reserve(self.task_id, "code", "worker prompt", 5)
        self.store.transition_run(run_id, to_status=RunState.STARTING, pid=7777, process_create_time=999)
        self.store.transition_run(run_id, to_status=RunState.IDENTITY_UNKNOWN, error="OS lookup failed")

        with self.assertRaises(ActiveWorkerFencingError) as ctx_worker:
            self.store.activate_trajectory_proposal(proposal.id)
        self.assertIn("identidade incerta", str(ctx_worker.exception).lower())

        # Step 6: Resolve lingering worker safely by operator reconciliation to INTERRUPTED
        self.store.transition_run(run_id, to_status=RunState.INTERRUPTED, error="Processo confirmado encerrado")

        # Now activation MUST succeed!
        new_rev = self.store.activate_trajectory_proposal(proposal.id)
        self.assertEqual(new_rev.revision, 2)
        self.assertEqual(new_rev.parent_revision, 1)
        self.assertIn("test_fast", new_rev.nodes)

        # Verify Join Point / Reconnection Node connectivity
        self.assertIn("deploy", new_rev.nodes)
        edges = [(e.from_node, e.to_node) for e in new_rev.edges]
        self.assertIn(("test_fast", "deploy"), edges)

        # Step 7: Record final outcome in ExperienceStore to guide future missions
        self.store.record_experience(
            project_id=str(self.proj_dir),
            task_id=self.task_id,
            problem_pattern="Risco de estouro de passos em pipeline com testes seriais",
            solution_pattern="Substituir por testes paralelos otimizados com reconciliacao limpa",
            category=ExperienceCategory.ARCHITECTURE_DECISION,
            outcome=ExperienceOutcome.SUCCESS,
            author=ExperienceAuthor.AGENT,
            visibility=ExperienceVisibility.PROJECT_SHARED,
        )
        resolved_priors = self.store.find_experiences(
            project_id=str(self.proj_dir),
            query_text="estouro passos",
            category=ExperienceCategory.ARCHITECTURE_DECISION,
            caller_role="agent",
            caller_authorized_projects=[str(self.proj_dir)],
        )
        self.assertEqual(len(resolved_priors), 1)
        self.assertEqual(resolved_priors[0].outcome, ExperienceOutcome.SUCCESS)
        self.assertIn("testes paralelos", resolved_priors[0].solution_pattern)

    # -------------------------------------------------------------------------
    # Scenario 2: Falha Crítica 1 — Queda após Escrita Física e antes do Commit
    # -------------------------------------------------------------------------
    def test_failure_after_physical_write_before_commit_reconciliation(self):
        """Simulates crash after physical file mutation before commit:
        Resource is uncertain -> activation blocked -> rollback restores file -> unblocked.
        """
        f = self.proj_dir / "critical_config.json"
        f.write_text('{"mode": "v1"}', encoding="utf-8")
        original_hash = compute_sha256(f)

        # 1. Record intent to modify
        rec = self.store.resource_ledger.record_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(f),
            action=ResourceAction.MODIFY,
            idempotency_key="intent-cfg-mod",
        )
        self.assertEqual(rec.pre_hash, original_hash)
        self.assertIn("backup_path", rec.metadata)

        # 2. Worker mutates file on disk, but process crashes before calling mark_applied()
        f.write_text('{"mode": "v2_corrupted"}', encoding="utf-8")

        # 3. Crash recovery reconciliation runs
        uncertain_records = self.store.resource_ledger.reconcile_uncertain(self.task_id)
        self.assertEqual(len(uncertain_records), 1)
        self.assertEqual(uncertain_records[0].status, ResourceState.UNCERTAIN)

        # 4. Attempting to activate any plan proposal must be BLOCKED
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="Plano A")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Adicionar B",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        with self.assertRaises(UnresolvedResourceError) as ctx:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("estado incerto", str(ctx.exception))

        # 5. Compensate restores the physical file to exact v1 content and hash
        comp = self.store.resource_ledger.compensate(rec.id)
        self.assertEqual(comp.status, ResourceState.COMPENSATED)
        self.assertEqual(f.read_text(encoding="utf-8"), '{"mode": "v1"}')
        self.assertEqual(compute_sha256(f), original_hash)

        # 6. Now proposal activation succeeds
        rev2 = self.store.activate_trajectory_proposal(prop.id)
        self.assertEqual(rev2.revision, 2)

    # -------------------------------------------------------------------------
    # Scenario 3: Falha Crítica 2 — Cancelamento Concorrente e Identidade Desconhecida
    # -------------------------------------------------------------------------
    def test_failure_during_cancellation_and_identity_unknown_fencing(self):
        """Validates that identity_unknown runs block activation, cannot be ignored,
        and transition cleanly to INTERRUPTED upon cancel/reconcile.
        """
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="Plano Cancel")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Proposta com cancelamento",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # Start a worker that loses identity during supervisor lookup
        run_id, _, _ = self.store.reserve(self.task_id, "code", "prompt", 5)
        self.store.transition_run(run_id, to_status=RunState.STARTING, pid=4321, process_create_time=123)
        self.store.transition_run(run_id, to_status=RunState.IDENTITY_UNKNOWN, error="Permissao negada no SO")

        # Ensure IDENTITY_UNKNOWN is NOT in TERMINAL_RUN_STATES
        self.assertNotIn(RunState.IDENTITY_UNKNOWN, TERMINAL_RUN_STATES)
        self.assertIn(RunState.IDENTITY_UNKNOWN, UNCERTAIN_RUN_STATES)

        # Activation strictly blocked
        with self.assertRaises(ActiveWorkerFencingError):
            self.store.activate_trajectory_proposal(prop.id)

        # Operator requests cancel on the unknown run
        self.store.request_cancel(run_id)
        # Supervisor confirms process is terminated, moving to INTERRUPTED
        self.store.transition_run(run_id, to_status=RunState.INTERRUPTED, error="Processo finalizado")

        # Activation now unblocked
        rev = self.store.activate_trajectory_proposal(prop.id)
        self.assertEqual(rev.revision, 2)

    # -------------------------------------------------------------------------
    # Scenario 4: Falha Crítica 3 — Falhas Durante Compensação Física
    # -------------------------------------------------------------------------
    def test_failure_during_physical_compensation(self):
        """Validates that compensation cannot advance to 'compensated' when:
        a) file modification has no backup available,
        b) file was modified by a third party after the original operation,
        and succeeds only when backup is verified.
        """
        test_file = self.proj_dir / "target_modify.txt"
        test_file.write_text("versao 1 original", encoding="utf-8")

        # 4a: Modify without backup -> must FAIL to compensate and NOT advance to compensated
        rec_no_bak = self.store.resource_ledger.record_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(test_file),
            action=ResourceAction.MODIFY,
            idempotency_key="intent-no-bak",
            metadata={},  # explicit empty metadata without backup
        )
        # Artificially remove backup_path if auto-created to simulate unbacked modification
        self.store.db.execute("UPDATE resource_ledger SET metadata='{}' WHERE id=?", (rec_no_bak.id,))
        test_file.write_text("versao 2 modificada", encoding="utf-8")

        with self.assertRaises(ResourceLedgerError) as ctx_no_bak:
            self.store.resource_ledger.compensate(rec_no_bak.id)
        self.assertIn("nenhum backup", str(ctx_no_bak.exception))

        # Check database: status MUST NOT be 'compensated'
        row = self.store.db.execute("SELECT status FROM resource_ledger WHERE id=?", (rec_no_bak.id,)).fetchone()
        self.assertNotEqual(row["status"], ResourceState.COMPENSATED)

        # 4b: Third party modification detection
        test_file_3p = self.proj_dir / "third_party.txt"
        test_file_3p.write_text("original 3p", encoding="utf-8")
        rec_3p = self.store.resource_ledger.record_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(test_file_3p),
            action=ResourceAction.MODIFY,
            idempotency_key="intent-3p",
        )
        # Mark applied with post_hash
        test_file_3p.write_text("worker modified", encoding="utf-8")
        applied_hash = compute_sha256(test_file_3p)
        self.store.resource_ledger.confirm_applied(rec_3p.id, post_hash=applied_hash)

        # Third party modifies file outside system knowledge
        test_file_3p.write_text("tampered by external actor", encoding="utf-8")

        with self.assertRaises(ResourceConflictError) as ctx_conflict:
            self.store.resource_ledger.compensate(rec_3p.id)
        self.assertIn("modificado por terceiro", str(ctx_conflict.exception))

        row_3p = self.store.db.execute("SELECT status FROM resource_ledger WHERE id=?", (rec_3p.id,)).fetchone()
        self.assertNotEqual(row_3p["status"], ResourceState.COMPENSATED)

        # 4c: Successful physical compensation with backup
        test_file_ok = self.proj_dir / "file_ok.txt"
        test_file_ok.write_text("ok versao original", encoding="utf-8")
        orig_hash = compute_sha256(test_file_ok)
        rec_ok = self.store.resource_ledger.record_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(test_file_ok),
            action=ResourceAction.MODIFY,
            idempotency_key="intent-ok",
        )
        test_file_ok.write_text("ok versao alterada", encoding="utf-8")
        self.store.resource_ledger.confirm_applied(rec_ok.id, post_hash=compute_sha256(test_file_ok))

        comp_ok = self.store.resource_ledger.compensate(rec_ok.id)
        self.assertEqual(comp_ok.status, ResourceState.COMPENSATED)
        self.assertEqual(test_file_ok.read_text(encoding="utf-8"), "ok versao original")
        self.assertEqual(compute_sha256(test_file_ok), orig_hash)

    # -------------------------------------------------------------------------
    # Scenario 5: Falha Crítica 4 — Falhas Entre Aprovação e Ativação
    # -------------------------------------------------------------------------
    def test_failure_between_proposal_approval_and_activation(self):
        """Validates that a proposal approved on revision 1 cannot be activated if:
        a) active revision advances in the meantime (StaleRevisionError),
        b) a new worker starts in the meantime (ActiveWorkerFencingError).
        """
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="Plano Base")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Proposta aprovada",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # 5a: Concurrent revision committed directly
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="Plano Concorrente R2")
        self.assertEqual(self.store.get_plan_revision(self.task_id).revision, 2)

        # Attempting activation with proposal based on revision 1 is rejected
        with self.assertRaises(StaleRevisionError) as ctx_stale:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("A proposta foi formulada sobre revisao 1, mas a ativa e 2", str(ctx_stale.exception))

        # 5b: New worker started between approval and activation
        prop2 = self.store.propose_trajectory(
            self.task_id,
            base_revision=2,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "c", "title": "C"}],
            proposed_edges=[{"from_node": "a", "to_node": "c"}],
            reason="Proposta baseada em R2",
        )
        self.store.resolve_trajectory_proposal(prop2.id, "approved")

        # A worker is reserved and starts running
        run_id, _, _ = self.store.reserve(self.task_id, "code", "worker", 5)
        self.store.transition_run(run_id, to_status=RunState.STARTING, pid=999, process_create_time=111)

        with self.assertRaises(ActiveWorkerFencingError):
            self.store.activate_trajectory_proposal(prop2.id)

        # Conclude worker safely (transition to running then exit)
        self.store.transition_run(run_id, to_status=RunState.RUNNING)
        self.store.confirm_exit(run_id, exit_code=0)
        rev3 = self.store.activate_trajectory_proposal(prop2.id)
        self.assertEqual(rev3.revision, 3)

    # -------------------------------------------------------------------------
    # Scenario 6: Autenticidade das Evidências e Bloqueio de Falsificação (3E.2)
    # -------------------------------------------------------------------------
    def test_evidence_authenticity_and_forgery_prevention(self):
        """Validates that:
        a) Unauthenticated textual claim with source='trusted_test_runner' is rejected when authenticity proof required,
        b) Forged verification token or forged secret key is rejected as UNKNOWN,
        c) Non-existent or failed run_id is rejected as UNKNOWN,
        d) Token without verified run_id in strict mode is rejected as UNKNOWN,
        e) Authentic token bound to task/revision/run_id + verified completed run are accepted as PASS.
        """
        oracle = InvariantOracle(run_lookup=self.store.get_run_for_task)

        inv_strict = InvariantDefinition(
            id="strict_security",
            task_id=self.task_id,
            description="Verificacao rigorosa de autenticidade",
            require_authenticity_proof=True,
        )

        # 6a: Pure textual self-declaration without proof token or run linkage -> UNKNOWN
        unverified_ev = InvariantEvidence(
            source="trusted_test_runner",
            exit_code=0,
        )
        res_unverified = oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv_strict,
            evidence=unverified_ev,
        )
        self.assertEqual(res_unverified.status, InvariantStatus.UNKNOWN)
        self.assertFalse(res_unverified.verified)
        self.assertIn("Evidencia rejeitada por falta de comprovacao formal vinculada", res_unverified.details)

        # 6b: Forged verification token -> UNKNOWN
        forged_ev = InvariantEvidence(
            source="trusted_test_runner",
            exit_code=0,
            verification_token="bad_token_12345",
        )
        res_forged = oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv_strict,
            evidence=forged_ev,
        )
        self.assertEqual(res_forged.status, InvariantStatus.UNKNOWN)
        self.assertFalse(res_forged.verified)
        self.assertIn("token criptografico HMAC-SHA256 invalido", res_forged.details)

        # 6c: Forged token calculated with an untrusted key (simulating rogue worker trying to forge HMAC) -> UNKNOWN
        forged_key_token = generate_verification_token(
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="strict_security",
            source="trusted_test_runner",
            exit_code=0,
            secret="untrusted_attacker_secret",
        )
        forged_key_ev = InvariantEvidence(
            source="trusted_test_runner",
            exit_code=0,
            verification_token=forged_key_token,
        )
        res_forged_key = oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv_strict,
            evidence=forged_key_ev,
        )
        self.assertEqual(res_forged_key.status, InvariantStatus.UNKNOWN)
        self.assertFalse(res_forged_key.verified)

        # 6d: Run linkage: claiming a non-existent run_id -> UNKNOWN
        fake_run_ev = InvariantEvidence(
            source="trusted_test_runner",
            run_id="fake-run-999",
            exit_code=0,
        )
        res_fake_run = oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv_strict,
            evidence=fake_run_ev,
        )
        self.assertEqual(res_fake_run.status, InvariantStatus.UNKNOWN)
        self.assertIn("nao existe no registro de execucoes", res_fake_run.details)

        # 6e: Valid HMAC token but missing verified completed run in strict mode -> UNKNOWN
        token_no_run = oracle.issue_evidence_token(
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="strict_security",
            source="trusted_test_runner",
            run_id=None,
            exit_code=0,
        )
        ev_no_run = InvariantEvidence(
            source="trusted_test_runner",
            exit_code=0,
            verification_token=token_no_run,
        )
        res_no_run = oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv_strict,
            evidence=ev_no_run,
        )
        self.assertEqual(res_no_run.status, InvariantStatus.UNKNOWN)
        self.assertFalse(res_no_run.verified)
        self.assertIn("execucao comprovada no registro da tarefa", res_no_run.details)

        # 6f: Both authentic HMAC token bound to run_id AND verified completed run -> PASS
        real_run_id, _, _ = self.store.reserve(self.task_id, "code", "run prompt", 5)
        self.store.transition_run(real_run_id, to_status=RunState.STARTING, pid=5555, process_create_time=777)
        self.store.transition_run(real_run_id, to_status=RunState.RUNNING)
        self.store.confirm_exit(real_run_id, exit_code=0)

        valid_token_with_run = oracle.issue_evidence_token(
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="strict_security",
            source="trusted_test_runner",
            run_id=real_run_id,
            exit_code=0,
        )
        real_run_ev = InvariantEvidence(
            source="trusted_test_runner",
            run_id=real_run_id,
            exit_code=0,
            verification_token=valid_token_with_run,
        )
        res_real_run = oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv_strict,
            evidence=real_run_ev,
        )
        self.assertEqual(res_real_run.status, InvariantStatus.PASS)
        self.assertTrue(res_real_run.verified)

    # -------------------------------------------------------------------------
    # Scenario 7: Proteção Administrativa de Barreiras de Ativação (3E.2)
    # -------------------------------------------------------------------------
    def test_administrative_override_protection(self):
        """Validates that disabling safety barriers (require_approval=False,
        enforce_invariants=False, enforce_clean_resources=False) strictly requires
        caller_role in ('admin', 'operator'), an admin_override_reason, AND a valid
        admin_token generated by the supervisor.
        """
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="Plano Admin")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Proposta sem aprovacao",
        )
        # Note: proposal is in 'proposed', NOT approved

        # 7a: Unprivileged call trying require_approval=False -> UnauthorizedOverrideError
        with self.assertRaises(UnauthorizedOverrideError) as ctx_unauth:
            self.store.activate_trajectory_proposal(
                prop.id,
                require_approval=False,
                caller_role="worker",
            )
        self.assertIn("token administrativo de supervisor ausente ou invalido", str(ctx_unauth.exception))

        # 7b: Self-declared admin role without valid supervisor admin_token -> UnauthorizedOverrideError
        with self.assertRaises(UnauthorizedOverrideError) as ctx_forged:
            self.store.activate_trajectory_proposal(
                prop.id,
                require_approval=False,
                caller_role="admin",
                admin_token="forged_attacker_token",
                admin_override_reason="Ignorar barreiras",
            )
        self.assertIn("token administrativo de supervisor ausente ou invalido", str(ctx_forged.exception))

        # 7c: Valid admin token but without admin_override_reason -> UnauthorizedOverrideError
        admin_tok = self.store.issue_admin_override_token(
            supervisor_key=self.store.supervisor_key,
            justification="Homologacao emergencial auditada",
        )
        with self.assertRaises(UnauthorizedOverrideError) as ctx_no_reason:
            self.store.activate_trajectory_proposal(
                prop.id,
                require_approval=False,
                caller_role="admin",
                admin_token=admin_tok,
                admin_override_reason="",  # empty justification
            )
        self.assertIn("justificativa formal obrigatoria ausente", str(ctx_no_reason.exception))

        # 7d: Privileged admin with authentic token and explicit justification -> SUCCEEDS
        rev_admin = self.store.activate_trajectory_proposal(
            prop.id,
            require_approval=False,
            caller_role="admin",
            admin_token=admin_tok,
            admin_override_reason="Homologacao emergencial auditada pelo comite de seguranca",
        )
        self.assertEqual(rev_admin.revision, 2)

    # -------------------------------------------------------------------------
    # Scenario 8: Revalidação Física de Artefatos no Momento da Ativação (3E.2)
    # -------------------------------------------------------------------------
    def test_phase3e2_artifact_tampering_and_missing_revalidation_at_activation(self):
        """Validates that proposal activation recomputes physical SHA-256 hashes of
        verified artifacts on disk:
        a) If artifact is altered between evaluation and activation -> InvariantVerificationError
        b) If artifact is deleted from disk -> InvariantVerificationError
        c) If artifact matches verified SHA-256 -> activation succeeds.
        """
        artifact_path = self.proj_dir / "dist" / "bundle.js"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("console.log('original valid build');", encoding="utf-8")
        original_hash = compute_sha256(artifact_path)

        # Register invariant requiring target artifact
        self.store.create_invariant(
            task_id=self.task_id,
            description="Bundle de producao integro e gerado",
            invariant_id="build_bundle",
            required=True,
            verifier_type="trusted_test_runner",
            target_artifact=str(artifact_path),
        )

        # Propose and approve alternative route
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="R1 Base")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Adicionar B com artefato validado",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # Record genuine passing invariant evaluation with artifact hash
        pass_eval = InvariantEvaluation(
            id="eval-bundle-1",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="build_bundle",
            status=InvariantStatus.PASS,
            evidence={
                "source": "trusted_test_runner",
                "artifact_path": str(artifact_path),
                "artifact_sha256": original_hash,
                "exit_code": 0,
            },
            verified=True,
            details="Bundle validado com sucesso",
        )
        self.store.record_invariant_evaluation(pass_eval)

        # 8a: Tamper artifact on disk after evaluation
        artifact_path.write_text("console.log('malicious injected payload');", encoding="utf-8")
        with self.assertRaises(InvariantVerificationError) as ctx_tamper:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("integridade do artefato", str(ctx_tamper.exception))
        self.assertIn("foi violada apos a avaliacao", str(ctx_tamper.exception))

        # 8b: Delete artifact on disk
        artifact_path.unlink()
        with self.assertRaises(InvariantVerificationError) as ctx_missing:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("nao existe mais no disco", str(ctx_missing.exception))

        # 8c: Restore artifact with original content -> activation succeeds
        artifact_path.write_text("console.log('original valid build');", encoding="utf-8")
        rev2 = self.store.activate_trajectory_proposal(prop.id)
        self.assertEqual(rev2.revision, 2)

    # -------------------------------------------------------------------------
    # Scenario 9: Neutralização Física de Processos no SO Bloqueia Ativação (3E.2)
    # -------------------------------------------------------------------------
    def test_phase3e2_os_process_neutralization_blocks_activation(self):
        """Validates that even if a worker run is marked INTERRUPTED in SQLite,
        proposal activation queries the operating system via safe_process_alive()
        and strictly blocks activation if the PID is still physically alive in the OS.
        """
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="R1 Base")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Adicionar B",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # Worker was interrupted in DB, but rogue subprocess is still alive in OS
        run_id, _, _ = self.store.reserve(self.task_id, "code", "prompt", 5)
        self.store.transition_run(run_id, to_status=RunState.STARTING, pid=8888, process_create_time=123456)
        self.store.transition_run(run_id, to_status=RunState.INTERRUPTED, error="Tentativa de cancelamento")

        # Mock safe_process_alive to return True for PID 8888
        with patch("ai_coordinator.storage.safe_process_alive", return_value=True):
            with self.assertRaises(ActiveWorkerFencingError) as ctx_os:
                self.store.activate_trajectory_proposal(prop.id)
            self.assertIn("PID 8888", str(ctx_os.exception))
            self.assertIn("ainda esta ativo no sistema operacional", str(ctx_os.exception))

        # When process is truly neutralized in OS (safe_process_alive returns False)
        with patch("ai_coordinator.storage.safe_process_alive", return_value=False):
            rev2 = self.store.activate_trajectory_proposal(prop.id)
            self.assertEqual(rev2.revision, 2)

    # -------------------------------------------------------------------------
    # Scenario 10: Fail-Fast no Backup do Resource Ledger (3E.2)
    # -------------------------------------------------------------------------
    def test_phase3e2_resource_ledger_fail_fast_backup(self):
        """Validates that ResourceLedger.record_intent() fails fast if physical backup
        creation fails, preventing unbacked modifications.
        """
        target_f = self.proj_dir / "protected.cfg"
        target_f.write_text("config_val=10", encoding="utf-8")

        # Mock shutil.copy2 to raise an IOError during backup creation
        with patch("shutil.copy2", side_effect=OSError("Disk write error on backup partition")):
            with self.assertRaises(ResourceLedgerError) as ctx_bak_fail:
                self.store.resource_ledger.record_intent(
                    task_id=self.task_id,
                    plan_revision=1,
                    resource_type=ResourceType.FILE,
                    raw_id=str(target_f),
                    action=ResourceAction.MODIFY,
                    idempotency_key="intent-fail-bak",
                )
            self.assertIn("Falha preventiva ao criar backup fisico", str(ctx_bak_fail.exception))

        # Ledger must not contain a valid record for the failed intent
        rows = self.store.db.execute(
            "SELECT * FROM resource_ledger WHERE idempotency_key='intent-fail-bak'",
        ).fetchall()
        self.assertEqual(len(rows), 0)

    # -------------------------------------------------------------------------
    # Scenario 11: Persistência de Exigência de Autenticidade no Oracle (3E.2)
    # -------------------------------------------------------------------------
    def test_phase3e2_invariant_persistence_of_authenticity_requirement(self):
        """Validates that require_authenticity_proof is persisted in SQLite invariants
        table schema and properly retrieved.
        """
        inv_created = self.store.create_invariant(
            task_id=self.task_id,
            description="Exigencia de autenticidade persistida",
            invariant_id="inv_auth_persisted",
            required=True,
            verifier_type="trusted_test_runner",
            require_authenticity_proof=True,
        )
        self.assertTrue(inv_created.require_authenticity_proof)

        # Retrieve via get_invariants
        invs = self.store.get_invariants(self.task_id)
        matched = [i for i in invs if i.id == "inv_auth_persisted"]
        self.assertEqual(len(matched), 1)
        self.assertTrue(matched[0].require_authenticity_proof)

    # -------------------------------------------------------------------------
    # Scenario 12: Emissão Autenticada de Admin Token e Rejeição de Não Autorizados (3E.3)
    # -------------------------------------------------------------------------
    def test_phase3e3_admin_token_issuance_unauthorized_rejection(self):
        """Validates that issue_admin_override_token() strictly requires supervisor_key:
        a) Unauthenticated call without supervisor_key or with forged key -> UnauthorizedOverrideError
        b) Call with authentic supervisor_key and justification -> returns admin_token.
        """
        # 12a: Unauthenticated worker / rogue agent attempts to issue admin override token
        with self.assertRaises(UnauthorizedOverrideError) as ctx_no_key:
            self.store.issue_admin_override_token(
                supervisor_key="worker_fake_key_12345",
                justification="Tentativa de auto-concessao",
            )
        self.assertIn("chave de supervisor ausente ou invalida", str(ctx_no_key.exception))

        # 12b: Empty key rejected
        with self.assertRaises(UnauthorizedOverrideError):
            self.store.issue_admin_override_token(
                supervisor_key="",
                justification="Sem chave",
            )

        # 12c: Genuine supervisor passes valid supervisor_key -> succeeds
        valid_tok = self.store.issue_admin_override_token(
            supervisor_key=self.store.supervisor_key,
            justification="Auditoria emergencial autorizada pelo supervisor",
        )
        self.assertEqual(valid_tok, self.store.admin_token)

    # -------------------------------------------------------------------------
    # Scenario 13: Verificação de Processos no SO com Falha Fail-Closed (3E.3)
    # -------------------------------------------------------------------------
    def test_phase3e3_process_query_os_error_fail_closed(self):
        """Validates that if safe_process_alive() raises an OS exception (e.g.,
        permission error or handle error), proposal activation strictly fails-closed
        with ActiveWorkerFencingError, preventing phantom process bypass.
        """
        self.store.create_plan_revision(self.task_id, nodes=[{"node_id": "a", "title": "A"}], edges=[], summary="R1 Fail-Closed")
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "a", "title": "A"}, {"node_id": "b", "title": "B"}],
            proposed_edges=[{"from_node": "a", "to_node": "b"}],
            reason="Adicionar B com teste de erro no SO",
        )
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # Worker PID registered in task
        run_id, _, _ = self.store.reserve(self.task_id, "code", "prompt", 5)
        self.store.transition_run(run_id, to_status=RunState.STARTING, pid=9999, process_create_time=54321)
        self.store.transition_run(run_id, to_status=RunState.INTERRUPTED, error="Tentativa de parada")

        # Mock safe_process_alive raising OSError
        with patch("ai_coordinator.storage.safe_process_alive", side_effect=OSError("Access denied during OS process query")):
            with self.assertRaises(ActiveWorkerFencingError) as ctx_err:
                self.store.activate_trajectory_proposal(prop.id)
            self.assertIn("falha na consulta de status do processo no SO", str(ctx_err.exception))
            self.assertIn("fail-closed", str(ctx_err.exception))

    # -------------------------------------------------------------------------
    # Scenario 14: Proveniência de Invariantes e Bloqueio de Inserções Não Autorizadas (3E.3)
    # -------------------------------------------------------------------------
    def test_phase3e3_record_invariant_provenance_enforcement(self):
        """Validates that record_invariant_evaluation() rejects unauthentic evaluations:
        a) Attempting to record verified=True on strict invariant without token -> InvariantVerificationError
        b) Attempting to record verified=True with forged HMAC token -> InvariantVerificationError
        c) Legitimate evaluation with supervisor-issued token -> succeeds.
        """
        self.store.create_invariant(
            task_id=self.task_id,
            description="Invariante de seguranca estrita",
            invariant_id="inv_strict_provenance",
            required=True,
            verifier_type="trusted_test_runner",
            require_authenticity_proof=True,
        )

        # 14a: Worker attempts to directly persist verified=True without token
        unauth_eval = InvariantEvaluation(
            id="eval-unauth-1",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="inv_strict_provenance",
            status=InvariantStatus.PASS,
            evidence={"source": "trusted_test_runner", "exit_code": 0},
            verified=True,
            details="Tentativa de aprovacao auto-declarada",
        )
        with self.assertRaises(InvariantVerificationError) as ctx_no_tok:
            self.store.record_invariant_evaluation(unauth_eval)
        self.assertIn("sem token HMAC emitido pelo supervisor", str(ctx_no_tok.exception))

        # 14b: Worker attempts to record with forged HMAC token
        forged_eval = InvariantEvaluation(
            id="eval-forged-1",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="inv_strict_provenance",
            status=InvariantStatus.PASS,
            evidence={
                "source": "trusted_test_runner",
                "exit_code": 0,
                "verification_token": "forged_hmac_12345",
            },
            verified=True,
            details="Tentativa com token falso",
        )
        with self.assertRaises(InvariantVerificationError) as ctx_forged:
            self.store.record_invariant_evaluation(forged_eval)
        self.assertIn("token HMAC adulterado ou forjado", str(ctx_forged.exception))

        # 14c: Authentic evaluation issued by supervisor succeeds
        auth_token = self.store.issue_evidence_token(
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="inv_strict_provenance",
            source="trusted_test_runner",
            exit_code=0,
        )
        legit_eval = InvariantEvaluation(
            id="eval-legit-1",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="inv_strict_provenance",
            status=InvariantStatus.PASS,
            evidence={
                "source": "trusted_test_runner",
                "exit_code": 0,
                "verification_token": auth_token,
            },
            verified=True,
            details="Evidencia legitima assinada pelo supervisor",
        )
        recorded = self.store.record_invariant_evaluation(legit_eval)
        self.assertTrue(recorded.verified)

    # -------------------------------------------------------------------------
    # Scenario 15: Vinculação de Hash do Artefato e Nonce no HMAC (3E.3)
    # -------------------------------------------------------------------------
    def test_phase3e3_hmac_artifact_hash_and_nonce_binding(self):
        """Validates that verification token payload binds artifact_sha256 and nonce:
        a) Token issued with specific artifact_sha256 and nonce matches when presented identically
        b) Tampering artifact_sha256 or nonce invalidates token (UNKNOWN status)
        c) Token cannot be replayed with different artifact or nonce.
        """
        oracle = InvariantOracle(run_lookup=self.store.get_run_for_task)
        inv = InvariantDefinition(
            id="inv_artifact_nonce",
            task_id=self.task_id,
            description="Vinculacao de artefato e nonce",
            require_authenticity_proof=True,
        )

        real_run_id, _, _ = self.store.reserve(self.task_id, "code", "run prompt", 5)
        self.store.transition_run(real_run_id, to_status=RunState.STARTING, pid=6666, process_create_time=888)
        self.store.transition_run(real_run_id, to_status=RunState.RUNNING)
        self.store.confirm_exit(real_run_id, exit_code=0)

        art_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        test_nonce = "nonce-eval-unique-9988"

        # Issue token with artifact_sha256 and nonce
        token = oracle.issue_evidence_token(
            task_id=self.task_id,
            plan_revision=1,
            invariant_id="inv_artifact_nonce",
            source="trusted_test_runner",
            run_id=real_run_id,
            artifact_sha256=art_hash,
            exit_code=0,
            nonce=test_nonce,
        )

        # 15a: Present matching evidence -> PASS
        ev_ok = InvariantEvidence(
            source="trusted_test_runner",
            run_id=real_run_id,
            artifact_sha256=art_hash,
            exit_code=0,
            nonce=test_nonce,
            verification_token=token,
        )
        res_ok = oracle.evaluate(self.task_id, plan_revision=1, invariant=inv, evidence=ev_ok)
        self.assertEqual(res_ok.status, InvariantStatus.PASS)
        self.assertTrue(res_ok.verified)

        # 15b: Tamper artifact hash -> UNKNOWN
        ev_tampered_hash = InvariantEvidence(
            source="trusted_test_runner",
            run_id=real_run_id,
            artifact_sha256="tampered_different_hash_00000000000000000000000000000000000000",
            exit_code=0,
            nonce=test_nonce,
            verification_token=token,
        )
        res_tampered = oracle.evaluate(self.task_id, plan_revision=1, invariant=inv, evidence=ev_tampered_hash)
        self.assertEqual(res_tampered.status, InvariantStatus.UNKNOWN)
        self.assertFalse(res_tampered.verified)

        # 15c: Tamper nonce -> UNKNOWN
        ev_tampered_nonce = InvariantEvidence(
            source="trusted_test_runner",
            run_id=real_run_id,
            artifact_sha256=art_hash,
            exit_code=0,
            nonce="replayed_nonce_different",
            verification_token=token,
        )
        res_nonce_tampered = oracle.evaluate(self.task_id, plan_revision=1, invariant=inv, evidence=ev_tampered_nonce)
        self.assertEqual(res_nonce_tampered.status, InvariantStatus.UNKNOWN)
        self.assertFalse(res_nonce_tampered.verified)


if __name__ == "__main__":
    unittest.main()
