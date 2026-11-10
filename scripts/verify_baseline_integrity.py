#!/usr/bin/env python3
"""Standalone Independent Verifier for AI Coordinator Baseline Reports (Phase 4C.0.5).

This script provides a 100% decoupled, standalone verification implementation
using ONLY Python standard libraries (hashlib, hmac, json, math, os, sys).
It does not depend on the ai_coordinator package, enabling independent third-party
auditors to verify report integrity and authenticity without installing the coordinator.

Security & Authentication Architecture:
1. Permanent Discontinuation of Direct CLI Secrets:
   Passing secrets via --hmac-key or sys.argv is strictly rejected with a fatal error (CWE-214).
   Secrets must only be ingested via:
   - Environment variables (--key-env NAME or default AI_COORDINATOR_HMAC_KEY)
   - Secure file reading (--key-file PATH)
   - Standard input stream (--key-stdin)

2. Public-Key Asymmetric Digital Signature (RSASSA-PKCS1-v1_5 with SHA-256):
   - Proves authentic authorship to external auditors without requiring any shared secrets.
   - Verified against the project's published public key (keys/baseline_authority_pubkey.json
     or custom path via --public-key PATH).

3. Internal Cluster HMAC-SHA256 Authentication:
   - Provides symmetric mutual authentication for internal peer nodes and runners.

4. Hierarchical Deep Audit:
   - Root manifest SHA-256 hash integrity.
   - Asymmetric digital signature proof of authority.
   - Deterministic task_results_digest matching all individual task results.
   - Individual task report SHA-256 integrity and internal HMAC signatures.
   - Full round-trip fidelity including security_verdict taxonomy.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# RFC 8017 / PKCS #1 v2.2 DigestInfo prefix for SHA-256
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rsa_verify_standalone(
    message_bytes: bytes,
    signature_hex: str,
    modulus_hex: str,
    public_exponent: int = 65537,
    key_size_bytes: int = 256,
) -> bool:
    """Pure-Python standard-library verification of RSASSA-PKCS1-v1_5 SHA-256 signature."""
    try:
        sig_bytes = bytes.fromhex(signature_hex.strip())
        if len(sig_bytes) != key_size_bytes:
            return False
        sig_int = int.from_bytes(sig_bytes, byteorder="big")
        n = int(modulus_hex, 16)
        if sig_int >= n:
            return False
        recovered_int = pow(sig_int, public_exponent, n)
        recovered_em = recovered_int.to_bytes(key_size_bytes, byteorder="big")

        digest = hashlib.sha256(message_bytes).digest()
        t = SHA256_DIGEST_INFO + digest
        ps_len = key_size_bytes - len(t) - 3
        expected_em = b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t
        return hmac.compare_digest(recovered_em, expected_em)
    except Exception:
        return False


def normalize_json_structure(data: Any) -> Any:
    """Recursively normalize data structure for deterministic canonical JSON serialization."""
    if data is None or isinstance(data, (bool, int, str)):
        return data
    if isinstance(data, float):
        return None if (math.isnan(data) or math.isinf(data)) else round(data, 8)
    if isinstance(data, dict):
        return {str(k): normalize_json_structure(v) for k, v in sorted(data.items(), key=lambda kv: str(kv[0]))}
    if isinstance(data, (list, tuple, set, frozenset)):
        if isinstance(data, (set, frozenset)):
            try:
                items = sorted(list(data), key=lambda x: str(x))
            except Exception:
                items = list(data)
        else:
            items = list(data)
        return [normalize_json_structure(x) for x in items]
    return str(data)


def canonical_json_bytes(data: Any) -> bytes:
    """Serialize normalized data into deterministic canonical UTF-8 bytes."""
    normalized = normalize_json_structure(data)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def safe_round(val: Any, decimals: int = 4) -> Optional[float]:
    """Safely round floating point values handling None, NaN, and Inf without raising."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, decimals)
    except (ValueError, TypeError):
        return None


