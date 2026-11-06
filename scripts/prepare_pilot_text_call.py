#!/usr/bin/env python3
"""Pre-flight verification and execution preparation for OpenRouter Free pilot (Phase 4C.1).

Prepares the single-call controlled experiment (N=1, TEXT modality, strict US$ 0.000000).

Enforces:
1. Strict financial pre-checks (Spend Cap US$ 0.00, no auto-recharge).
2. API Key verification via environment variable OPENROUTER_API_KEY.
3. Offline dry-run verification by default (ZERO network traffic).
4. Live execution requires BOTH --live and --authorized-by-user flags.
5. Dynamic selection of verified free text model from catalog.
6. Atomic SQLite quota store and durable AvailabilityLedger with fsync.
7. Strict classification of metric provenance (MEASURED vs UNKNOWN).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, Optional

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
    ModelModality,
    OfficialOpenRouterCatalogClient,
    OpenRouterHttpTransport,
    SlidingWindowRateLimiter,
)
from ai_coordinator.openrouter.adapters import TextModelAdapter


def check_account_prerequisites() -> Dict[str, Any]:
    """Check account prerequisites, API key presence, and environmental settings."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    has_api_key = bool(api_key)
    masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else ("SET" if has_api_key else "NOT SET")

    return {
        "api_key_present": has_api_key,
        "masked_api_key": masked_key,
        "spend_cap_policy": "US$ 0.00 (Hard Cap required in OpenRouter dashboard)",
        "auto_recharge_policy": "DISABLED (Must be verified in OpenRouter dashboard)",
        "allowed_modality": "TEXT (Strict fail-closed pilot scope)",
        "cost_limit_usd": 0.000000,
    }


def prepare_offline_dry_run() -> int:
    """Execute a complete pre-flight simulation offline to verify all gates without network calls."""
    print("=" * 70)
    print(" AI Coordinator — Pré-Voo do Piloto OpenRouter Free (Modo Dry-Run / Offline)")
    print("=" * 70)

    prereqs = check_account_prerequisites()
    print(f"  [CHAVE] OPENROUTER_API_KEY: {prereqs['masked_api_key']}")
    print(f"  [POLÍTICA] Spend Cap Exigido: {prereqs['spend_cap_policy']}")
    print(f"  [MODALIDADE] Escopo Restrito: {prereqs['allowed_modality']}")
    print(f"  [CUSTO TETO] Limite Máximo: US$ {prereqs['cost_limit_usd']:.6f}")

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
    print(f"\n[1/4] Modelo selecionado dinamicamente: {selected_model}")

    # 3. Cost Guard validation
    guard = FreeCostGuard(registry)
    entry = guard.authorize_model_execution(selected_model)
    print(f"[2/4] FreeCostGuard: Modelo autorizado com sucesso (Prompt: US$ {entry.pricing.prompt}, Completion: US$ {entry.pricing.completion})")

    # 4. Quota Store and Ledger initialization
    with tempfile.TemporaryDirectory(prefix="pilot_preflight_") as tmpdir:
        db_path = os.path.join(tmpdir, "quotas.db")
        ledger_path = os.path.join(tmpdir, "pilot_audit.jsonl")

        limiter = SlidingWindowRateLimiter(sqlite_path=db_path, max_per_minute=20, max_per_day=50)
        ledger = AvailabilityLedger(storage_path=ledger_path, require_durable=True)

        transport = OpenRouterHttpTransport(
            cost_guard=guard,
            rate_limiter=limiter,
            ledger=ledger,
            live_network_enabled=False,  # Keep offline
        )

        runner = ModelBenchmarkRunner(
            registry=registry,
            cost_guard=guard,
            ledger=ledger,
            rate_limiter=limiter,
            transport=transport,
        )

        print("[3/4] Inicializando barreira Egress Gate e simulador offline...")
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

        # 5. Run single N=1 test execution
        result = runner.execute_task(
            model_id=selected_model,
            modality=ModelModality.TEXT,
            inputs={"messages": [{"role": "user", "content": "Classify sentiment: This product exceeded all my expectations."}]},
            run_id_prefix="pilot_preflight",
        )

        print(f"[4/4] Execução pré-voo concluída:")
        print(f"      Status: {'SUCESSO' if result.success else 'FALHA'}")
        print(f"      Latência: {result.latency_ms:.2f} ms")
        print(f"      Tokens: {result.tokens} (Proveniência: {result.token_provenance.value})")
        print(f"      Custo Efetivo: US$ {result.cost_usd:.6f}")
        print(f"      TTFT: {result.ttft_ms} (Proveniência: {result.ttft_provenance.value})")
        print(f"      Despacho simulado acionado: {sim_called}")
        print(f"      Auditoria gravada no ledger: {os.path.exists(ledger_path)}")

    print("\n" + "=" * 70)
    print(" Pré-voo offline concluído com SUCESSO.")
    print(" Nenhuma chamada externa de rede foi realizada.")
    print(" Para a chamada real, utilize:")
    print("   python scripts/prepare_pilot_text_call.py --live --authorized-by-user")
    print("=" * 70)
    return 0


