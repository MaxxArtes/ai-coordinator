"""Phase 4D.4.3 tests: AI Providers telemetry (quotas, 5h/weekly windows, tokens, costs, health, IPC, fail-closed)."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.auth.authorizer import DesktopAuthorizer  # noqa: E402
from desktop.auth.profile import DesktopProfileManager  # noqa: E402
from desktop.ipc.protocol import DataOrigin, MetricProvenance, RPCErrorCode  # noqa: E402
from desktop.ipc.providers_telemetry import collect_providers_telemetry  # noqa: E402
from desktop.ipc.server import BaseTransport, SecureIPCServer  # noqa: E402
from desktop.terminal.manager import TerminalManager  # noqa: E402


class _DummyTransport(BaseTransport):
    def read_message(self):
        return None

    def write_message(self, _message: str) -> bool:
        return True

    def log_diagnostic(self, _text: str) -> None:
        pass


def _create_sample_sqlite(db_path: Path, now_sec: float) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE route_health (key TEXT PRIMARY KEY, client TEXT, provider TEXT, model TEXT, "
        "kind TEXT, detail TEXT, observed REAL, retry_at REAL, expires REAL)"
    )
    conn.execute(
        "CREATE TABLE runs (id TEXT, client TEXT, model TEXT, usage TEXT)"
    )
    conn.execute(
        "CREATE TABLE cognitive_telemetry (id INTEGER PRIMARY KEY, provider TEXT, model_name TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER, "
        "total_tokens INTEGER, tokens_provenance TEXT, total_cost_usd REAL, cost_provenance TEXT)"
    )
    conn.execute(
        "INSERT INTO route_health VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("client|claude", "claude", "claude", "sonnet", "success", "ok", now_sec - 10, 0.0, now_sec + 590),
    )
    conn.execute(
        "INSERT INTO route_health VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "provider|opencode|openrouter",
            "opencode",
            "openrouter",
            "openrouter/test",
            "rate_limit",
            "Authorization Bearer sk-ant-secretvalue1234567890123456 rate limited",
            now_sec - 5,
            now_sec + 120,
            now_sec + 120,
        ),
    )
    # Claude run reporting input, output, cache read/creation and reasoning tokens
    claude_usage = json.dumps(
        {
            "input_tokens": 1200,
            "output_tokens": 350,
            "cache_read_input_tokens": 4000,
            "cache_creation_input_tokens": 500,
            "reasoning_tokens": 150,
            "total_cost_usd": 0.0125,
        }
    )
    conn.execute("INSERT INTO runs VALUES ('r1', 'claude', 'sonnet', ?)", (claude_usage,))
    # OpenCode routing through OpenRouter credits OpenRouter (never double-counted)
    or_usage = json.dumps({"input_tokens": 800, "output_tokens": 200, "cost": 0.0})
    conn.execute("INSERT INTO runs VALUES ('r2', 'opencode', 'openrouter/cohere/north-mini-code:free', ?)", (or_usage,))
    # Codex run with NO usage tokens reported -> provenance stays unknown
    conn.execute("INSERT INTO runs VALUES ('r3', 'codex', 'gpt-6-luna', '{}')")
    # Cognitive telemetry entry for agy
    conn.execute(
        "INSERT INTO cognitive_telemetry (provider, model_name, input_tokens, output_tokens, cache_read_tokens, "
        "cache_creation_tokens, total_tokens, tokens_provenance, total_cost_usd, cost_provenance) "
        "VALUES ('agy', 'gemini-3.8-flash-low', 600, 120, 0, 0, 720, 'measured', NULL, 'unknown')"
    )
    conn.commit()
    conn.close()


def test_01_inventory_and_honest_defaults_when_no_local_sources_exist() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        snap = collect_providers_telemetry(
            profile_name="operador-1",
            vault_keys=[],
            data_dir=Path(tmp),
            agents_data={"agents": [{"client": "claude", "available": True}]},
            routes_data={"routes": []},
            now_ms=1_700_000_000_000,
        )
        assert snap["schema_version"] == 1
        assert snap["profile_name"] == "operador-1"
        assert snap["provenance"] == MetricProvenance.UNKNOWN.value
        assert snap["data_origin"] == DataOrigin.OFFLINE_CACHE.value
        by_id = {p["provider"]: p for p in snap["providers"]}
        assert {"claude", "codex", "agy", "opencode", "openrouter"} <= set(by_id)
        for p in by_id.values():
            assert p["windows"]["five_hour"]["used_percent"] is None
            assert p["windows"]["five_hour"]["provenance"] == "unknown"
            assert p["windows"]["weekly"]["used_percent"] is None
            assert p["windows"]["weekly"]["provenance"] == "unknown"
            assert p["tokens"]["total_tokens"] is None
            assert p["tokens"]["provenance"] == "unknown"


def test_02_sqlite_tokens_costs_health_and_secret_redaction() -> None:
    now_ms = 1_700_000_000_000
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        data_dir = Path(tmp)
        _create_sample_sqlite(data_dir / "coordination.sqlite", now_ms / 1000.0)
        snap = collect_providers_telemetry(
            profile_name="default",
            vault_keys=["OPENROUTER_API_KEY"],
            data_dir=data_dir,
            coordinator_budget={"max_steps": 40, "max_elapsed_seconds": 1800},
            agents_data={"agents": [{"client": "claude", "available": True}]},
            routes_data={"routes": []},
            now_ms=now_ms,
        )
        assert snap["sources"]["sqlite_available"] is True
        by_id = {p["provider"]: p for p in snap["providers"]}

        claude = by_id["claude"]
        assert claude["availability_status"] == "available"
        assert claude["tokens"]["input_tokens"] == 1200
        assert claude["tokens"]["output_tokens"] == 350
        assert claude["tokens"]["cache_read_tokens"] == 4000
        assert claude["tokens"]["cache_creation_tokens"] == 500
        assert claude["tokens"]["reasoning_tokens"] == 150
        assert claude["tokens"]["total_tokens"] == 6200
        assert claude["tokens"]["provenance"] == "measured"
        assert claude["cost_credits"]["spent_usd"] == pytest.approx(0.0125)
        assert claude["local_limit"]["max_steps"] == 40
        assert claude["models"][0]["model"] == "sonnet"

        openrouter = by_id["openrouter"]
        assert openrouter["has_vault_key"] is True
        assert openrouter["availability_status"] == "rate_limit"
        assert "sk-ant-secretvalue" not in (openrouter["availability_detail"] or "")
        assert "[REDACTED_SECRET]" in (openrouter["availability_detail"] or "")
        assert openrouter["tokens"]["total_tokens"] == 1000

        # Codex had no tokens reported -> never faked as 0
        assert by_id["codex"]["tokens"]["total_tokens"] is None
        assert by_id["codex"]["tokens"]["provenance"] == "unknown"


def test_03_quota_snapshot_5h_weekly_api_credits_expiration_and_test_fixture_segregation() -> None:
    now_ms = 1_700_000_500_000
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        data_dir = Path(tmp)
        qdoc = {
            "data_origin": "test_fixture",
            "observed_at_unix_ms": now_ms - 600_000,  # 10 min ago -> expired if ttl=300s!
            "ttl_ms": 300_000,
            "providers": {
                "claude": {
                    "quota_kind": "subscription_quota",
                    "windows": {
                        "five_hour": {"used_percent": 38.0, "resets_at_iso": "2026-11-24T19:00:00Z", "provenance": "measured"},
                        "weekly": {"used_percent": 64.0, "resets_at_iso": "2026-11-30T00:00:00Z", "provenance": "measured"},
                    },
                },
                "openrouter": {
                    "quota_kind": "financial_credit",
                    "cost_credits": {"balance_usd": 12.5, "credits_remaining_usd": 12.5, "spent_usd": 1.25, "provenance": "measured"},
                },
                "opencode": {
                    "quota_kind": "api_rate_limit",
                    "api_limits": {"requests_remaining": 95, "requests_limit": 100, "tokens_remaining": 40000, "tokens_limit": 50000},
                },
                "codex": {
                    "windows": {
                        "five_hour": {"used_percent": 999.0, "provenance": "measured"},  # invalid > 100 -> fail closed!
                    }
                },
            },
        }
        (data_dir / "provider_quotas.json").write_text(json.dumps(qdoc), encoding="utf-8")

        snap = collect_providers_telemetry(
            profile_name="diag",
            vault_keys=[],
            data_dir=data_dir,
            agents_data={"agents": []},
            routes_data={"routes": []},
            now_ms=now_ms,
        )
        # Fixture origin propagates to envelope so UI never mistakes it for production
        assert snap["data_origin"] == DataOrigin.TEST_FIXTURE.value
        assert snap["stale"] is True
        by_id = {p["provider"]: p for p in snap["providers"]}

        c = by_id["claude"]
        assert c["windows"]["five_hour"]["used_percent"] == 38.0
        assert c["windows"]["five_hour"]["remaining_percent"] == 62.0
        assert c["windows"]["five_hour"]["stale"] is True
        assert c["windows"]["weekly"]["used_percent"] == 64.0
        assert c["windows"]["weekly"]["remaining_percent"] == 36.0

        assert by_id["openrouter"]["quota_kind"] == "financial_credit"
        assert by_id["openrouter"]["cost_credits"]["balance_usd"] == 12.5
        assert by_id["opencode"]["quota_kind"] == "api_rate_limit"
        assert by_id["opencode"]["api_limits"]["requests_remaining"] == 95

        # Out-of-range percentage rejected fail-closed to unknown
        assert by_id["codex"]["windows"]["five_hour"]["used_percent"] is None
        assert by_id["codex"]["windows"]["five_hour"]["provenance"] == "unknown"


def test_04_corrupted_files_and_ipc_auth_contract() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        (root / "coordination.sqlite").write_text("NOT A SQLITE FILE", encoding="utf-8")
        (root / "provider_quotas.json").write_text("{corrupt json", encoding="utf-8")
        snap = collect_providers_telemetry(data_dir=root, agents_data={"agents": []}, routes_data={"routes": []})
        assert snap["sources"]["sqlite_available"] is False
        assert snap["sources"]["quota_snapshot_available"] is False
        assert snap["notice"] is not None

        profiles = DesktopProfileManager(root_dir=root / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=profiles)
        token = authorizer.issue_session("default")
        server = SecureIPCServer(
            transport=_DummyTransport(),
            terminal_manager=TerminalManager(authorizer=authorizer),
            session_token=token,
        )
        unauth = server.handle_raw_payload('{"jsonrpc":"2.0","id":1,"method":"telemetry.providers_snapshot","params":{}}')
        assert unauth is not None and unauth.error["code"] == RPCErrorCode.AUTH_ERROR.value

        ok = server.handle_raw_payload(
            f'{{"jsonrpc":"2.0","id":2,"session_token":"{token}","method":"telemetry.providers_snapshot","params":{{}}}}'
        )
        assert ok is not None and ok.error is None and ok.result is not None
        assert len(ok.result["providers"]) >= 5