def compute_task_canonical_manifest(oracle: Dict[str, Any], benchmark_version: Optional[str] = None) -> Dict[str, Any]:
    """Reconstruct canonical manifest for an individual task evaluation oracle report."""
    ver = benchmark_version if benchmark_version is not None else oracle.get("benchmark_version", "2026.11.03")
    obs_tok = oracle.get("observed_tokens", {})
    obs_cost = oracle.get("observed_cost", {})
    obs_lat = oracle.get("observed_latencies", {})

    return {
        "version": ver,
        "task_id": oracle.get("task_id"),
        "suite": oracle.get("suite"),
        "timestamp": oracle.get("timestamp"),
        "input_payload_hash": oracle.get("input_payload_hash", ""),
        "expected_output_hash": oracle.get("expected_output_hash"),
        "canonical_output_hash": oracle.get("canonical_output_hash"),
        "passed_all": oracle.get("passed_all"),
        "functional_success": oracle.get("functional_success"),
        "correctness_score": round(float(oracle.get("correctness_score", 0.0)), 4),
        "efficiency_success": oracle.get("efficiency_success"),
        "budget_tokens_compliant": oracle.get("budget_tokens_compliant"),
        "budget_cost_compliant": oracle.get("budget_cost_compliant"),
        "budget_latency_compliant": oracle.get("budget_latency_compliant", True),
        "observed_tokens": {
            "input_tokens": obs_tok.get("input_tokens"),
            "output_tokens": obs_tok.get("output_tokens"),
            "cache_read_tokens": obs_tok.get("cache_read_tokens"),
            "cache_creation_tokens": obs_tok.get("cache_creation_tokens"),
            "total_tokens": obs_tok.get("total_tokens"),
            "provenance": obs_tok.get("provenance"),
        },
        "observed_cost": {
            "input_cost_usd": safe_round(obs_cost.get("input_cost_usd"), 6),
            "output_cost_usd": safe_round(obs_cost.get("output_cost_usd"), 6),
            "cache_read_cost_usd": safe_round(obs_cost.get("cache_read_cost_usd"), 6),
            "cache_creation_cost_usd": safe_round(obs_cost.get("cache_creation_cost_usd"), 6),
            "total_cost_usd": safe_round(obs_cost.get("total_cost_usd"), 6),
            "pricing_version": obs_cost.get("pricing_version"),
            "provenance": obs_cost.get("provenance"),
        },
        "observed_latencies": {
            "total_duration_ms": round(float(obs_lat.get("total_duration_ms", 0.0)), 4),
            "ttft_ms": safe_round(obs_lat.get("ttft_ms"), 4),
            "generation_duration_ms": safe_round(obs_lat.get("generation_duration_ms"), 4),
            "tool_duration_ms": safe_round(obs_lat.get("tool_duration_ms"), 4),
            "supervisor_overhead_ms": safe_round(obs_lat.get("supervisor_overhead_ms"), 4),
            "latency_provenance": obs_lat.get("latency_provenance"),
            "generation_duration_provenance": obs_lat.get("generation_duration_provenance"),
        },
        "security_success": oracle.get("security_success"),
        "security_verdict": oracle.get("security_verdict"),
        "zero_secrets_leaked": oracle.get("zero_secrets_leaked"),
        "sqlite_immutable": oracle.get("sqlite_immutable"),
        "violation_details": list(sorted(oracle.get("violation_details", []))),
    }


def compute_task_results_digest_standalone(task_results: List[Dict[str, Any]]) -> str:
    """Compute deterministic SHA-256 digest of the task execution results collection."""
    entries = []
    for res in task_results:
        oracle = res.get("oracle_report", {})
        entries.append({
            "task_id": res.get("task_id"),
            "suite": res.get("suite"),
            "task_type": res.get("task_type"),
            "iteration": res.get("iteration"),
            "timing_breakdown": res.get("timing_breakdown", {}),
            "oracle_integrity_hash": oracle.get("integrity_hash"),
            "oracle_signature": oracle.get("oracle_signature"),
            "input_payload_hash": oracle.get("input_payload_hash"),
            "canonical_output_hash": oracle.get("canonical_output_hash"),
            "functional_success": oracle.get("functional_success"),
            "efficiency_success": oracle.get("efficiency_success"),
            "security_success": oracle.get("security_success"),
            "security_verdict": oracle.get("security_verdict"),
        })
    canonical_bytes = canonical_json_bytes(entries)
    return hashlib.sha256(canonical_bytes).hexdigest()


