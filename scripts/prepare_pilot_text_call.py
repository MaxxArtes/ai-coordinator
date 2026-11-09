#!/usr/bin/env python3
"""Pre-flight verification and execution preparation for OpenRouter Free pilot (Phase 4C.1).

Prepares the single-call controlled experiment (N=1, TEXT modality, strict US$ 0.000000).

Enforces:
1. Official key verification endpoint (https://openrouter.ai/api/v1/key).
2. Strict financial pre-checks (Spend Cap US$ 0.00, no auto-recharge).
   Classifies requirements as VERIFIED, PENDING, or FAILED.
   Requires finite numeric limit == 0.0 (is_free_tier alone is insufficient).
   Tracks provenance with EvidenceSource (API, HUMAN_DOCUMENTED, OPERATOR_FLAG, LOCAL_INVARIANT).
   Blocks inference fail-closed if any financial protection is unconfirmed.
3. Strict credential redirect defense: RejectAllRedirectsHandler rejects any HTTP redirect
   to prevent credential leakage to unauthorized destinations.
4. Official catalog fetching via OfficialOpenRouterCatalogClient(timeout_sec=...).
5. Dynamic selection of verified free text model from authenticated catalog.
6. Formal execution authorization manifest binding model, US$ 0.00 budget, code commit, and authorizer.
   Strict resolve_current_commit() with zero fallback to stale hashes.
7. Offline dry-run verification by default (identifies catalog, tokens, latency, cost as SYNTHETIC).
8. Strict single-attempt guarantee (RetryPolicy max_attempts=1, zero retries on 429/503/timeout).
9. Live execution requires --live, --authorized-by-user, and verified financial protections.
10. Atomic SQLite quota store and durable AvailabilityLedger with fsync.
11. Strict classification of metric provenance (MEASURED vs UNKNOWN).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
from enum import Enum
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request

# Ensure repository root is in sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from ai_coordinator.openrouter import (
    AuthenticatedCatalogSnapshot,
    AvailabilityEventType,
    AvailabilityLedger,
    FreeCostGuard,
    FreeModelRegistry,
    MetricProvenance,
    ModelBenchmarkRunner,
    ModelCatalogEntry,
    ModelModality,
    OfficialOpenRouterCatalogClient,
    OpenRouterHttpTransport,
    RetryPolicy,
    SlidingWindowRateLimiter,
    StrictOpenRouterRedirectHandler,
    UntrustedCatalogSourceError,
)
from ai_coordinator.openrouter.adapters import TextModelAdapter


class RequirementStatus(str, Enum):
    """Status classification for pre-flight requirements."""
    VERIFIED = "VERIFIED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class EvidenceSource(str, Enum):
    """Provenance and evidence classification for operational requirements."""
    API = "API"
    HUMAN_DOCUMENTED = "HUMAN_DOCUMENTED"
    OPERATOR_FLAG = "OPERATOR_FLAG"
    LOCAL_INVARIANT = "LOCAL_INVARIANT"
    PENDING = "PENDING"


class OpenRouterAccountSecurityError(RuntimeError):
    """Raised when an insecure operation or redirect is detected during account verification."""
    pass


class RejectAllRedirectsHandler(urllib.request.HTTPRedirectHandler):
    """Strict handler rejecting all redirects to prevent credential transmission to unauthorized destinations."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        raise OpenRouterAccountSecurityError(
            f"[FAIL-CLOSED] Redirecionamento HTTP {code} para '{newurl}' rejeitado categoricamente. "
            "Requisições autenticadas com chave de API não seguem redirecionamentos."
        )

    def http_error_301(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_303(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_307(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))

    def http_error_308(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        return self.redirect_request(req, fp, code, msg, headers, headers.get("Location", ""))


@dataclasses.dataclass(frozen=True)
class ExecutionAuthorizationRecord:
    """Formal authorization manifest binding execution parameters to human/operational consent."""
    target_model: str
    max_budget_usd: float
    code_commit: str
    execution_scope: str
    operator_intent_flag: bool
    timestamp_utc: str
    spend_cap_verified: bool
    auto_recharge_verified: bool
    manifest_id: str
    authorizer: str = "operator"
    evidence_source: str = "OPERATOR_FLAG"

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def resolve_current_commit() -> str:
    """Resolve current Git commit hash strictly from repository. Fails closed if unavailable."""
    try:
        res = subprocess.run(
            ["git", "-C", REPO_ROOT, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        commit = res.stdout.strip()
        if commit and re.fullmatch(r"[0-9a-f]{40}", commit):
            return commit
        raise ValueError(f"Formato de hash de commit inválido retornado pelo git: '{commit}'")
    except Exception as e:
        raise RuntimeError(f"Impossível resolver commit atual via 'git rev-parse HEAD': {e}") from e


def validate_authorization_manifest(
    manifest_data: Dict[str, Any],
    expected_model: str,
    expected_commit: str,
) -> Tuple[bool, str]:
    """Validate external authorization manifest file against execution parameters.

    Supports both Phase 4C.2 Ed25519 signed manifest containers (with Trust Store
    tripartite verification) and backward-compatible legacy Phase 4C.1 manifests.
    """
    # Phase 4C.2: Cryptographically verifiable signed manifest container
    if "manifest" in manifest_data and "signature" in manifest_data:
        try:
            from ai_coordinator.manifest_auth import (
                SignedAuthorizationManifest,
                TrustStore,
                TripartiteAuthorizationVerifier,
            )
            signed_manifest = SignedAuthorizationManifest.from_dict(manifest_data)
            script_dir = os.path.dirname(os.path.abspath(__file__))
            default_ts_path = os.path.join(os.path.dirname(script_dir), "docs", "benchmarks", "trust_store.json")
            if os.path.exists(default_ts_path):
                trust_store = TrustStore.load_from_file(default_ts_path)
            else:
                trust_store = TrustStore()
                if signed_manifest.signature.public_key_hex:
                    from ai_coordinator.manifest_auth import (
                        TrustedKeyEntry,
                        KEY_STATUS_ACTIVE,
                        PERMISSION_DISPATCH_OPENROUTER_FREE,
                    )
                    trust_store.register_key(
                        TrustedKeyEntry(
                            key_id=signed_manifest.signature.key_id,
                            public_key_hex=signed_manifest.signature.public_key_hex,
                            identity=signed_manifest.manifest.authorizer_identity,
                            permissions=[PERMISSION_DISPATCH_OPENROUTER_FREE],
                            status=KEY_STATUS_ACTIVE,
                            created_at_utc=signed_manifest.manifest.issued_at_utc,
                        )
                    )

            verifier = TripartiteAuthorizationVerifier(trust_store=trust_store)
            result = verifier.verify(
                signed_manifest=signed_manifest,
                expected_commit=expected_commit,
                requested_model=expected_model,
                requested_modality="text",
                requested_dispatches=1,
                human_operator_consent=True,
                check_replay=False,
            )
            if not result.is_fully_authorized:
                return False, f"Falha na validação criptográfica tripartite do manifesto: {result.summary()}"
            return True, (
                f"Manifesto formal assinado Ed25519 comprovado "
                f"(autorizador: '{signed_manifest.manifest.authorizer_identity}', "
                f"chave: '{signed_manifest.signature.key_id}', "
                f"commit: {signed_manifest.manifest.code_commit[:8]})"
            )
        except Exception as exc:
            return False, f"Erro ao processar manifesto assinado Ed25519: {exc}"

    # Legacy Phase 4C.1: Unsigned dictionary manifest validation
    required = ["target_model", "max_budget_usd", "code_commit", "authorizer", "valid_until"]
    missing = [f for f in required if f not in manifest_data or manifest_data[f] is None]
    if missing:
        return False, f"Campos obrigatórios ausentes no manifesto: {missing}"

    authorizer = str(manifest_data["authorizer"]).strip()
    if not authorizer:
        return False, "Identidade do autorizador ('authorizer') não pode estar vazia"

    budget = manifest_data["max_budget_usd"]
    if not isinstance(budget, (int, float)) or isinstance(budget, bool):
        return False, f"Orçamento inválido no manifesto: {budget}"
    if not math.isfinite(budget) or float(budget) != 0.0:
        return False, f"Orçamento no manifesto ({budget}) excede US$ 0.000000"

    target_model = manifest_data["target_model"]
    if target_model != expected_model:
        return False, f"Modelo no manifesto ('{target_model}') diverge do modelo selecionado ('{expected_model}')"

    code_commit = manifest_data["code_commit"]
    if code_commit != expected_commit:
        return False, f"Commit no manifesto ('{code_commit}') diverge do HEAD atual ('{expected_commit}')"

    valid_until = str(manifest_data["valid_until"])
    try:
        exp_dt = dt.datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
        now_utc = dt.datetime.now(dt.timezone.utc)
        if exp_dt < now_utc:
            return False, f"Manifesto de autorização expirado em {valid_until}"
    except Exception as e:
        return False, f"Formato inválido de valid_until ('{valid_until}'): {e}"

    return True, f"Manifesto formal comprovado (autorizador: '{authorizer}', commit: {code_commit[:8]})"


class OpenRouterAccountVerifier:
    """Verifies account and API key financial controls against OpenRouter official endpoint."""

    def __init__(
        self,
        timeout_sec: float = 15.0,
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        self.endpoint_url = "https://openrouter.ai/api/v1/key"
        self.timeout_sec = timeout_sec
        self.redirect_handler = RejectAllRedirectsHandler()
        self.opener = opener if opener is not None else urllib.request.build_opener(self.redirect_handler)

    def verify_key_limits(self, api_key: str) -> Dict[str, Any]:
        """Query key info from OpenRouter and extract limit, usage, and tier."""
        parsed_target = urllib.parse.urlparse(self.endpoint_url)
        if (
            parsed_target.scheme.lower() != "https"
            or (parsed_target.hostname or "").lower() != "openrouter.ai"
            or parsed_target.path != "/api/v1/key"
        ):
            return {
                "status": "FAILED",
                "error": f"Endpoint não autorizado: '{self.endpoint_url}'. Apenas 'https://openrouter.ai/api/v1/key' é permitido.",
            }

        req = urllib.request.Request(
            self.endpoint_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "AI-Coordinator/0.5.1 (Financial Verification Gate)",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self.opener.open(req, timeout=self.timeout_sec) as resp:
                effective_url = resp.geturl() if hasattr(resp, "geturl") else self.endpoint_url
                parsed_effective = urllib.parse.urlparse(effective_url)
                if (
                    parsed_effective.scheme.lower() != "https"
                    or (parsed_effective.hostname or "").lower() != "openrouter.ai"
                    or parsed_effective.path != "/api/v1/key"
                ):
                    return {
                        "status": "FAILED",
                        "error": f"URL final inesperada ou redirecionamento não autorizado: '{effective_url}'",
                    }

                status_code = resp.getcode() if hasattr(resp, "getcode") else getattr(resp, "status", getattr(resp, "code", 200))
                if status_code != 200:
                    return {"status": "FAILED", "error": f"HTTP {status_code}"}

                raw_bytes = resp.read()
                try:
                    payload = json.loads(raw_bytes.decode("utf-8"))
                except Exception as json_err:
                    return {"status": "FAILED", "error": f"Resposta JSON inválida: {json_err}"}

                data = payload.get("data")
                if not isinstance(data, dict):
                    return {"status": "FAILED", "error": "Campo 'data' ausente ou inválido na resposta da API"}

                raw_limit = data.get("limit")
                usage = data.get("usage", 0.0)
                is_free = data.get("is_free_tier", False)
                label = data.get("label", "")

                # Strict validation of limit:
                # Must be a finite numeric float/int, not bool, and strictly 0.0.
                # In OpenRouter API, limit=null indicates UNLIMITED spending (positive risk).
                limit_is_zero = False
                if raw_limit is not None and not isinstance(raw_limit, bool) and isinstance(raw_limit, (int, float)):
                    if math.isfinite(raw_limit) and float(raw_limit) == 0.0:
                        limit_is_zero = True

                return {
                    "status": "OK",
                    "limit": raw_limit,
                    "limit_is_zero": limit_is_zero,
                    "usage": usage,
                    "is_free_tier": is_free,
                    "label": label,
                }
        except OpenRouterAccountSecurityError as sec_err:
            return {"status": "FAILED", "error": str(sec_err)}
        except Exception as e:
            return {"status": "ERROR", "error": str(e)}


def evaluate_requirements(
    api_key: str,
    key_verification: Optional[Dict[str, Any]] = None,
    spend_cap_confirmed: bool = False,
    auto_recharge_confirmed: bool = False,
    model_entry: Optional[ModelCatalogEntry] = None,
    modality: ModelModality = ModelModality.TEXT,
    db_path: Optional[str] = None,
    ledger_path: Optional[str] = None,
    authorized_by_user: bool = False,
    manifest_validation_result: Optional[Tuple[bool, str]] = None,
    require_api_verification: bool = False,
    require_documented_authorization: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Evaluate and classify all financial and operational requirements with explicit evidence provenance."""
    reqs: Dict[str, Dict[str, Any]] = {}

    # 1. API_KEY_PRESENT
    has_key = bool(api_key.strip())
    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else ("CONFIGURADA" if has_key else "NÃO CONFIGURADA")
    reqs["API_KEY_PRESENT"] = {
        "status": RequirementStatus.VERIFIED if has_key else RequirementStatus.FAILED,
        "evidence_source": EvidenceSource.LOCAL_INVARIANT if has_key else EvidenceSource.PENDING,
        "details": f"Chave de API: {masked_key}",
    }

    # 2. SPEND_CAP_ENFORCED
    if key_verification is not None:
        if key_verification.get("status") == "OK":
            if key_verification.get("limit_is_zero") is True:
                reqs["SPEND_CAP_ENFORCED"] = {
                    "status": RequirementStatus.VERIFIED,
                    "evidence_source": EvidenceSource.API,
                    "details": f"Verificado via endpoint oficial /api/v1/key (limit={key_verification.get('limit')}, is_free_tier={key_verification.get('is_free_tier')})",
                }
            else:
                raw_limit = key_verification.get("limit")
                is_free = key_verification.get("is_free_tier")
                desc = "ausente/ilimitado (null)" if raw_limit is None else f"US$ {raw_limit}"
                reqs["SPEND_CAP_ENFORCED"] = {
                    "status": RequirementStatus.FAILED,
                    "evidence_source": EvidenceSource.API,
                    "details": f"Limite {desc} detectado (is_free_tier={is_free}); exige-se Hard Cap US$ 0.00 numérico",
                }
        else:
            reqs["SPEND_CAP_ENFORCED"] = {
                "status": RequirementStatus.FAILED,
                "evidence_source": EvidenceSource.API,
                "details": f"Falha na consulta à API: {key_verification.get('error')} (bloqueio fail-closed; confirmação booleana desconsiderada)",
            }
    elif require_api_verification:
        reqs["SPEND_CAP_ENFORCED"] = {
            "status": RequirementStatus.PENDING,
            "evidence_source": EvidenceSource.PENDING,
            "details": "Verificação automática via API /api/v1/key obrigatória mas não executada",
        }
    elif spend_cap_confirmed:
        reqs["SPEND_CAP_ENFORCED"] = {
            "status": RequirementStatus.VERIFIED,
            "evidence_source": EvidenceSource.OPERATOR_FLAG,
            "details": "Confirmado manualmente pelo operador no painel OpenRouter (Hard Cap US$ 0.00; sem comprovação por API)",
        }
    else:
        reqs["SPEND_CAP_ENFORCED"] = {
            "status": RequirementStatus.PENDING,
            "evidence_source": EvidenceSource.PENDING,
            "details": "Pendente de comprovação efetiva na conta OpenRouter (Hard Cap US$ 0.00)",
        }

    # 3. AUTO_RECHARGE_DISABLED
    if auto_recharge_confirmed:
        reqs["AUTO_RECHARGE_DISABLED"] = {
            "status": RequirementStatus.VERIFIED,
            "evidence_source": EvidenceSource.OPERATOR_FLAG,
            "details": "Confirmado pelo operador no painel OpenRouter (Recarga automática DESATIVADA)",
        }
    else:
        reqs["AUTO_RECHARGE_DISABLED"] = {
            "status": RequirementStatus.PENDING,
            "evidence_source": EvidenceSource.PENDING,
            "details": "Pendente de confirmação manual no painel OpenRouter",
        }

    # 4. MODEL_ZERO_COST
    if model_entry is not None:
        if model_entry.is_free:
            reqs["MODEL_ZERO_COST"] = {
                "status": RequirementStatus.VERIFIED,
                "evidence_source": EvidenceSource.LOCAL_INVARIANT,
                "details": f"Preço $0.00 confirmado (prompt={model_entry.pricing.prompt}, completion={model_entry.pricing.completion})",
            }
        else:
            reqs["MODEL_ZERO_COST"] = {
                "status": RequirementStatus.FAILED,
                "evidence_source": EvidenceSource.LOCAL_INVARIANT,
                "details": f"Modelo {model_entry.id} não possui custo zero comprovado",
            }
    else:
        reqs["MODEL_ZERO_COST"] = {
            "status": RequirementStatus.PENDING,
            "evidence_source": EvidenceSource.PENDING,
            "details": "Aguardando consulta e validação no FreeCostGuard",
        }

    # 5. MODALITY_SCOPE_RESTRICTED
    if modality == ModelModality.TEXT:
        reqs["MODALITY_SCOPE_RESTRICTED"] = {
            "status": RequirementStatus.VERIFIED,
            "evidence_source": EvidenceSource.LOCAL_INVARIANT,
            "details": "Restrito estritamente a TEXT (gate fail-closed [PILOT_SCOPE_RESTRICTION])",
        }
    else:
        reqs["MODALITY_SCOPE_RESTRICTED"] = {
            "status": RequirementStatus.FAILED,
            "evidence_source": EvidenceSource.LOCAL_INVARIANT,
            "details": f"Modalidade {modality.value} proibida no escopo do piloto inicial",
        }

    # 6. QUOTA_PERSISTENCE_READY
    if db_path:
        reqs["QUOTA_PERSISTENCE_READY"] = {
            "status": RequirementStatus.VERIFIED,
            "evidence_source": EvidenceSource.LOCAL_INVARIANT,
            "details": f"SQLite atômico configurado em {os.path.basename(db_path)}",
        }
    else:
        reqs["QUOTA_PERSISTENCE_READY"] = {
            "status": RequirementStatus.PENDING,
            "evidence_source": EvidenceSource.PENDING,
            "details": "Pendente de inicialização de storage",
        }

    # 7. AUDIT_PERSISTENCE_READY
    if ledger_path:
        reqs["AUDIT_PERSISTENCE_READY"] = {
            "status": RequirementStatus.VERIFIED,
            "evidence_source": EvidenceSource.LOCAL_INVARIANT,
            "details": f"AvailabilityLedger com fsync configurado em {os.path.basename(ledger_path)}",
        }
    else:
        reqs["AUDIT_PERSISTENCE_READY"] = {
            "status": RequirementStatus.PENDING,
            "evidence_source": EvidenceSource.PENDING,
            "details": "Pendente de inicialização de storage",
        }

    # 8. EXECUTION_AUTHORIZATION
    if manifest_validation_result is not None:
        is_valid, reason = manifest_validation_result
        if is_valid:
            reqs["EXECUTION_AUTHORIZATION"] = {
                "status": RequirementStatus.VERIFIED,
                "evidence_source": EvidenceSource.HUMAN_DOCUMENTED,
                "details": reason,
            }
        else:
            reqs["EXECUTION_AUTHORIZATION"] = {
                "status": RequirementStatus.FAILED,
                "evidence_source": EvidenceSource.HUMAN_DOCUMENTED,
                "details": f"Manifesto formal rejeitado: {reason}",
            }
    elif require_documented_authorization:
        reqs["EXECUTION_AUTHORIZATION"] = {
            "status": RequirementStatus.PENDING,
            "evidence_source": EvidenceSource.PENDING,
            "details": (
                "Manifesto formal documentado (--authorization-manifest) é OBRIGATÓRIO para o piloto real N=1. "
                "A flag operacional isolada (--authorized-by-user) não comprova consentimento humano independente."
            ),
        }
    elif authorized_by_user:
        reqs["EXECUTION_AUTHORIZATION"] = {
            "status": RequirementStatus.VERIFIED,
            "evidence_source": EvidenceSource.OPERATOR_FLAG,
            "details": "Flag operacional --authorized-by-user presente (indicação de intenção operacional, não comprovada por manifesto formal)",
        }
    else:
        reqs["EXECUTION_AUTHORIZATION"] = {
            "status": RequirementStatus.FAILED,
            "evidence_source": EvidenceSource.PENDING,
            "details": "Execução não autorizada pelo operador (ausência de flag ou manifesto)",
        }

    return reqs


def print_requirements_table(reqs: Dict[str, Dict[str, Any]]) -> None:
    """Print a clean formatted table of requirement statuses and evidence sources."""
    print("=" * 105)
    print(" AI Coordinator — Matriz de Pré-Requisitos Financeiros e Operacionais (Piloto 4C.1)")
    print("=" * 105)
    print(f" {'Requisito':<28} | {'Situação':<10} | {'Origem Evidência':<18} | {'Detalhes'}")
    print("-" * 105)
    for req_name, info in reqs.items():
        status_val = info["status"].value if isinstance(info["status"], RequirementStatus) else str(info["status"])
        source_val = info.get("evidence_source", EvidenceSource.PENDING)
        source_str = source_val.value if isinstance(source_val, EvidenceSource) else str(source_val)
        print(f" {req_name:<28} | {status_val:<10} | {source_str:<18} | {info['details']}")
    print("=" * 105)


def prepare_offline_dry_run() -> int:
    """Execute a complete pre-flight simulation offline to verify all gates without network calls."""
    print("=" * 88)
    print(" AI Coordinator — Pré-Voo do Piloto OpenRouter Free (Modo Dry-Run / Offline)")
    print("=" * 88)

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    # 1. Setup local registry with synthetic snapshot
    raw_catalog = [
        {
            "id": "meta-llama/llama-3.2-3b-instruct:free",
            "name": "Meta: Llama 3.2 3B Instruct (free)",
            "context_length": 131072,
            "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
            "architecture": {"modality": "text->text", "instruct_type": "llama3"},
        },
        {
            "id": "google/gemini-2.0-flash-exp:free",
            "name": "Google: Gemini 2.0 Flash Experimental (free)",
            "context_length": 1048576,
            "pricing": {"prompt": "0", "completion": "0", "image": "0", "request": "0"},
            "architecture": {"modality": "text->text", "instruct_type": "gemini"},
        },
    ]
    raw_bytes = json.dumps({"data": raw_catalog}).encode("utf-8")
    snapshot = AuthenticatedCatalogSnapshot.create(raw_bytes)
    registry = FreeModelRegistry(require_trusted_source=False)
    registry.load_authenticated_snapshot(snapshot)

    # 2. Dynamic model selection
    text_models = registry.get_by_modality(ModelModality.TEXT)
    if not text_models:
        print("  [ERRO] Nenhum modelo gratuito de TEXT localizado no catálogo.")
        return 1

    selected_model = text_models[0].id
    guard = FreeCostGuard(registry)
    entry = guard.authorize_model_execution(selected_model)

    with tempfile.TemporaryDirectory(prefix="pilot_dryrun_") as tmpdir:
        db_path = os.path.join(tmpdir, "quotas.db")
        ledger_path = os.path.join(tmpdir, "pilot_audit.jsonl")

        reqs = evaluate_requirements(
            api_key=api_key,
            model_entry=entry,
            modality=ModelModality.TEXT,
            db_path=db_path,
            ledger_path=ledger_path,
            authorized_by_user=False,
        )
        print_requirements_table(reqs)

        limiter = SlidingWindowRateLimiter(sqlite_path=db_path, max_per_minute=20, max_per_day=50)
        ledger = AvailabilityLedger(storage_path=ledger_path, require_durable=True)

        transport = OpenRouterHttpTransport(
            cost_guard=guard,
            rate_limiter=limiter,
            ledger=ledger,
            live_network_enabled=False,  # Keep offline
        )

        strict_single_attempt_policy = RetryPolicy(max_attempts=1)
        runner = ModelBenchmarkRunner(
            registry=registry,
            cost_guard=guard,
            ledger=ledger,
            rate_limiter=limiter,
            transport=transport,
            retry_policy=strict_single_attempt_policy,
        )

        sim_called = False

        def mock_handler(inputs: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal sim_called
            sim_called = True
            return {
                "success": True,
                "status_code": 200,
                "text": "Classified: POSITIVE",
                "reported_tokens": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
                "latency_ms": 145.2,
            }

        mock_adapter = TextModelAdapter(guard, mock_handler=mock_handler)
        runner.register_custom_adapter(ModelModality.TEXT, mock_adapter)

        # Run single synthetic execution
        result = runner.execute_task(
            model_id=selected_model,
            modality=ModelModality.TEXT,
            inputs={"messages": [{"role": "user", "content": "Classify sentiment: This product exceeded all my expectations."}]},
            run_id_prefix="pilot_dryrun",
        )

        print("\n[Execução Pré-Voo Sintética]")
        print(f"  Status: {'SUCESSO' if result.success else 'FALHA'} (simulação local de bancada)")
        print(f"  Catálogo: SINTÉTICO (mock local para teste de infraestrutura)")
        print(f"  Modelo Selecionado: {result.model_id} (seleção dinâmica)")
        print(f"  Latência Total: {result.latency_ms:.2f} ms (SINTÉTICA — medição local de mock, sem conexão de rede)")
        print(f"  Tokens Reportados: {result.tokens} (SINTÉTICOS — estimativa de bancada, proveniência: {result.token_provenance.value})")
        print(f"  TTFT: {result.ttft_ms} (UNKNOWN — streaming desabilitado)")
        print(f"  Custo Apresentado: US$ {result.cost_usd:.6f} (SINTÉTICO — NÃO REPRESENTA COBRANÇA EFETIVA VERIFICADA EM CONTA REAL)")
        print(f"  Despacho simulado acionado: {sim_called}")
        print(f"  Auditoria gravada no ledger: {os.path.exists(ledger_path)}")

    print("\n" + "=" * 88)
    print(" Pré-voo sintético concluído com SUCESSO.")
    print(" Nenhuma chamada externa de rede foi realizada e nenhum custo real foi verificado.")
    print(" Para o teste operacional live quando formalmente autorizado:")
    print("   python scripts/prepare_pilot_text_call.py --live --authorized-by-user "
          "--confirm-spend-cap-zero --confirm-no-auto-recharge")
    print("=" * 88)
    return 0


def execute_live_pilot(
    model_id_override: Optional[str] = None,
    api_key_override: Optional[str] = None,
    catalog_client: Optional[OfficialOpenRouterCatalogClient] = None,
    account_verifier: Optional[OpenRouterAccountVerifier] = None,
    confirm_spend_cap_zero: bool = False,
    confirm_no_auto_recharge: bool = False,
    verify_key_endpoint: bool = False,
    authorization_manifest_path: Optional[str] = None,
    authorized_by_user: bool = False,
    bench_dir: Optional[str] = None,
    custom_transport: Optional[OpenRouterHttpTransport] = None,
) -> int:
    """Execute the live N=1 pilot call following strict governance protocol."""
    print("=" * 88)
    print(" AI Coordinator — Chamada Experimental Controlada (LIVE N=1)")
    print("=" * 88)

    # Gate 0: Operator intent flag is mandatory
    if not authorized_by_user:
        print("[ABORTADO] Execução live recusada: a flag --authorized-by-user é obrigatória.", file=sys.stderr)
        print("O piloto em ambiente real depende de autorização formal expressa do usuário.", file=sys.stderr)
        return 1

    # Resolve commit strictly
    try:
        commit_hash = resolve_current_commit()
    except Exception as e:
        print(f"[ERRO CRÍTICO] Falha ao verificar commit atual do repositório: {e}", file=sys.stderr)
        return 1

    api_key = (api_key_override or os.environ.get("OPENROUTER_API_KEY", "")).strip()

    # Step 1: Storage paths setup
    bench_directory = bench_dir or os.path.join(REPO_ROOT, "docs", "benchmarks")
    os.makedirs(bench_directory, exist_ok=True)
    db_path = os.path.join(bench_directory, "pilot_quotas.db")
    ledger_path = os.path.join(bench_directory, "pilot_audit.jsonl")

    # Step 2: Query account verification endpoint if requested or in live mode with key
    key_verification: Optional[Dict[str, Any]] = None
    if (verify_key_endpoint or api_key) and api_key:
        verifier = account_verifier or OpenRouterAccountVerifier(timeout_sec=15.0)
        key_verification = verifier.verify_key_limits(api_key)

    # Step 3: Fetch fresh official authenticated catalog via HTTPS
    print("\n[Passo 1/6] Obtendo catálogo oficial via HTTPS com atestação de origem...")
    client = catalog_client or OfficialOpenRouterCatalogClient(timeout_sec=20.0)
    try:
        snapshot = client.fetch_catalog()
    except Exception as e:
        print(f"[ERRO CRÍTICO] Falha ao consultar catálogo oficial da OpenRouter: {e}")
        return 1

    # Step 4: Load into trusted registry and validate provenance
    print("[Passo 2/6] Carregando catálogo oficial no FreeModelRegistry...")
    registry = FreeModelRegistry(require_trusted_source=True)
    registry.load_authenticated_snapshot(snapshot)
    if not registry.is_authenticated_origin:
        print("[ERRO CRÍTICO] Atestação de origem não confirmada. Abortando fail-closed.")
        return 1

    # Step 5: Dynamic selection and strict zero-cost authorization
    print("[Passo 3/6] Selecionando modelo gratuito de TEXT dinamicamente...")
    text_models = registry.get_by_modality(ModelModality.TEXT)
    if not text_models:
        print("[ERRO CRÍTICO] Nenhum modelo gratuito de TEXT disponível no catálogo oficial.")
        return 1

    chosen_model = model_id_override or text_models[0].id
    guard = FreeCostGuard(registry)
    try:
        entry = guard.authorize_model_execution(chosen_model)
    except Exception as e:
        print(f"[ERRO CRÍTICO] Modelo {chosen_model} não autorizado pelo FreeCostGuard: {e}")
        return 1

    # Step 6: Validate manifest if provided
    manifest_validation_result: Optional[Tuple[bool, str]] = None
    manifest_data: Optional[Dict[str, Any]] = None
    if authorization_manifest_path:
        if not os.path.exists(authorization_manifest_path):
            manifest_validation_result = (False, f"Arquivo de manifesto não encontrado: {authorization_manifest_path}")
        else:
            try:
                with open(authorization_manifest_path, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
                manifest_validation_result = validate_authorization_manifest(
                    manifest_data,
                    expected_model=chosen_model,
                    expected_commit=commit_hash,
                )
            except Exception as e:
                manifest_validation_result = (False, f"Erro ao ler manifesto JSON: {e}")

    # Step 7: Evaluate requirements matrix before any inference dispatch
    print("[Passo 4/6] Avaliando matriz de pré-requisitos financeiros e operacionais...")
    reqs = evaluate_requirements(
        api_key=api_key,
        key_verification=key_verification,
        spend_cap_confirmed=confirm_spend_cap_zero,
        auto_recharge_confirmed=confirm_no_auto_recharge,
        model_entry=entry,
        modality=ModelModality.TEXT,
        db_path=db_path,
        ledger_path=ledger_path,
        authorized_by_user=authorized_by_user,
        manifest_validation_result=manifest_validation_result,
        require_api_verification=verify_key_endpoint,
        require_documented_authorization=True,
    )
    print_requirements_table(reqs)

    # Fail-closed check: every single requirement must be VERIFIED
    unverified = [k for k, v in reqs.items() if v["status"] != RequirementStatus.VERIFIED]
    if unverified:
        print(f"\n[BLOQUEIO FINANCEIRO] Impossível prosseguir para inferência: {len(unverified)} requisito(s) NÃO VERIFICADO(S):")
        for u in unverified:
            source_str = reqs[u].get("evidence_source", EvidenceSource.PENDING)
            source_val = source_str.value if isinstance(source_str, EvidenceSource) else str(source_str)
            print(f"  - {u}: {reqs[u]['status'].value} [{source_val}] ({reqs[u]['details']})")
        print("\nA inferência live está terminantemente bloqueada até a comprovação de todos os requisitos.")
        return 1

    # Step 8: Build and register formal ExecutionAuthorizationRecord
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest_id = f"auth_{hashlib.sha256(f'{chosen_model}:{commit_hash}:{now_iso}'.encode()).hexdigest()[:16]}"
    authorizer_identity = manifest_data.get("authorizer", "operator") if manifest_data else "operator"
    evidence_type = "HUMAN_DOCUMENTED" if manifest_validation_result and manifest_validation_result[0] else "OPERATOR_FLAG"

    auth_record = ExecutionAuthorizationRecord(
        target_model=chosen_model,
        max_budget_usd=0.000000,
        code_commit=commit_hash,
        execution_scope="N=1 single controlled pilot call (TEXT modality)",
        operator_intent_flag=authorized_by_user,
        timestamp_utc=now_iso,
        spend_cap_verified=True,
        auto_recharge_verified=True,
        manifest_id=manifest_id,
        authorizer=authorizer_identity,
        evidence_source=evidence_type,
    )

    # Step 9: Initialize durable rate limiter and ledger
    limiter = SlidingWindowRateLimiter(sqlite_path=db_path, max_per_minute=20, max_per_day=50)
    ledger = AvailabilityLedger(storage_path=ledger_path, require_durable=True)

    # Record startup authorization in ledger
    ledger.record_event(
        model_id=chosen_model,
        event_type=AvailabilityEventType.BENCHMARK_EXECUTED,
        details={"stage": "pilot_authorization_manifest", "authorization_manifest": auth_record.to_dict()},
    )

    # Step 10: Configure live transport with STRICT SINGLE ATTEMPT (NO RETRIES)
    print("\n[Passo 5/6] Ativando OpenRouterHttpTransport em modo LIVE (tentativa única estrita N=1)...")
    transport = custom_transport or OpenRouterHttpTransport(
        cost_guard=guard,
        rate_limiter=limiter,
        ledger=ledger,
        api_key=api_key,
        live_network_enabled=True,  # LIVE OPT-IN
    )

    strict_single_attempt_policy = RetryPolicy(max_attempts=1)
    runner = ModelBenchmarkRunner(
        registry=registry,
        cost_guard=guard,
        ledger=ledger,
        rate_limiter=limiter,
        transport=transport,
        retry_policy=strict_single_attempt_policy,
    )

    # Step 11: Dispatch single controlled call (N=1)
    prompt = "Classify sentiment: This product exceeded all my expectations."
    print(f"[Passo 6/6] Despachando chamada controlada N=1 para {chosen_model}...")
    t0 = time.perf_counter()
    result = runner.execute_task(
        model_id=chosen_model,
        modality=ModelModality.TEXT,
        inputs={"messages": [{"role": "user", "content": prompt}]},
        run_id_prefix="pilot_live_n1",
    )
    elapsed = (time.perf_counter() - t0) * 1000

    print("\n" + "=" * 88)
    print(" Resultado da Execução Experimental Live (N=1)")
    print("=" * 88)
    print(f"  Sucesso: {result.success}")
    print(f"  Status HTTP: {result.status_code}")
    print(f"  Latência Total: {result.latency_ms:.2f} ms (medido localmente: {elapsed:.2f} ms)")
    print(f"  Tokens Reportados: {result.tokens} (Proveniência: {result.token_provenance.value})")
    print(f"  TTFT: {result.ttft_ms} (Proveniência: {result.ttft_provenance.value})")
    cost_prov_val = (
        result.cost_provenance.value
        if isinstance(getattr(result, "cost_provenance", None), MetricProvenance)
        else str(getattr(result, "cost_provenance", "UNKNOWN"))
    )
    print(f"  Custo Reportado: US$ {result.cost_usd:.6f} (Proveniência: {cost_prov_val} — presumido pelo catálogo, não cobrança efetiva faturada)")
    if result.error_message:
        print(f"  Mensagem de Erro: {result.error_message}")
    print(f"  Auditoria gravada em: {ledger_path}")
    print(f"  Manifesto de Autorização: {manifest_id} (origem: {evidence_type})")
    print("=" * 88)

    # Save run artifact
    result_dict = result.to_dict()
    result_dict["authorization_record"] = auth_record.to_dict()
    result_dict["cost_provenance"] = cost_prov_val
    result_dict["cost_statement"] = "Presumido pelo catálogo com base no preço declarado US$ 0.00; não representa cobrança bancária ou fatura confirmada pela API."
    result_json_path = os.path.join(bench_directory, "pilot_live_n1_result.json")
    with open(result_json_path, "w", encoding="utf-8") as f:
        json.dump(result_dict, f, indent=2)
    print(f"Resultado persistido em: {result_json_path}")

    return 0 if result.success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Coordinator — Pré-Voo e Execução do Piloto OpenRouter Free")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Executar pré-voo offline de teste sem rede")
    parser.add_argument("--check-prerequisites", action="store_true", default=False, help="Conferir matriz de pré-requisitos")
    parser.add_argument("--live", action="store_true", default=False, help="Habilitar execução live na rede")
    parser.add_argument("--authorized-by-user", action="store_true", default=False, help="Indicação operacional de autorização humana")
    parser.add_argument("--confirm-spend-cap-zero", action="store_true", default=False, help="Confirmar Spend Cap US$ 0.00 verificado no painel")
    parser.add_argument("--confirm-no-auto-recharge", action="store_true", default=False, help="Confirmar recarga automática desativada no painel")
    parser.add_argument("--verify-key-endpoint", action="store_true", default=False, help="Consultar endpoint /api/v1/key para verificação")
    parser.add_argument("--authorization-manifest", type=str, default=None, help="Caminho para manifesto JSON de autorização formal")
    parser.add_argument("--model-id", type=str, default=None, help="Sobrescrever ID do modelo gratuito")

    args = parser.parse_args()

    if args.check_prerequisites:
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        key_verification = None
        if args.verify_key_endpoint and api_key:
            verifier = OpenRouterAccountVerifier()
            key_verification = verifier.verify_key_limits(api_key)

        manifest_validation_result = None
        if args.authorization_manifest:
            try:
                commit_hash = resolve_current_commit()
            except Exception:
                commit_hash = "unresolved"
            if not os.path.exists(args.authorization_manifest):
                manifest_validation_result = (False, f"Arquivo não encontrado: {args.authorization_manifest}")
            else:
                try:
                    with open(args.authorization_manifest, "r", encoding="utf-8") as f:
                        m_data = json.load(f)
                    manifest_validation_result = validate_authorization_manifest(
                        m_data,
                        expected_model=args.model_id or m_data.get("target_model", ""),
                        expected_commit=commit_hash,
                    )
                except Exception as e:
                    manifest_validation_result = (False, f"Erro ao ler JSON: {e}")

        reqs = evaluate_requirements(
            api_key=api_key,
            key_verification=key_verification,
            spend_cap_confirmed=args.confirm_spend_cap_zero,
            auto_recharge_confirmed=args.confirm_no_auto_recharge,
            modality=ModelModality.TEXT,
            authorized_by_user=args.authorized_by_user,
            manifest_validation_result=manifest_validation_result,
            require_api_verification=args.verify_key_endpoint,
        )
        print_requirements_table(reqs)
        unverified = [k for k, v in reqs.items() if v["status"] != RequirementStatus.VERIFIED]
        return 0 if not unverified else 1

    if args.live:
        return execute_live_pilot(
            model_id_override=args.model_id,
            confirm_spend_cap_zero=args.confirm_spend_cap_zero,
            confirm_no_auto_recharge=args.confirm_no_auto_recharge,
            verify_key_endpoint=args.verify_key_endpoint,
            authorization_manifest_path=args.authorization_manifest,
            authorized_by_user=args.authorized_by_user,
        )

    # Default to offline dry-run
    return prepare_offline_dry_run()


if __name__ == "__main__":
    sys.exit(main())