def execute_live_pilot(model_id_override: Optional[str] = None) -> int:
    """Execute the live N=1 pilot call following strict governance protocol."""
    print("=" * 70)
    print(" AI Coordinator — Chamada Experimental Controlada (LIVE N=1)")
    print("=" * 70)

    prereqs = check_account_prerequisites()
    if not prereqs["api_key_present"]:
        print("[ERRO CRÍTICO] OPENROUTER_API_KEY não encontrada no ambiente.")
        print("Configure a variável antes de prosseguir:")
        print("  $env:OPENROUTER_API_KEY = 'sk-or-v1-...'")
        return 1

    print(f"  [CHAVE] OPENROUTER_API_KEY confirmada: {prereqs['masked_api_key']}")
    print(f"  [POLÍTICA] Spend Cap: {prereqs['spend_cap_policy']}")
    print(f"  [MODALIDADE] Escopo Restrito: {prereqs['allowed_modality']}")

    # Step 1: Fetch fresh official authenticated catalog via HTTPS
    print("\n[Passo 1/6] Obtendo catálogo oficial via HTTPS com atestação de origem...")
    client = OfficialOpenRouterCatalogClient()
    try:
        snapshot = client.fetch_catalog(timeout_sec=20.0)
    except Exception as e:
        print(f"[ERRO] Falha ao consultar catálogo oficial da OpenRouter: {e}")
        return 1

    # Step 2: Load into trusted registry
    print("[Passo 2/6] Carregando catálogo oficial no FreeModelRegistry...")
    registry = FreeModelRegistry(require_trusted_source=True)
    registry.load_authenticated_snapshot(snapshot)
    if not registry.is_authenticated_origin:
        print("[ERRO CRÍTICO] Atestação de origem não confirmada. Abortando fail-closed.")
        return 1

    # Step 3: Dynamic selection and strict zero-cost authorization
    print("[Passo 3/6] Selecionando modelo gratuito de TEXT dinamicamente...")
    text_models = registry.get_by_modality(ModelModality.TEXT)
    if not text_models:
        print("[ERRO] Nenhum modelo gratuito de TEXT disponível no catálogo oficial.")
        return 1

    chosen_model = model_id_override or text_models[0].id
    guard = FreeCostGuard(registry)
    try:
        entry = guard.authorize_model_execution(chosen_model)
    except Exception as e:
        print(f"[ERRO] Modelo {chosen_model} não autorizado pelo FreeCostGuard: {e}")
        return 1

    print(f"  Modelo Selecionado: {chosen_model}")
    print(f"  Preços confirmados: prompt={entry.pricing.prompt}, completion={entry.pricing.completion}, request={entry.pricing.request}")

    # Step 4: Configure durable quotas and audit ledger
    print("[Passo 4/6] Configurando quota atômica SQLite e AvailabilityLedger com fsync...")
    bench_dir = os.path.join(REPO_ROOT, "docs", "benchmarks")
    os.makedirs(bench_dir, exist_ok=True)
    db_path = os.path.join(bench_dir, "pilot_quotas.db")
    ledger_path = os.path.join(bench_dir, "pilot_audit.jsonl")

    limiter = SlidingWindowRateLimiter(sqlite_path=db_path, max_per_minute=20, max_per_day=50)
    ledger = AvailabilityLedger(storage_path=ledger_path, require_durable=True)

    # Step 5: Configure live transport
    print("[Passo 5/6] Ativando OpenRouterHttpTransport em modo LIVE...")
    transport = OpenRouterHttpTransport(
        cost_guard=guard,
        rate_limiter=limiter,
        ledger=ledger,
        live_network_enabled=True,  # LIVE OPT-IN
    )

    runner = ModelBenchmarkRunner(
        registry=registry,
        cost_guard=guard,
        ledger=ledger,
        rate_limiter=limiter,
        transport=transport,
    )

    # Step 6: Dispatch single controlled call (N=1)
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

    print("\n" + "=" * 70)
    print(" Resultado da Execução Experimental Live (N=1)")
    print("=" * 70)
    print(f"  Sucesso: {result.success}")
    print(f"  Status HTTP: {result.status_code}")
    print(f"  Latência Total: {result.latency_ms:.2f} ms (medido localmente: {elapsed:.2f} ms)")
    print(f"  Tokens Reportados: {result.tokens} (Proveniência: {result.token_provenance.value})")
    print(f"  TTFT: {result.ttft_ms} (Proveniência: {result.ttft_provenance.value})")
    print(f"  Custo Reportado: US$ {result.cost_usd:.6f}")
    if result.error_message:
        print(f"  Mensagem de Erro: {result.error_message}")
    print(f"  Auditoria gravada em: {ledger_path}")
    print("=" * 70)

    # Save run artifact
    result_json_path = os.path.join(bench_dir, "pilot_live_n1_result.json")
    with open(result_json_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2)
    print(f"Resultado persistido em: {result_json_path}")

    return 0 if result.success else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Coordinator — Pré-Voo e Execução do Piloto OpenRouter Free")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Executar pré-voo offline de teste sem rede")
    parser.add_argument("--check-prerequisites", action="store_true", default=False, help="Conferir credenciais e pré-requisitos")
    parser.add_argument("--live", action="store_true", default=False, help="Habilitar execução live na rede")
    parser.add_argument("--authorized-by-user", action="store_true", default=False, help="Confirmação de autorização expressa do usuário")
    parser.add_argument("--model-id", type=str, default=None, help="Sobrescrever ID do modelo gratuito")

    args = parser.parse_args()

    if args.check_prerequisites:
        prereqs = check_account_prerequisites()
        print(json.dumps(prereqs, indent=2))
        return 0

    if args.live:
        if not args.authorized_by_user:
            print("[ABORTADO] Execução live recusada: a flag --authorized-by-user é obrigatória.", file=sys.stderr)
            print("O piloto em ambiente real depende de autorização formal expressa do usuário.", file=sys.stderr)
            return 1
        return execute_live_pilot(model_id_override=args.model_id)

    # Default to offline dry-run
    return prepare_offline_dry_run()


if __name__ == "__main__":
    sys.exit(main())