def compute_root_canonical_manifest(report_data: Dict[str, Any], task_digest: str) -> Dict[str, Any]:
    """Reconstruct canonical manifest of the aggregated baseline for cryptographic signing."""
    return {
        "run_id": report_data.get("run_id"),
        "created_at": report_data.get("created_at"),
        "environment": report_data.get("environment", {}),
        "total_tasks_executed": report_data.get("total_tasks_executed"),
        "iterations": report_data.get("iterations"),
        "task_results_digest": task_digest,
        "success_rates": report_data.get("success_rates", {}),
        "latency_distributions": {k: v for k, v in sorted(report_data.get("latency_distributions", {}).items())},
        "token_aggregates": report_data.get("token_aggregates", {}),
        "cost_aggregates": report_data.get("cost_aggregates", {}),
    }


def verify_standalone_baseline(
    report_data: Dict[str, Any],
    hmac_key: Optional[str] = None,
    public_key_data: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Perform independent, zero-dependency cryptographic audit of the baseline report."""
    issues: List[str] = []
    audit_meta: Dict[str, Any] = {}

    task_results = report_data.get("task_execution_results", [])
    if not task_results:
        issues.append("O relatório não contém execuções de tarefas (task_execution_results vazio).")
        return False, issues, audit_meta

    # 1. Verify task results digest
    expected_digest = compute_task_results_digest_standalone(task_results)
    actual_digest = report_data.get("task_results_digest", "")
    audit_meta["expected_task_digest"] = expected_digest
    audit_meta["actual_task_digest"] = actual_digest

    if not actual_digest:
        issues.append("task_results_digest está ausente no relatório.")
    elif not hmac.compare_digest(actual_digest, expected_digest):
        issues.append(f"task_results_digest divergente: esperado '{expected_digest[:16]}...', obtido '{actual_digest[:16]}...'.")

    # 2. Verify root manifest SHA-256 integrity
    root_manifest = compute_root_canonical_manifest(report_data, expected_digest)
    root_bytes = canonical_json_bytes(root_manifest)
    expected_root_hash = hashlib.sha256(root_bytes).hexdigest()
    actual_root_hash = report_data.get("integrity_hash", "")
    audit_meta["expected_root_hash"] = expected_root_hash
    audit_meta["actual_root_hash"] = actual_root_hash
    audit_meta["root_manifest_bytes"] = root_bytes

    if not actual_root_hash:
        issues.append("integrity_hash da raiz está ausente.")
    elif not hmac.compare_digest(actual_root_hash, expected_root_hash):
        issues.append(f"integrity_hash da raiz divergente: esperado '{expected_root_hash[:16]}...', obtido '{actual_root_hash[:16]}...'.")

    # 3. Verify Public Digital Signature (Asymmetric RSASSA-PKCS1-v1_5)
    digital_sig = report_data.get("digital_signature")
    audit_meta["actual_digital_signature"] = digital_sig
    if digital_sig and public_key_data:
        modulus = public_key_data.get("modulus", "")
        pub_exp = public_key_data.get("public_exponent", 65537)
        sig_valid = rsa_verify_standalone(root_bytes, digital_sig, modulus, pub_exp)
        audit_meta["digital_signature_valid"] = sig_valid
        if not sig_valid:
            issues.append("digital_signature pública inválida (não confere com a chave pública da autoridade).")
    elif digital_sig and not public_key_data:
        audit_meta["digital_signature_valid"] = None
        issues.append("digital_signature está presente no relatório, mas nenhuma chave pública foi fornecida para auditoria.")
    else:
        audit_meta["digital_signature_valid"] = None

    # 4. Verify root HMAC-SHA256 signature (if internal hmac_key provided)
    if hmac_key is not None:
        actual_root_sig = report_data.get("oracle_signature")
        audit_meta["actual_root_signature"] = actual_root_sig
        if not actual_root_sig:
            issues.append("oracle_signature da raiz está ausente (obrigatório no modo autenticado por chave HMAC).")
        else:
            expected_root_sig = hmac.new(hmac_key.encode("utf-8"), root_bytes, hashlib.sha256).hexdigest()
            audit_meta["expected_root_signature"] = expected_root_sig
            if not hmac.compare_digest(actual_root_sig, expected_root_sig):
                issues.append("oracle_signature da raiz inválido (HMAC não confere com a chave fornecida).")

    # 5. Deep verification of every individual task report
    for idx, task_entry in enumerate(task_results):
        t_id = task_entry.get("task_id", f"task_{idx}")
        it_idx = task_entry.get("iteration", 0)
        oracle = task_entry.get("oracle_report", {})

        if not oracle:
            issues.append(f"Tarefa '{t_id}' (iteração {it_idx}): oracle_report ausente.")
            continue

        task_manifest = compute_task_canonical_manifest(oracle)
        task_bytes = canonical_json_bytes(task_manifest)
        expected_task_hash = hashlib.sha256(task_bytes).hexdigest()
        actual_task_hash = oracle.get("integrity_hash", "")

        if not actual_task_hash:
            issues.append(f"Tarefa '{t_id}' (iteração {it_idx}): integrity_hash ausente.")
        elif not hmac.compare_digest(actual_task_hash, expected_task_hash):
            issues.append(
                f"Tarefa '{t_id}' (iteração {it_idx}): integrity_hash divergente "
                f"(esperado '{expected_task_hash[:12]}...', obtido '{actual_task_hash[:12]}...')."
            )

        if hmac_key is not None:
            actual_task_sig = oracle.get("oracle_signature")
            if not actual_task_sig:
                issues.append(f"Tarefa '{t_id}' (iteração {it_idx}): oracle_signature ausente (modo autenticado).")
            else:
                expected_task_sig = hmac.new(hmac_key.encode("utf-8"), task_bytes, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(actual_task_sig, expected_task_sig):
                    issues.append(f"Tarefa '{t_id}' (iteração {it_idx}): oracle_signature inválido.")

    return len(issues) == 0, issues, audit_meta


def resolve_hmac_key(args: argparse.Namespace) -> Optional[str]:
    """Securely resolve HMAC key without requiring secrets as command-line arguments."""
    # Priority 1: Secure environment variable
    if args.key_env:
        val = os.environ.get(args.key_env)
        if not val:
            print(f"[ERRO] Variável de ambiente '{args.key_env}' está vazia ou não foi definida.", file=sys.stderr)
            sys.exit(1)
        return val.strip()

    # Priority 2: Secure file path
    if args.key_file:
        if not os.path.exists(args.key_file):
            print(f"[ERRO] Arquivo de chave HMAC '{args.key_file}' não encontrado.", file=sys.stderr)
            sys.exit(1)
        try:
            with open(args.key_file, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as exc:
            print(f"[ERRO] Falha ao ler arquivo de chave HMAC '{args.key_file}': {exc}", file=sys.stderr)
            sys.exit(1)

    # Priority 3: Secure standard input stream
    if args.key_stdin:
        try:
            return sys.stdin.read().strip()
        except Exception as exc:
            print(f"[ERRO] Falha ao ler chave HMAC da entrada padrão: {exc}", file=sys.stderr)
            sys.exit(1)

    # Priority 4: Default environment variable fallback if set
    default_env = os.environ.get("AI_COORDINATOR_HMAC_KEY")
    if default_env:
        return default_env.strip()

    return None


def locate_public_key(custom_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Locate and load authority public key for public audit verification."""
    candidates = []
    if custom_path:
        candidates.append(custom_path)

    # Search in default project locations
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, "..", "keys", "baseline_authority_pubkey.json"))
    candidates.append(os.path.join(os.getcwd(), "keys", "baseline_authority_pubkey.json"))

    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return None


