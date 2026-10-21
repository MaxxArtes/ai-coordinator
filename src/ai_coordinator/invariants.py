"""Invariant Oracle for deterministic verification of mandatory mission constraints."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Callable, Dict, List, Optional, Set, Union
import uuid

now = lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def validate_id(ident: str) -> str:
    if not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9_-]{8,64}", ident):
        raise ValueError("Identificador invalido: use letras minusculas, numeros, sublinhado e hifen (8 a 64 chars)")
    return ident


class InvariantError(Exception):
    """Base exception for invariant oracle failures."""


class UntrustedEvidenceError(InvariantError):
    """Raised when an untrusted or self-declared claim attempts to bypass verification."""


class StaleEvidenceError(InvariantError):
    """Raised when evidence from an older plan revision is supplied for a newer revision."""


class ArtifactMismatchError(InvariantError):
    """Raised when an artifact hash does not match the verified evidence on disk."""


class UnauthorizedWaiverError(InvariantError):
    """Raised when an invariant is marked not_applicable without proper authorization."""


class InvariantVerificationError(InvariantError):
    """Raised when invariant verification blocks proposal activation or progress."""


class InvariantStatus:
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"

    ALL = {PASS, FAIL, UNKNOWN, NOT_APPLICABLE}


def compute_sha256(path_or_bytes: Union[str, Path, bytes]) -> str:
    """Compute deterministic SHA-256 hex digest of a file path or raw bytes."""
    hasher = hashlib.sha256()
    if isinstance(path_or_bytes, (str, Path)):
        p = Path(path_or_bytes)
        if not p.is_file():
            raise FileNotFoundError(f"Arquivo nao encontrado para calculo SHA-256: {path_or_bytes}")
        with open(p, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
    else:
        hasher.update(path_or_bytes)
    return hasher.hexdigest()


def generate_verification_token(
    task_id: str,
    plan_revision: int,
    invariant_id: str,
    source: str,
    run_id: Optional[str] = None,
    exit_code: int = 0,
    secret: Optional[Union[str, bytes]] = None,
) -> str:
    """Generate deterministic HMAC-SHA256 verification token bound to task, revision, invariant, source, run_id, and exit_code."""
    if secret is None:
        raise ValueError("Chave secreta obrigatoria para geracao de token de autenticidade (HMAC-SHA256)")
    key = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    payload = f"{task_id}:{plan_revision}:{invariant_id}:{source}:{run_id or ''}:{exit_code}".encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class InvariantDefinition:
    id: str
    task_id: str
    description: str
    required: bool = True
    verifier_type: str = "trusted_test_runner"
    target_artifact: Optional[str] = None
    require_authenticity_proof: bool = False
    created_at: str = field(default_factory=now)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> InvariantDefinition:
        return cls(
            id=data["id"],
            task_id=data["task_id"],
            description=data["description"],
            required=bool(data.get("required", True)),
            verifier_type=data.get("verifier_type", "trusted_test_runner"),
            target_artifact=data.get("target_artifact"),
            require_authenticity_proof=bool(data.get("require_authenticity_proof", False)),
            created_at=data.get("created_at", now()),
        )


@dataclass(frozen=True)
class InvariantEvidence:
    source: str
    run_id: Optional[str] = None
    exit_code: Optional[int] = None
    artifact_path: Optional[str] = None
    artifact_sha256: Optional[str] = None
    verification_token: Optional[str] = None
    proof_type: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> InvariantEvidence:
        return cls(
            source=data.get("source", "unknown"),
            run_id=data.get("run_id"),
            exit_code=data.get("exit_code"),
            artifact_path=data.get("artifact_path"),
            artifact_sha256=data.get("artifact_sha256"),
            verification_token=data.get("verification_token"),
            proof_type=data.get("proof_type"),
            details=data.get("details"),
        )


@dataclass(frozen=True)
class InvariantEvaluation:
    id: str
    task_id: str
    plan_revision: int
    invariant_id: str
    status: str
    evidence: Optional[Dict[str, Any]] = None
    verified: bool = False
    waiver_authorized: bool = False
    waiver_reason: Optional[str] = None
    evaluated_at: str = field(default_factory=now)
    details: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> InvariantEvaluation:
        return cls(
            id=data["id"],
            task_id=data["task_id"],
            plan_revision=int(data["plan_revision"]),
            invariant_id=data["invariant_id"],
            status=data["status"],
            evidence=data.get("evidence"),
            verified=bool(data.get("verified", False)),
            waiver_authorized=bool(data.get("waiver_authorized", False)),
            waiver_reason=data.get("waiver_reason"),
            evaluated_at=data.get("evaluated_at", now()),
            details=data.get("details"),
        )


@dataclass(frozen=True)
class RevisionOracleReport:
    task_id: str
    plan_revision: int
    all_satisfied: bool
    status: str  # pass, fail, unknown
    evaluations: List[InvariantEvaluation] = field(default_factory=list)
    missing_invariants: List[str] = field(default_factory=list)
    failing_invariants: List[str] = field(default_factory=list)
    unknown_invariants: List[str] = field(default_factory=list)
    details: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "plan_revision": self.plan_revision,
            "all_satisfied": self.all_satisfied,
            "status": self.status,
            "evaluations": [e.to_dict() for e in self.evaluations],
            "missing_invariants": self.missing_invariants,
            "failing_invariants": self.failing_invariants,
            "unknown_invariants": self.unknown_invariants,
            "details": self.details,
        }


DEFAULT_TRUSTED_VERIFIERS: Set[str] = {
    "trusted_test_runner",
    "trusted_linter",
    "trusted_checker",
    "system_verifier",
    "compiler_verifier",
}


class InvariantOracle:
    """Deterministic Invariant Oracle.

    Evaluates mandatory project invariants against trusted evidence,
    pinning verification to exact task IDs, plan revisions, artifact hashes,
    and verified execution provenance.
    """

    def __init__(
        self,
        trusted_verifiers: Optional[Set[str]] = None,
        run_lookup: Optional[Callable[[str, str], Optional[Dict[str, Any]]]] = None,
        secret: Optional[Union[str, bytes]] = None,
    ) -> None:
        self.trusted_verifiers = set(trusted_verifiers) if trusted_verifiers else set(DEFAULT_TRUSTED_VERIFIERS)
        self.run_lookup = run_lookup
        self.secret = secret if secret is not None else secrets.token_bytes(32)

    def issue_evidence_token(
        self,
        task_id: str,
        plan_revision: int,
        invariant_id: str,
        source: str,
        run_id: Optional[str] = None,
        exit_code: int = 0,
    ) -> str:
        """Issue an authentic HMAC-SHA256 verification token using the supervisor's private key."""
        return generate_verification_token(
            task_id=task_id,
            plan_revision=plan_revision,
            invariant_id=invariant_id,
            source=source,
            run_id=run_id,
            exit_code=exit_code,
            secret=self.secret,
        )

    def register_trusted_verifier(self, verifier_name: str) -> None:
        """Register a new trusted verifier identity."""
        clean_name = verifier_name.strip()
        if not clean_name:
            raise ValueError("Nome do verificador confiavel nao pode ser vazio")
        self.trusted_verifiers.add(clean_name)

    def is_verifier_trusted(self, verifier_name: Optional[str]) -> bool:
        """Check whether a verifier name is in the trusted registry."""
        if not verifier_name:
            return False
        return verifier_name.strip() in self.trusted_verifiers

    def evaluate(
        self,
        task_id: str,
        plan_revision: int,
        invariant: InvariantDefinition,
        evidence: Optional[Union[InvariantEvidence, Dict[str, Any]]] = None,
        waiver_authorized: bool = False,
        waiver_reason: Optional[str] = None,
        evidence_plan_revision: Optional[int] = None,
    ) -> InvariantEvaluation:
        """Evaluate an invariant against provided evidence and policy constraints."""
        eval_id = str(uuid.uuid4())
        task_id = validate_id(task_id)

        # 1. Waiver check (not_applicable)
        if waiver_authorized or waiver_reason:
            if not waiver_authorized:
                raise UnauthorizedWaiverError("Dispensa (waiver) nao autorizada para o invariante")
            if not waiver_reason or not waiver_reason.strip():
                raise UnauthorizedWaiverError("Justificativa obrigatoria ausente para a dispensa (waiver)")

            return InvariantEvaluation(
                id=eval_id,
                task_id=task_id,
                plan_revision=plan_revision,
                invariant_id=invariant.id,
                status=InvariantStatus.NOT_APPLICABLE,
                evidence=evidence.to_dict() if hasattr(evidence, "to_dict") else evidence,
                verified=True,
                waiver_authorized=True,
                waiver_reason=waiver_reason.strip(),
                details=f"Dispensa autorizada: {waiver_reason.strip()}",
            )

        # 2. Stale evidence revision check
        if evidence_plan_revision is not None and evidence_plan_revision != plan_revision:
            raise StaleEvidenceError(
                f"Evidencia gerada para a revisao {evidence_plan_revision} nao pode validar a revisao {plan_revision}"
            )

        # 3. Check evidence presence
        if evidence is None:
            return InvariantEvaluation(
                id=eval_id,
                task_id=task_id,
                plan_revision=plan_revision,
                invariant_id=invariant.id,
                status=InvariantStatus.UNKNOWN,
                evidence=None,
                verified=False,
                details="Nenhuma evidencia fornecida para avaliacao",
            )

        ev_dict: Dict[str, Any] = evidence.to_dict() if hasattr(evidence, "to_dict") else dict(evidence)  # type: ignore

        # 4. Untrusted claim check: Textual declaration from worker or unverified source
        source = str(ev_dict.get("source", "unknown")).strip()
        if not self.is_verifier_trusted(source):
            return InvariantEvaluation(
                id=eval_id,
                task_id=task_id,
                plan_revision=plan_revision,
                invariant_id=invariant.id,
                status=InvariantStatus.UNKNOWN,
                evidence=ev_dict,
                verified=False,
                details=f"Evidencia originada de fonte nao confiavel / auto-declaracao textual: '{source}'",
            )

        # 4.1 Authenticity verification: check verification token if supplied
        token = ev_dict.get("verification_token")
        token_valid = False
        if token is not None:
            expected_token = generate_verification_token(
                task_id=task_id,
                plan_revision=plan_revision,
                invariant_id=invariant.id,
                source=source,
                run_id=ev_dict.get("run_id"),
                exit_code=int(ev_dict.get("exit_code", 0)),
                secret=self.secret,
            )
            if not hmac.compare_digest(str(token).strip(), expected_token):
                return InvariantEvaluation(
                    id=eval_id,
                    task_id=task_id,
                    plan_revision=plan_revision,
                    invariant_id=invariant.id,
                    status=InvariantStatus.UNKNOWN,
                    evidence=ev_dict,
                    verified=False,
                    details="Falha de autenticidade: token criptografico HMAC-SHA256 invalido, forjado ou com parametros divergentes",
                )
            token_valid = True

        # 4.2 Authenticity verification: check run_id against execution record if lookup available
        run_id = ev_dict.get("run_id")
        run_verified = False
        if run_id is not None and self.run_lookup is not None:
            run_info = self.run_lookup(task_id, str(run_id).strip())
            if not run_info:
                return InvariantEvaluation(
                    id=eval_id,
                    task_id=task_id,
                    plan_revision=plan_revision,
                    invariant_id=invariant.id,
                    status=InvariantStatus.UNKNOWN,
                    evidence=ev_dict,
                    verified=False,
                    details=f"Falha de autenticidade: run_id '{run_id}' nao existe no registro de execucoes da tarefa",
                )
            if run_info.get("status") not in ("complete", "COMPLETE"):
                return InvariantEvaluation(
                    id=eval_id,
                    task_id=task_id,
                    plan_revision=plan_revision,
                    invariant_id=invariant.id,
                    status=InvariantStatus.UNKNOWN,
                    evidence=ev_dict,
                    verified=False,
                    details=f"Falha de autenticidade: worker '{run_id}' nao possui status concluido ('complete'), status atual: '{run_info.get('status')}'",
                )
            if ev_dict.get("exit_code") is not None and run_info.get("exit_code") is not None:
                if int(ev_dict.get("exit_code")) != int(run_info.get("exit_code")):
                    return InvariantEvaluation(
                        id=eval_id,
                        task_id=task_id,
                        plan_revision=plan_revision,
                        invariant_id=invariant.id,
                        status=InvariantStatus.UNKNOWN,
                        evidence=ev_dict,
                        verified=False,
                        details="Falha de autenticidade: exit_code da evidencia diverge do codigo de saida registrado no executor",
                    )
            run_verified = True

        # 4.3 Mandatory authenticity proof if required by invariant definition
        if getattr(invariant, "require_authenticity_proof", False):
            # Strict mode: requires BOTH valid HMAC token AND verified execution record in task!
            if not (token_valid and run_verified):
                missing_items = []
                if not token_valid:
                    missing_items.append("token HMAC-SHA256 valido do supervisor")
                if not run_verified:
                    missing_items.append("execucao comprovada no registro da tarefa (run_id concluido)")
                return InvariantEvaluation(
                    id=eval_id,
                    task_id=task_id,
                    plan_revision=plan_revision,
                    invariant_id=invariant.id,
                    status=InvariantStatus.UNKNOWN,
                    evidence=ev_dict,
                    verified=False,
                    details=f"Evidencia rejeitada por falta de comprovacao formal vinculada: pendente {', '.join(missing_items)}",
                )

        # 5. Trusted verifier evaluation
        exit_code = ev_dict.get("exit_code")
        if exit_code is not None and int(exit_code) != 0:
            return InvariantEvaluation(
                id=eval_id,
                task_id=task_id,
                plan_revision=plan_revision,
                invariant_id=invariant.id,
                status=InvariantStatus.FAIL,
                evidence=ev_dict,
                verified=True,
                details=f"Verificador confiavel indicou falha com codigo de saida {exit_code}",
            )

        # 6. Artifact hash verification if target artifact is specified
        target_path = ev_dict.get("artifact_path") or invariant.target_artifact
        expected_hash = ev_dict.get("artifact_sha256")

        if target_path:
            p = Path(target_path)
            if not p.is_file():
                return InvariantEvaluation(
                    id=eval_id,
                    task_id=task_id,
                    plan_revision=plan_revision,
                    invariant_id=invariant.id,
                    status=InvariantStatus.UNKNOWN,
                    evidence=ev_dict,
                    verified=False,
                    details=f"Artefato verificado nao existe no disco: {target_path}",
                )

            current_hash = compute_sha256(p)
            if expected_hash and current_hash.lower() != expected_hash.strip().lower():
                return InvariantEvaluation(
                    id=eval_id,
                    task_id=task_id,
                    plan_revision=plan_revision,
                    invariant_id=invariant.id,
                    status=InvariantStatus.UNKNOWN,
                    evidence=ev_dict,
                    verified=False,
                    details=(
                        f"Integridade do artefato comprometida. Hash esperado {expected_hash}, "
                        f"mas arquivo atual possui hash {current_hash}. Evidencia invalidada."
                    ),
                )

        # Passed all verification checks
        return InvariantEvaluation(
            id=eval_id,
            task_id=task_id,
            plan_revision=plan_revision,
            invariant_id=invariant.id,
            status=InvariantStatus.PASS,
            evidence=ev_dict,
            verified=True,
            details="Invariante comprovado por verificador confiavel com integridade de artefatos validada",
        )

    def evaluate_revision(
        self,
        task_id: str,
        plan_revision: int,
        invariants: List[InvariantDefinition],
        evaluations: List[InvariantEvaluation],
    ) -> RevisionOracleReport:
        """Evaluate entire plan revision against registered mandatory invariants."""
        task_id = validate_id(task_id)
        eval_map: Dict[str, InvariantEvaluation] = {e.invariant_id: e for e in evaluations if e.plan_revision == plan_revision}

        missing: List[str] = []
        failing: List[str] = []
        unknowns: List[str] = []

        for inv in invariants:
            if not inv.required:
                continue

            ev = eval_map.get(inv.id)
            if not ev:
                missing.append(inv.id)
            elif ev.status == InvariantStatus.FAIL:
                failing.append(inv.id)
            elif ev.status == InvariantStatus.UNKNOWN or not ev.verified:
                unknowns.append(inv.id)

        if failing:
            overall_status = InvariantStatus.FAIL
            all_satisfied = False
            details = f"Revisao {plan_revision} possui invariante(s) violado(s): {', '.join(failing)}"
        elif missing or unknowns:
            overall_status = InvariantStatus.UNKNOWN
            all_satisfied = False
            unresolved = missing + unknowns
            details = f"Revisao {plan_revision} possui invariante(s) nao comprovado(s): {', '.join(unresolved)}"
        else:
            overall_status = InvariantStatus.PASS
            all_satisfied = True
            details = f"Todos os {len(invariants)} invariantes da revisao {plan_revision} estao comprovados."

        return RevisionOracleReport(
            task_id=task_id,
            plan_revision=plan_revision,
            all_satisfied=all_satisfied,
            status=overall_status,
            evaluations=list(eval_map.values()),
            missing_invariants=missing,
            failing_invariants=failing,
            unknown_invariants=unknowns,
            details=details,
        )
