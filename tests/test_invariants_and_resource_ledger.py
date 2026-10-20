"""Acceptance test suite for Phase 3C: Invariant Oracle and Resource Ledger."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_coordinator import (
    Store,
    RunState,
    ProposalNotApprovedError,
    InvariantError,
    UntrustedEvidenceError,
    StaleEvidenceError,
    ArtifactMismatchError,
    UnauthorizedWaiverError,
    InvariantVerificationError,
    InvariantStatus,
    InvariantDefinition,
    InvariantEvidence,
    InvariantEvaluation,
    RevisionOracleReport,
    InvariantOracle,
    compute_sha256,
    ResourceLedgerError,
    ResourceConflictError,
    AuthorizationRequiredError,
    IdempotencyError,
    UnresolvedResourceError,
    ResourceState,
    ResourceType,
    ResourceAction,
    ResourceRecord,
    ResourceEvent,
    ResourceLedger,
    canonicalize_resource_id,
)
from ai_coordinator import cli


class InvariantsAndResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "coordinator_data"
        self.proj_dir = Path(self.tmp.name) / "proj"
        self.proj_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root)
        self.oracle = InvariantOracle()
        self.task_id = "task-inv-001"

        # Initialize base task
        self.store.new(self.proj_dir, "Missao Invariantes e Recursos")
        # Ensure task_id matches
        self.store.db.execute(
            "UPDATE tasks SET id=? WHERE project=?",
            (self.task_id, str(self.proj_dir.resolve())),
        )
        self.store.db.commit()

        # Create base revision R1
        self.store.create_plan_revision(
            self.task_id,
            nodes=[{"node_id": "step_1", "title": "Passo 1"}],
            edges=[],
            summary="R1 Inicial",
        )

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    # -------------------------------------------------------------------------
    # Scenario 1: Evidência real de teste aprovado -> pass
    # -------------------------------------------------------------------------
    def test_real_test_evidence_pass(self):
        test_artifact = Path(self.tmp.name) / "test_report.xml"
        test_artifact.write_text("<testsuite tests='10' failures='0'/>", encoding="utf-8")
        art_hash = compute_sha256(test_artifact)

        inv = InvariantDefinition(
            id="unit_tests",
            task_id=self.task_id,
            description="Todos os testes unitarios devem passar",
            target_artifact=str(test_artifact),
        )

        evidence = InvariantEvidence(
            source="trusted_test_runner",
            run_id="run-001",
            exit_code=0,
            artifact_path=str(test_artifact),
            artifact_sha256=art_hash,
        )

        evaluation = self.oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv,
            evidence=evidence,
        )

        self.assertEqual(evaluation.status, InvariantStatus.PASS)
        self.assertTrue(evaluation.verified)
        self.assertIn("comprovado por verificador confiavel", evaluation.details)

    # -------------------------------------------------------------------------
    # Scenario 2: Declaração textual sem comprovação -> unknown
    # -------------------------------------------------------------------------
    def test_unverified_text_claim_unknown(self):
        inv = InvariantDefinition(
            id="security_audit",
            task_id=self.task_id,
            description="Auditoria de seguranca sem vulnerabilidades",
        )

        # Worker claiming 'tests passed' via textual output
        untrusted_evidence = {
            "source": "worker_text_claim",
            "claim": "All security tests passed 100% successfully",
            "exit_code": 0,
        }

        evaluation = self.oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv,
            evidence=untrusted_evidence,
        )

        self.assertEqual(evaluation.status, InvariantStatus.UNKNOWN)
        self.assertFalse(evaluation.verified)
        self.assertIn("auto-declaracao textual", evaluation.details)

    # -------------------------------------------------------------------------
    # Scenario 3: Invariante violado -> fail
    # -------------------------------------------------------------------------
    def test_violated_invariant_fail(self):
        inv = InvariantDefinition(
            id="linter_check",
            task_id=self.task_id,
            description="Zero erros de sintaxe e estilo",
        )

        failing_evidence = InvariantEvidence(
            source="trusted_linter",
            run_id="run-002",
            exit_code=1,
            details={"errors": 3},
        )

        evaluation = self.oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv,
            evidence=failing_evidence,
        )

        self.assertEqual(evaluation.status, InvariantStatus.FAIL)
        self.assertTrue(evaluation.verified)
        self.assertIn("falha com codigo de saida 1", evaluation.details)

    # -------------------------------------------------------------------------
    # Scenario 4: Dispensa sem autorização -> Rejeição
    # -------------------------------------------------------------------------
    def test_waiver_without_authorization_rejected(self):
        inv = InvariantDefinition(
            id="e2e_tests",
            task_id=self.task_id,
            description="Testes de ponta a ponta",
        )

        # Attempting waiver without authorization flag
        with self.assertRaises(UnauthorizedWaiverError) as ctx:
            self.oracle.evaluate(
                task_id=self.task_id,
                plan_revision=1,
                invariant=inv,
                waiver_authorized=False,
                waiver_reason="Pulando por conveniencia",
            )
        self.assertIn("Dispensa (waiver) nao autorizada", str(ctx.exception))

        # Attempting waiver without reason
        with self.assertRaises(UnauthorizedWaiverError) as ctx2:
            self.oracle.evaluate(
                task_id=self.task_id,
                plan_revision=1,
                invariant=inv,
                waiver_authorized=True,
                waiver_reason="",
            )
        self.assertIn("Justificativa obrigatoria ausente", str(ctx2.exception))

        # Valid waiver succeeds
        valid_eval = self.oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv,
            waiver_authorized=True,
            waiver_reason="Dispensa expressamente aprovada pelo Tech Lead para ambiente sandbox",
        )
        self.assertEqual(valid_eval.status, InvariantStatus.NOT_APPLICABLE)
        self.assertTrue(valid_eval.verified)
        self.assertTrue(valid_eval.waiver_authorized)

    # -------------------------------------------------------------------------
    # Scenario 5: Evidência de revisão antiga -> Rejeição
    # -------------------------------------------------------------------------
    def test_stale_revision_evidence_rejected(self):
        inv = InvariantDefinition(
            id="regression_suite",
            task_id=self.task_id,
            description="Suite de regressao completa",
        )

        evidence = InvariantEvidence(
            source="trusted_test_runner",
            exit_code=0,
        )

        # Evidence was captured on revision 1, but presented to validate revision 2
        with self.assertRaises(StaleEvidenceError) as ctx:
            self.oracle.evaluate(
                task_id=self.task_id,
                plan_revision=2,
                invariant=inv,
                evidence=evidence,
                evidence_plan_revision=1,
            )
        self.assertIn("revisao 1 nao pode validar a revisao 2", str(ctx.exception))

    # -------------------------------------------------------------------------
    # Scenario 6: Artefato alterado após verificação -> Evidência invalidada
    # -------------------------------------------------------------------------
    def test_artifact_altered_invalidated(self):
        artifact = Path(self.tmp.name) / "compiled_binary.dll"
        artifact.write_bytes(b"ORIGINAL_BINARY_V1")
        original_hash = compute_sha256(artifact)

        inv = InvariantDefinition(
            id="binary_integrity",
            task_id=self.task_id,
            description="Integridade do artefato compilado",
            target_artifact=str(artifact),
        )

        evidence = InvariantEvidence(
            source="compiler_verifier",
            exit_code=0,
            artifact_path=str(artifact),
            artifact_sha256=original_hash,
        )

        # First evaluation: file matches hash -> PASS
        eval_pass = self.oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv,
            evidence=evidence,
        )
        self.assertEqual(eval_pass.status, InvariantStatus.PASS)

        # Malicious or accidental modification to binary on disk
        artifact.write_bytes(b"TAMPERED_BINARY_V2")

        # Second evaluation with same evidence: file on disk changed -> INVALIDATED / UNKNOWN
        eval_tampered = self.oracle.evaluate(
            task_id=self.task_id,
            plan_revision=1,
            invariant=inv,
            evidence=evidence,
        )
        self.assertEqual(eval_tampered.status, InvariantStatus.UNKNOWN)
        self.assertFalse(eval_tampered.verified)
        self.assertIn("Integridade do artefato comprometida", eval_tampered.details)

    # -------------------------------------------------------------------------
    # Scenario 7: Recurso criado e registrado -> Histórico completo
    # -------------------------------------------------------------------------
    def test_resource_lifecycle_and_audit_events(self):
        target_file = Path(self.tmp.name) / "new_module.py"

        # Phase 1: Intent
        rec = self.store.record_resource_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(target_file),
            action=ResourceAction.CREATE,
            idempotency_key="create_new_module_py",
            compensation_action="delete_file",
        )
        self.assertEqual(rec.status, ResourceState.INTENT_RECORDED)
        self.assertEqual(rec.action, ResourceAction.CREATE)

        # Physical operation executes
        target_file.write_text("print('hello world')", encoding="utf-8")
        file_hash = compute_sha256(target_file)

        # Phase 2: Applied
        applied_rec = self.store.confirm_resource_applied(rec.id)
        self.assertEqual(applied_rec.status, ResourceState.APPLIED)
        self.assertEqual(applied_rec.post_hash, file_hash)

        # Phase 3: Retained for subsequent revisions
        retained_rec = self.store.retain_resource(rec.id)
        self.assertEqual(retained_rec.status, ResourceState.RETAINED)

        # Verify full append-only audit trail
        events = self.store.get_resource_events(rec.id)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0].event_type, "intent_recorded")
        self.assertEqual(events[1].event_type, "applied")
        self.assertEqual(events[2].event_type, "retained")

    # -------------------------------------------------------------------------
    # Scenario 8: Queda entre efeito físico e confirmação -> Estado incerto recuperável
    # -------------------------------------------------------------------------
    def test_crash_recovery_uncertain_state(self):
        target_file = Path(self.tmp.name) / "interrupted_file.py"

        # Record intent
        rec = self.store.record_resource_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(target_file),
            action=ResourceAction.CREATE,
            idempotency_key="key_interrupted_file",
        )

        # Simulation: physical file was written, but process died before confirm_resource_applied()
        target_file.write_text("def work(): pass", encoding="utf-8")

        # Supervisor or coordinator restarts and reconciles task
        reconciled = self.store.reconcile_resources(self.task_id)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].id, rec.id)
        self.assertEqual(reconciled[0].status, ResourceState.UNCERTAIN)

        # Audit events capture the uncertain status detection
        events = self.store.get_resource_events(rec.id)
        self.assertEqual(events[-1].event_type, "uncertain_detected")
        self.assertIn("Arquivo fisico foi criado mas transicao", events[-1].details)

    # -------------------------------------------------------------------------
    # Scenario 9: Compensação executada duas vezes -> Sem efeito destrutivo adicional (idempotente)
    # -------------------------------------------------------------------------
    def test_compensation_idempotent(self):
        cleanup_file = Path(self.tmp.name) / "temp_to_cleanup.txt"
        cleanup_file.write_text("temporary content", encoding="utf-8")

        rec = self.store.record_resource_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(cleanup_file),
            action=ResourceAction.CREATE,
            idempotency_key="key_temp_cleanup",
        )
        self.store.confirm_resource_applied(rec.id)
        self.assertTrue(cleanup_file.exists())

        # First compensation execution: removes file
        comp_rec1 = self.store.apply_resource_compensation_step(rec.id)
        self.assertEqual(comp_rec1.status, ResourceState.COMPENSATED)
        self.assertFalse(cleanup_file.exists())

        # Second compensation execution: idempotent, no crash, no extra destructive side effect
        comp_rec2 = self.store.apply_resource_compensation_step(rec.id)
        self.assertEqual(comp_rec2.status, ResourceState.COMPENSATED)
        self.assertFalse(cleanup_file.exists())

    # -------------------------------------------------------------------------
    # Scenario 10: Recurso externo irreversível -> Exige autorização
    # -------------------------------------------------------------------------
    def test_external_irreversible_requires_authorization(self):
        rec = self.store.record_resource_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.EXTERNAL,
            raw_id="https://api.external.com/v1/charge",
            action=ResourceAction.CALL,
            idempotency_key="key_external_charge_001",
            compensable=False,
        )
        self.store.confirm_resource_applied(rec.id)
        self.assertTrue(rec.requires_authorization)

        # Unauthorized compensation preparation is blocked
        with self.assertRaises(AuthorizationRequiredError) as ctx:
            self.store.prepare_resource_compensation(rec.id, authorized=False)
        self.assertIn("requer autorizacao explicita", str(ctx.exception))

        # Unauthorized compensation execution is blocked
        with self.assertRaises(AuthorizationRequiredError) as ctx2:
            self.store.apply_resource_compensation_step(rec.id, authorized=False)
        self.assertIn("requer autorizacao formal", str(ctx2.exception))

        # Authorized compensation succeeds
        authorized_comp = self.store.apply_resource_compensation_step(rec.id, authorized=True)
        self.assertEqual(authorized_comp.status, ResourceState.COMPENSATED)

    # -------------------------------------------------------------------------
    # Scenario 11: Tentativa de compensar arquivo modificado por terceiro -> Bloqueada
    # -------------------------------------------------------------------------
    def test_compensation_blocked_if_modified_by_third_party(self):
        file_path = Path(self.tmp.name) / "shared_document.py"
        file_path.write_text("initial worker content", encoding="utf-8")

        rec = self.store.record_resource_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(file_path),
            action=ResourceAction.CREATE,
            idempotency_key="shared_doc_key",
        )
        self.store.confirm_resource_applied(rec.id)

        # Third party modifies document externally after worker applied change
        file_path.write_text("MALICIOUS OR CONCURRENT THIRD PARTY CHANGES", encoding="utf-8")

        # Compensation MUST FAIL CLOSED to avoid destroying concurrent work
        with self.assertRaises(ResourceConflictError) as ctx:
            self.store.apply_resource_compensation_step(rec.id)

        self.assertIn("modificado por terceiro apos a operacao original", str(ctx.exception))
        self.assertIn("Compensacao bloqueada por seguranca", str(ctx.exception))
        # Content remains protected
        self.assertEqual(file_path.read_text(encoding="utf-8"), "MALICIOUS OR CONCURRENT THIRD PARTY CHANGES")

    # -------------------------------------------------------------------------
    # Scenario 12: Proposta com recursos críticos não resolvidos -> Ativação bloqueada
    # -------------------------------------------------------------------------
    def test_activation_blocked_by_unresolved_resources_or_failed_invariants(self):
        # 1. Register mandatory invariant
        inv = self.store.create_invariant(
            task_id=self.task_id,
            description="Suite de testes de integracao obrigatoria",
            required=True,
            invariant_id="integration_tests",
        )

        # Propose a new trajectory
        prop = self.store.propose_trajectory(
            self.task_id,
            base_revision=1,
            proposed_nodes=[{"node_id": "step_1", "title": "Step 1"}, {"node_id": "step_2", "title": "Step 2"}],
            proposed_edges=[{"from_node": "step_1", "to_node": "step_2"}],
            reason="Adicionar passo 2",
        )

        # Gate 1: Unapproved proposal is strictly rejected
        with self.assertRaises(ProposalNotApprovedError) as ctx_app:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("formalmente aprovada ('approved')", str(ctx_app.exception))

        # Approve proposal
        self.store.resolve_trajectory_proposal(prop.id, "approved")

        # Gate 2: Proposal activation blocked because mandatory invariant has no evaluation (missing/unknown)
        with self.assertRaises(InvariantVerificationError) as ctx_inv:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("integration_tests", str(ctx_inv.exception))

        # Evaluate invariant as FAIL
        fail_eval = InvariantEvaluation(
            id="eval-fail-001",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id=inv.id,
            status=InvariantStatus.FAIL,
            verified=True,
        )
        self.store.record_invariant_evaluation(fail_eval)

        # Gate 3: Invariant evaluated to FAIL strictly blocks activation
        with self.assertRaises(InvariantVerificationError) as ctx_inv_fail:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("possui status 'fail'", str(ctx_inv_fail.exception))

        # Satisfy invariant with PASS
        pass_eval = InvariantEvaluation(
            id="eval-pass-001",
            task_id=self.task_id,
            plan_revision=1,
            invariant_id=inv.id,
            status=InvariantStatus.PASS,
            verified=True,
        )
        self.store.record_invariant_evaluation(pass_eval)

        # Now introduce an UNRESOLVED (uncertain) resource into the ledger
        res = self.store.record_resource_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.FILE,
            raw_id=str(Path(self.tmp.name) / "uncertain_res.txt"),
            action=ResourceAction.CREATE,
            idempotency_key="key_uncertain_res",
        )
        self.store.reconcile_resources(self.task_id)  # marks as uncertain

        # Gate 4: Activation blocked by unresolved uncertain resources
        with self.assertRaises(UnresolvedResourceError) as ctx_res:
            self.store.activate_trajectory_proposal(prop.id)
        self.assertIn("recurso(s) em estado incerto ('uncertain')", str(ctx_res.exception))

        # Resolve uncertain resource (retain or confirm)
        self.store.retain_resource(res.id)

        # All gates clear! Activation succeeds smoothly
        new_rev = self.store.activate_trajectory_proposal(prop.id)
        self.assertEqual(new_rev.revision, 2)
        self.assertIn("step_2", new_rev.nodes)

    # -------------------------------------------------------------------------
    # Scenario 13: CLI integration for invariants and resource ledger
    # -------------------------------------------------------------------------
    def test_cli_integration_invariants_and_resource_ledger(self):
        # 1. Register invariant and evaluation
        self.store.create_invariant(self.task_id, "Check CLI", required=True, invariant_id="cli_inv")
        self.store.record_invariant_evaluation(
            InvariantEvaluation(
                id="eval-cli-1",
                task_id=self.task_id,
                plan_revision=1,
                invariant_id="cli_inv",
                status=InvariantStatus.PASS,
                verified=True,
            )
        )

        # Record a resource
        self.store.record_resource_intent(
            self.task_id,
            1,
            ResourceType.FILE,
            str(Path(self.tmp.name) / "cli_file.txt"),
            ResourceAction.CREATE,
            "cli_file_key",
        )

        # CLI: invariant-check
        with patch.object(cli, "emit") as mock_emit, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "invariant-check", "--task", self.task_id, "--revision", "1"
        ]):
            cli.main()
            mock_emit.assert_called()
            report = mock_emit.call_args[0][0]
            self.assertTrue(report["all_satisfied"])
            self.assertEqual(report["status"], "pass")

        # CLI: resource-ledger
        with patch.object(cli, "emit") as mock_emit2, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "resource-ledger", "--task", self.task_id
        ]):
            cli.main()
            mock_emit2.assert_called()
            res_list = mock_emit2.call_args[0][0]
            self.assertEqual(len(res_list), 1)
            self.assertEqual(res_list[0]["idempotency_key"], "cli_file_key")

        # CLI: resource-reconcile
        with patch.object(cli, "emit") as mock_emit3, patch("sys.argv", [
            "cli.py", "--root", str(self.root), "resource-reconcile", "--task", self.task_id
        ]):
            cli.main()
            mock_emit3.assert_called()
            reconciled_list = mock_emit3.call_args[0][0]
            self.assertEqual(len(reconciled_list), 1)
            self.assertEqual(reconciled_list[0]["status"], ResourceState.UNCERTAIN)

    # -------------------------------------------------------------------------
    # Scenario 14: Secret and credential sanitization in ledger
    # -------------------------------------------------------------------------
    def test_credential_sanitization_in_resource_ledger(self):
        rec = self.store.record_resource_intent(
            task_id=self.task_id,
            plan_revision=1,
            resource_type=ResourceType.EXTERNAL,
            raw_id="https://api.gateway.internal/service",
            action=ResourceAction.CALL,
            idempotency_key="key_scrub_test",
            metadata={
                "auth_header": "Bearer secret_token_xyz_1234567890",
                "api_key": "super_secret_api_key_value",
                "normal_field": "safe_information",
            },
        )

        # Verify sensitive keys/values were scrubbed from SQLite
        self.assertEqual(rec.metadata["api_key"], "[REDACTED]")
        self.assertNotIn("secret_token_xyz", json.dumps(rec.metadata))
        self.assertEqual(rec.metadata["normal_field"], "safe_information")


if __name__ == "__main__":
    unittest.main()