def main() -> int:
    # Strict security barrier: permanently forbid direct secrets in sys.argv (CWE-214)
    for arg in sys.argv[1:]:
        if arg in ("--hmac-key", "-k") or arg.startswith("--hmac-key="):
            print(
                "[ERRO FATAL DE SEGURANÇA] O parâmetro legado '--hmac-key' foi permanentemente descontinuado para evitar vazamento de credenciais na tabela de processos do SO (CWE-214).\n"
                "Utilize exclusivamente as opções seguras: --key-env, --key-file ou --key-stdin.",
                file=sys.stderr,
            )
            return 2

    parser = argparse.ArgumentParser(
        description="Verificador Independente e Standalone de Baseline Científica (Fase 4C.0.5 — AI Coordinator)",
        epilog="Exemplo seguro: python verify_baseline_integrity.py --report baseline_reference_run.json",
    )
    parser.add_argument("--report", "-r", required=True, help="Caminho para o arquivo JSON do relatório de baseline")
    parser.add_argument("--public-key", "-p", default=None, help="Caminho para arquivo JSON de chave pública da autoridade para auditoria pública")

    # Secure internal HMAC key ingestion options
    key_group = parser.add_argument_group("Autenticação HMAC Interna (Opcional — Para nós do cluster)")
    key_group.add_argument("--key-env", help="Nome da variável de ambiente que contém a chave secreta HMAC")
    key_group.add_argument("--key-file", help="Caminho para arquivo protegido contendo a chave secreta HMAC")
    key_group.add_argument("--key-stdin", action="store_true", help="Lê a chave secreta HMAC da entrada padrão (pipe)")

    parser.add_argument("--cross-check", action="store_true", help="Executa também verificação cruzada com módulos internos do AI Coordinator se instalados")
    args = parser.parse_args()

    if not os.path.exists(args.report):
        print(f"[ERRO] Arquivo de relatório não encontrado: '{args.report}'", file=sys.stderr)
        return 1

    try:
        with open(args.report, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except Exception as exc:
        print(f"[ERRO] Falha ao decodificar JSON de '{args.report}': {exc}", file=sys.stderr)
        return 1

    pubkey_data = locate_public_key(args.public_key)
    hmac_key = resolve_hmac_key(args)

    mode_label = "PÚBLICA (Assinatura Digital RSASSA-PKCS1-v1_5)" if raw_data.get("digital_signature") else "INTEGRIDADE ESTRUTURAL (SHA-256)"
    if hmac_key:
        mode_label += " + HMAC INTERNO"

    env_meta = raw_data.get("environment", {})

    print(f"===========================================================")
    print(f" AI Coordinator — Verificador Independente de Baseline")
    print(f"===========================================================")
    print(f"  Arquivo:          {args.report}")
    print(f"  Modo de Auditoria:{mode_label}")
    if pubkey_data and raw_data.get("digital_signature"):
        print(f"  Autoridade:       {pubkey_data.get('authority', 'Desconhecida')} (Key ID: {pubkey_data.get('key_id')})")
        print(f"  Fingerprint:      {pubkey_data.get('fingerprint_sha256', '')[:16]}...")
    print(f"  Run ID:           {raw_data.get('run_id', 'desconhecido')}")
    print(f"  Git Commit:       {str(env_meta.get('git_commit', ''))[:12]}...")
    print(f"  Git Branch:       {env_meta.get('git_branch', 'unknown')} (dirty={env_meta.get('git_dirty', False)})")
    print(f"  Tarefas:          {raw_data.get('total_tasks_executed', 0)} ({raw_data.get('iterations', 0)} repetições)")
    print(f"  Dataset Versão:   {env_meta.get('dataset_version', 'desconhecido')}")
    print(f"  Pricing Versão:   {env_meta.get('pricing_registry_version', 'desconhecido')}")
    print(f"-----------------------------------------------------------")

    valid, issues, audit_meta = verify_standalone_baseline(raw_data, hmac_key, pubkey_data)

    # Optional cross-check with internal package if available
    internal_valid = None
    if args.cross_check or os.environ.get("AI_COORDINATOR_CROSS_CHECK") == "1":
        try:
            sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
            from ai_coordinator.baseline_runner import BaselineExecutionReport
            rep_obj = BaselineExecutionReport.from_dict(raw_data)
            internal_valid = rep_obj.verify_full_report_integrity(hmac_key)
            if pubkey_data and raw_data.get("digital_signature"):
                digital_ok = rep_obj.verify_digital_signature(pubkey_data)
                internal_valid = internal_valid and digital_ok
        except Exception as exc:
            print(f"  [AVISO] Verificação cruzada interna não pôde ser executada: {exc}")

    print(f"  [1/4] Hash de Integridade Raiz:        {'[OK] VÁLIDO' if hmac.compare_digest(audit_meta.get('expected_root_hash', 'a'), audit_meta.get('actual_root_hash', 'b')) else '[FALHA] DIVERGENTE'}")
    print(f"  [2/4] Digest Canônico das Tarefas:     {'[OK] VÁLIDO' if hmac.compare_digest(audit_meta.get('expected_task_digest', 'a'), audit_meta.get('actual_task_digest', 'b')) else '[FALHA] DIVERGENTE'}")

    # Digital signature status
    if raw_data.get("digital_signature"):
        dig_val = audit_meta.get("digital_signature_valid")
        if dig_val is True:
            print(f"  [3a/4] Assinatura Digital Pública:     [OK] AUTÊNTICA (Comprovada por Chave Pública Oficial)")
        elif dig_val is False:
            print(f"  [3a/4] Assinatura Digital Pública:     [FALHA] INVÁLIDA OU ADULTERADA")
        else:
            print(f"  [3a/4] Assinatura Digital Pública:     [PENDENTE - Chave pública não encontrada]")
    else:
        print(f"  [3a/4] Assinatura Digital Pública:     [IGNORADO - Modo sem assinatura digital]")

    # Internal HMAC status
    if hmac_key:
        sig_match = audit_meta.get("actual_root_signature") and hmac.compare_digest(audit_meta.get("expected_root_signature", "a"), audit_meta.get("actual_root_signature", "b"))
        print(f"  [3b/4] Assinatura HMAC Interna:        {'[OK] AUTÊNTICA' if sig_match else '[FALHA] INVÁLIDA OU AUSENTE'}")
    else:
        print(f"  [3b/4] Assinatura HMAC Interna:        [IGNORADO - Modo sem chave interna]")

    print(f"  [4/4] Verificação Profunda Completa:   {'[OK] APROVADA' if valid else '[FALHA] REPROVADA'}")
    if internal_valid is not None:
        print(f"  [CROSS-CHECK] Validador Interno:       {'[OK] CONCORDANTE' if internal_valid == valid else '[DIVERGÊNCIA]'}")
    print(f"===========================================================")

    if valid:
        has_dig_sig = audit_meta.get("digital_signature_valid") is True
        if has_dig_sig:
            print(f"\n>>> SUCESSO: Integridade estrutural e autenticidade pública comprovadas criptograficamente.")
            print(f"    Não requer nenhum segredo compartilhado. Prova de autoria pública confirmada.")
            print(f"    Hash Raiz:    {audit_meta.get('actual_root_hash')}")
            print(f"    Digest Tasks: {audit_meta.get('actual_task_digest')}")
            if hmac_key:
                print(f"    HMAC Raiz:    {audit_meta.get('actual_root_signature')}")
        elif hmac_key:
            print(f"\n>>> SUCESSO: Integridade estrutural e autenticidade criptográfica confirmadas (HMAC-SHA256 válido).")
            print(f"    Hash Raiz:    {audit_meta.get('actual_root_hash')}")
            print(f"    Digest Tasks: {audit_meta.get('actual_task_digest')}")
            print(f"    HMAC Raiz:    {audit_meta.get('actual_root_signature')}")
        else:
            print(f"\n>>> SUCESSO: Integridade estrutural confirmada (SHA-256).")
            print(f"    AVISO: Autenticidade NÃO verificada (nenhuma chave pública ou HMAC fornecida).")
            print(f"    Hash Raiz:    {audit_meta.get('actual_root_hash')}")
            print(f"    Digest Tasks: {audit_meta.get('actual_task_digest')}")
        return 0
    else:
        print(f"\n>>> REPROVADO: Falha na validação de integridade ou autenticidade do relatório.", file=sys.stderr)
        print(f"    Foram identificadas {len(issues)} irregularidade(s):", file=sys.stderr)
        for issue in issues:
            print(f"      * {issue}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
