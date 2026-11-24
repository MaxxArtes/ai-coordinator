"""Phase 4D.4 r2 tests: local provider adapters (Claude, Codex, AGY, OpenCode, OpenRouter).

Synthetic fixtures only (tmp_path); nothing here reads the operator's real files. Covers numbers-only parsing,
read-only SQLite, byte/file limits, TTL and per-file caches, corrupted/missing sources, no process/network,
discovery without fixed user folders and the merge into the providers snapshot.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.auth.authorizer import DesktopAuthorizer  # noqa: E402
from desktop.auth.profile import DesktopProfileManager  # noqa: E402
from desktop.ipc import provider_adapters  # noqa: E402
from desktop.ipc.provider_adapters import (  # noqa: E402
    LOCAL_CACHE_TTL_MS,
    LocalProviderReader,
    LocalSourcePaths,
    discover_local_source_paths,
)
from desktop.ipc.providers_telemetry import collect_providers_telemetry  # noqa: E402
from desktop.ipc.server import BaseTransport, SecureIPCServer  # noqa: E402
from desktop.terminal.manager import TerminalManager  # noqa: E402

NOW_MS = 1_795_550_000_000
NOW_S = NOW_MS / 1000.0
SECRET_MARKERS = (
    "CONVERSA-SECRETA",
    "conteudo do assistente",
    "INSTRUCOES-SECRETAS",
    "TITULO-SECRETO",
    "sk-or-v1-SEGREDOOPENROUTER1234567890",
    "sk-ant-api03-SEGREDOANTHROPIC1234567890",
    "texto-da-esgotada",
    "hash-da-conta",
)


def _iso(ms: float) -> str:
    return dt.datetime.fromtimestamp(ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _touch(path: Path, age_s: float = 60.0) -> None:
    os.utime(path, (NOW_S - age_s, NOW_S - age_s))


def _write_json(path: Path, doc: Dict[str, Any], age_s: float = 60.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    _touch(path, age_s)
    return path


def _write_lines(path: Path, lines: List[Dict[str, Any]], age_s: float = 60.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    _touch(path, age_s)
    return path


def _claude_line(msg_id: str, model: str, usage: Dict[str, Any], at_ms: float) -> Dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": _iso(at_ms),
        "sessionId": "sessao-1",
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": "conteudo do assistente sk-ant-api03-SEGREDOANTHROPIC1234567890"}],
            "usage": usage,
        },
    }


def _claude_fixture(home: Path) -> None:
    projects = home / ".claude" / "projects" / "proj-a"
    _write_lines(
        projects / "sessao-1.jsonl",
        [
            {"type": "user", "message": {"role": "user", "content": "CONVERSA-SECRETA com a chave sk-ant-api03-SEGREDOANTHROPIC1234567890"}},
            _claude_line(
                "m1",
                "claude-test-opus",
                {
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 50,
                    "output_tokens_details": {"thinking_tokens": 20},
                },
                NOW_MS - 120_000,
            ),
            _claude_line(
                "m1",
                "claude-test-opus",
                {
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 50,
                    "output_tokens_details": {"thinking_tokens": 20},
                },
                NOW_MS - 119_000,
            ),
            _claude_line("syn", "<synthetic>", {"input_tokens": 0, "output_tokens": 0}, NOW_MS - 100_000),
            _claude_line(
                "m2",
                "claude-test-opus",
                {"input_tokens": 5, "output_tokens": 200, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0},
                NOW_MS - 90_000,
            ),
        ],
    )
    _write_lines(
        home / ".env-max" / ".claude" / "projects" / "proj-b" / "sessao-2.jsonl",
        [_claude_line("m3", "claude-test-sonnet", {"input_tokens": 1, "output_tokens": 1}, NOW_MS - 600_000)],
        age_s=600,
    )


def _quota_fixture(quota: Path, home: Path) -> None:
    _write_json(
        quota / "leitura-claude-carvalima.json",
        {
            "harness": "claude",
            "perfil": "carvalima",
            "observado_em": NOW_S - 60,
            "fonte": "statusline",
            "conta": "hash-da-conta",
            "userprofile": str(home),
            "session_id": "sessao-1",
            "janelas": [
                {"id": "five_hour", "rotulo": "janela de 5 h", "usado_pct": 24.0, "reset_epoch": NOW_S + 3600},
                {"id": "seven_day", "rotulo": "janela semanal", "usado_pct": 4.0, "reset_epoch": NOW_S + 86400},
            ],
            "sessoes": {"sessao-1": {"bruto": {"five_hour": [24.0, NOW_S + 3600]}, "visto": NOW_S - 60}},
            "esgotada": None,
        },
    )
    _write_json(
        quota / "leitura-claude-max.json",
        {
            "harness": "claude",
            "perfil": "max",
            "observado_em": NOW_S - 120,
            "janelas": [{"id": "five_hour", "usado_pct": 49.0, "reset_epoch": NOW_S + 1800}],
        },
    )
    _write_json(
        quota / "leitura-codex-padrao.json",
        {
            "harness": "codex",
            "perfil": "padrao",
            "observado_em": NOW_S - 3600,
            "janelas": [
                {"id": "w300", "usado_pct": 11.0, "reset_epoch": NOW_S + 7200},
                {"id": "w10080", "usado_pct": 2.0, "reset_epoch": NOW_S + 400000},
            ],
        },
    )
    _write_json(
        quota / "agy-quota-cache-padrao.json",
        {
            "observed_at": NOW_S - 30,
            "source": "usage",
            "buckets": {
                "gemini-weekly": {"group": "Gemini Models", "window": "weekly", "remaining_fraction": 0.5, "reset_time": _iso(NOW_MS + 3 * 86400_000), "disabled": False},
                "gemini-5h": {"group": "Gemini Models", "window": "5h", "remaining_fraction": 0.75, "reset_time": _iso(NOW_MS + 3600_000), "disabled": False},
                "3p-weekly": {"group": "Claude and GPT models", "window": "weekly", "remaining_fraction": 0, "reset_time": _iso(NOW_MS + 86400_000), "disabled": False},
                "3p-5h": {"group": "Claude and GPT models", "window": "5h", "remaining_fraction": 1, "reset_time": None, "disabled": True},
            },
            "esgotada": {"em": NOW_S - 100, "ate": NOW_S + 3600, "texto": "texto-da-esgotada", "modelo": "gemini-test-high"},
        },
    )
    _write_json(
        quota / "leitura-agy-padrao.json",
        {
            "harness": "agy",
            "perfil": "padrao",
            "observado_em": NOW_S - 30,
            "janelas": [
                {"id": "gemini-5h", "usado_pct": 25.0, "reset_epoch": NOW_S + 3600},
                {"id": "gemini-weekly", "usado_pct": 50.0, "reset_epoch": NOW_S + 3 * 86400},
            ],
        },
    )
    _write_json(
        quota / "agy-quota-cache-carvalima.json",
        {
            "observed_at": NOW_S - 2 * 86400,
            "buckets": {
                "gemini-5h": {"group": "Gemini Models", "window": "5h", "remaining_fraction": 0.1, "reset_time": _iso(NOW_MS - 3600_000), "disabled": False},
            },
        },
    )
    _write_json(
        quota / "leitura-opencode-padrao.json",
        {
            "harness": "opencode",
            "perfil": "padrao",
            "observado_em": NOW_S - 300,
            "janelas": [{"id": "openrouter-free-dia", "rotulo": "cota diaria OpenRouter :free", "usado_pct": 2, "reset_epoch": NOW_S + 5 * 3600}],
            "nota": "contagem local",
        },
    )


def _codex_fixture(codex_home: Path) -> None:
    day = dt.datetime.fromtimestamp(NOW_S, tz=dt.timezone.utc).date()
    folder = codex_home / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    _write_lines(
        folder / "rollout-a.jsonl",
        [
            {"type": "session_meta", "payload": {"base_instructions": "INSTRUCOES-SECRETAS", "model_provider": "openai"}},
            {"type": "turn_context", "payload": {"model": "gpt-test-codex", "cwd": "D:/projeto"}},
            {"type": "response_item", "payload": {"type": "message", "content": [{"type": "input_text", "text": "CONVERSA-SECRETA"}]}},
            {"timestamp": _iso(NOW_MS - 200_000), "type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": None}},
            {
                "timestamp": _iso(NOW_MS - 60_000),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 1000, "cached_input_tokens": 800, "cache_write_input_tokens": 0, "output_tokens": 50, "reasoning_output_tokens": 10, "total_tokens": 1050},
                        "last_token_usage": {"input_tokens": 400, "cached_input_tokens": 300, "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 420},
                        "model_context_window": 1000,
                    },
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": 22.0, "window_minutes": 300, "resets_at": NOW_S + 3600},
                        "secondary": {"used_percent": 3.0, "window_minutes": 10080, "resets_at": NOW_S + 500000},
                        "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                        "plan_type": "team",
                    },
                },
            },
        ],
        age_s=60,
    )
    _write_lines(
        folder / "rollout-b.jsonl",
        [
            {"type": "turn_context", "payload": {"model": "gpt-test-mini"}},
            {
                "timestamp": _iso(NOW_MS - 900_000),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10, "total_tokens": 110}},
                    "rate_limits": None,
                },
            },
        ],
        age_s=900,
    )


def _opencode_fixture(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE account (id TEXT PRIMARY KEY, email TEXT, access_token TEXT)")
    conn.execute("INSERT INTO account VALUES ('a1', 'pessoa@example.com', 'sk-or-v1-SEGREDOOPENROUTER1234567890')")
    for table in ("session_v2", "session"):
        conn.execute(
            f"CREATE TABLE {table} (id TEXT PRIMARY KEY, title TEXT, model TEXT, cost REAL, tokens_input INTEGER, "
            "tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER, "
            "time_created INTEGER, time_updated INTEGER)"
        )
    rows = [
        ("s1", json.dumps({"id": "big-pickle", "providerID": "opencode"}), 0.0, 100, 10, 5, 1000, 0, NOW_MS - 3600_000),
        ("s2", json.dumps({"id": "grok-test", "providerID": "xai"}), 0.5, 200, 20, 0, 0, 10, NOW_MS - 7200_000),
        ("s3", json.dumps({"id": "google/gemma-test:free", "providerID": "openrouter"}), 0.0, 50, 5, 0, 0, 0, NOW_MS - 3 * 3600_000),
        ("s4", json.dumps({"id": "old", "providerID": "opencode"}), 9.0, 9999, 9999, 0, 0, 0, NOW_MS - 10 * 86400_000),
    ]
    for sid, model, cost, t_in, t_out, t_reas, t_cr, t_cw, updated in rows:
        conn.execute(
            "INSERT INTO session_v2 VALUES (?, 'TITULO-SECRETO', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, model, cost, t_in, t_out, t_reas, t_cr, t_cw, updated, updated),
        )
    conn.execute(
        "INSERT INTO session VALUES ('s1', 'TITULO-SECRETO', ?, 0.0, 100, 10, 5, 1000, 0, ?, ?)",
        (rows[0][1], NOW_MS - 3600_000, NOW_MS - 3600_000),
    )
    conn.commit()
    conn.close()


def _paths(tmp_path: Path) -> LocalSourcePaths:
    home = tmp_path / "home"
    quota = tmp_path / "cota"
    _claude_fixture(home)
    _quota_fixture(quota, home)
    _codex_fixture(home / ".codex")
    _opencode_fixture(home / ".local" / "share" / "opencode" / "opencode.db")
    return LocalSourcePaths(
        quota_dir=quota,
        claude_dirs=(("carvalima", home / ".claude"), ("max", home / ".env-max" / ".claude")),
        codex_homes=(("padrao", home / ".codex"),),
        opencode_dbs=(("padrao", home / ".local" / "share" / "opencode" / "opencode.db"),),
    )


def _assert_no_leak(result: Any, tmp_path: Path) -> None:
    dumped = json.dumps(result, ensure_ascii=False)
    for marker in SECRET_MARKERS:
        assert marker not in dumped, marker
    assert str(tmp_path) not in dumped
    assert json.dumps(str(tmp_path))[1:-1] not in dumped


def test_claude_statusline_profiles_and_session_usage(tmp_path: Path) -> None:
    result = LocalProviderReader(_paths(tmp_path)).read(now_ms=NOW_MS)
    claude = result["providers"]["claude"]
    assert claude["available"] is True
    five, weekly = claude["windows"]["five_hour"], claude["windows"]["weekly"]
    assert five["used_percent"] == 24.0 and five["remaining_percent"] == 76.0
    assert five["provenance"] == "measured" and five["data_origin"] == "offline_cache"
    assert five["resets_at_iso"] == dt.datetime.fromtimestamp(NOW_S + 3600, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert five["stale"] is False and five["profile"] == "carvalima"
    assert weekly["used_percent"] == 4.0
    assert [a["profile"] for a in claude["accounts"]] == ["carvalima", "max"]
    max_account = claude["accounts"][1]
    assert max_account["windows"]["five_hour"]["used_percent"] == 49.0
    assert max_account["windows"]["weekly"]["provenance"] == "unknown"

    tokens = claude["tokens"]
    assert tokens["input_tokens"] == 16  # m1 counted once (split blocks), m2, m3; synthetic skipped
    assert tokens["output_tokens"] == 301
    assert tokens["cache_read_tokens"] == 3000
    assert tokens["cache_creation_tokens"] == 50
    assert tokens["reasoning_tokens"] == 20
    assert tokens["total_tokens"] == 3367
    assert tokens["provenance"] == "measured"
    assert claude["usage_scope"]["sessions"] == 2 and claude["usage_scope"]["truncated"] is False

    context = claude["context"]
    assert context["used_tokens"] == 2005  # last turn: input + cache_read + cache_creation
    assert context["limit_tokens"] is None and context["used_percent"] is None
    assert context["model"] == "claude-test-opus"
    assert {m["model"] for m in claude["models"]} == {"claude-test-opus", "claude-test-sonnet"}
    assert "<synthetic>" not in json.dumps(claude)
    _assert_no_leak(result, tmp_path)


def test_codex_token_count_rate_limits_context_and_plan(tmp_path: Path) -> None:
    codex = LocalProviderReader(_paths(tmp_path)).read(now_ms=NOW_MS)["providers"]["codex"]
    assert codex["windows"]["five_hour"]["used_percent"] == 22.0
    assert codex["windows"]["weekly"]["used_percent"] == 3.0
    assert codex["windows"]["five_hour"]["observed_at_unix_ms"] == NOW_MS - 60_000
    tokens = codex["tokens"]
    assert tokens["input_tokens"] == 300  # uncached: (1000 - 800) + (100 - 0)
    assert tokens["cache_read_tokens"] == 800
    assert tokens["output_tokens"] == 60
    assert tokens["reasoning_tokens"] == 10
    assert tokens["total_tokens"] == 1160
    assert tokens["provenance"] == "measured"
    assert codex["context"] == {
        "used_tokens": 420,
        "limit_tokens": 1000,
        "used_percent": 42.0,
        "model": "gpt-test-codex",
        "provenance": "measured",
        "observed_at_unix_ms": NOW_MS - 60_000,
    }
    assert codex["plan"]["plan_type"] == "team"
    assert codex["plan"]["credits_balance"] == 0.0 and codex["plan"]["has_credits"] is False
    assert {m["model"] for m in codex["models"]} == {"gpt-test-codex", "gpt-test-mini"}


def test_codex_rate_limits_null_falls_back_to_quota_reading_or_unknown(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    folder = next((tmp_path / "home" / ".codex" / "sessions").rglob("rollout-a.jsonl")).parent
    (folder / "rollout-a.jsonl").unlink()
    codex = LocalProviderReader(paths).read(now_ms=NOW_MS)["providers"]["codex"]
    assert codex["tokens"]["total_tokens"] == 110
    assert codex["windows"]["five_hour"]["used_percent"] == 11.0  # leitura-codex (codex exec gave rate_limits null)
    assert codex["plan"] is None
    session_source = next(s for s in codex["sources"] if s["kind"] == "session_logs")
    assert session_source["available"] is True and "rate_limits" in (session_source["reason"] or "")

    (paths.quota_dir / "leitura-codex-padrao.json").unlink()
    codex = LocalProviderReader(paths).read(now_ms=NOW_MS)["providers"]["codex"]
    assert "five_hour" not in codex["windows"] and "weekly" not in codex["windows"]
    assert codex["tokens"]["total_tokens"] == 110


def test_agy_groups_disabled_renewed_and_exhausted(tmp_path: Path) -> None:
    result = LocalProviderReader(_paths(tmp_path)).read(now_ms=NOW_MS)
    agy = result["providers"]["agy"]
    assert agy["windows"]["five_hour"]["used_percent"] == 25.0
    assert agy["windows"]["weekly"]["used_percent"] == 50.0
    assert "Gemini Models" in agy["windows"]["five_hour"]["label"]
    extras = {w["label"]: w for w in agy["extra_windows"]}
    assert extras["Claude and GPT models · semanal"]["used_percent"] == 100.0
    disabled = extras["Claude and GPT models · 5 h"]
    assert disabled["disabled"] is True and disabled["used_percent"] is None and disabled["provenance"] == "unknown"
    # quota-guard marks a 429 per model: other model groups may still have quota, so the provider is not blocked
    assert agy["exhausted"] is False
    assert agy["exhausted_model"] == "gemini-test-high" and agy["exhausted_until_iso"]
    padrao = next(a for a in agy["accounts"] if a["profile"] == "padrao")
    assert padrao["exhausted"] is True and padrao["exhausted_model"] == "gemini-test-high"
    renewed = next(a for a in agy["accounts"] if a["profile"] == "carvalima")
    assert renewed["windows"]["five_hour"]["used_percent"] is None
    assert renewed["windows"]["five_hour"]["provenance"] == "unknown"
    assert "renovada" in renewed["windows"]["five_hour"]["note"]
    assert renewed["stale"] is True
    assert agy["tokens"] is None and agy["context"] is None
    _assert_no_leak(result, tmp_path)


def test_opencode_and_openrouter_from_sqlite_strictly_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    db = paths.opencode_dbs[0][1]
    before_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    before_mtime = db.stat().st_mtime_ns
    before_files = sorted(os.listdir(db.parent))

    uris: List[str] = []
    real_connect = sqlite3.connect

    def spy_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        uris.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(provider_adapters.sqlite3, "connect", spy_connect)
    result = LocalProviderReader(paths).read(now_ms=NOW_MS)
    monkeypatch.undo()

    assert uris and all(u.startswith("file:") and u.endswith("?mode=ro") for u in uris)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before_hash
    assert db.stat().st_mtime_ns == before_mtime
    assert sorted(os.listdir(db.parent)) == before_files

    opencode = result["providers"]["opencode"]
    assert opencode["windows_supported"] is False
    assert opencode["windows"]["five_hour"]["supported"] is False
    assert opencode["windows"]["five_hour"]["used_percent"] is None
    assert opencode["tokens"]["input_tokens"] == 300
    assert opencode["tokens"]["cache_creation_tokens"] == 10
    assert opencode["tokens"]["total_tokens"] == 1345  # s1 + s2; s4 outside the window, s1 not counted twice
    assert opencode["cost"] == {"spent_usd": 0.5, "provenance": "measured"}
    assert {m["model"] for m in opencode["models"]} == {"opencode/big-pickle", "xai/grok-test"}

    openrouter = result["providers"]["openrouter"]
    assert openrouter["tokens"]["total_tokens"] == 55
    assert openrouter["cost"] == {"spent_usd": 0.0, "provenance": "measured"}
    assert [m["model"] for m in openrouter["models"]] == ["google/gemma-test:free"]
    daily = openrouter["extra_windows"][0]
    assert daily["label"] == "Cota diária :free" and daily["used_percent"] == 2.0
    assert openrouter["remote_lookup"]["enabled"] is False
    _assert_no_leak(result, tmp_path)


def test_opencode_older_message_schema_fallback(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    conn.execute(
        "INSERT INTO message VALUES ('u1', 's', ?, ?, ?)",
        (NOW_MS - 1000, NOW_MS - 1000, json.dumps({"role": "user", "text": "CONVERSA-SECRETA"})),
    )
    assistant = {
        "role": "assistant",
        "providerID": "openrouter",
        "modelID": "vendor/model-test:free",
        "cost": 0.01,
        "tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 4, "write": 0}},
    }
    conn.execute("INSERT INTO message VALUES ('a1', 's', ?, ?, ?)", (NOW_MS - 900, NOW_MS - 900, json.dumps(assistant)))
    conn.commit()
    conn.close()
    result = LocalProviderReader(LocalSourcePaths(opencode_dbs=(("padrao", db),))).read(now_ms=NOW_MS)
    openrouter = result["providers"]["openrouter"]
    assert openrouter["tokens"]["total_tokens"] == 17
    assert openrouter["cost"]["spent_usd"] == 0.01
    assert result["providers"]["opencode"]["available"] is False
    _assert_no_leak(result, tmp_path)


def test_corrupted_missing_and_unreadable_sources_fail_closed(tmp_path: Path) -> None:
    quota = tmp_path / "cota"
    quota.mkdir()
    (quota / "leitura-claude-carvalima.json").write_text("{corrompido", encoding="utf-8")
    (quota / "leitura-codex-padrao.json").write_text("[1, 2]", encoding="utf-8")
    (quota / "agy-quota-cache-padrao.json").write_bytes(b"\xff\xfe\x00garbage")
    projects = tmp_path / "claude" / "projects" / "p"
    projects.mkdir(parents=True)
    (projects / "s.jsonl").write_bytes(b'{"type": "assistant", "usage"\n\x00\xff not json "usage"\n{"type":"assistant","message":"x","usage":1}\n')
    _touch(projects / "s.jsonl")
    bad_db = tmp_path / "opencode.db"
    bad_db.write_text("NOT A SQLITE FILE", encoding="utf-8")
    paths = LocalSourcePaths(
        quota_dir=quota,
        claude_dirs=(("padrao", tmp_path / "claude"),),
        codex_homes=(("padrao", tmp_path / "sem-codex"),),
        opencode_dbs=(("padrao", bad_db), ("max", tmp_path / "ausente.db")),
    )
    result = LocalProviderReader(paths).read(now_ms=NOW_MS)
    for pid in ("claude", "codex", "agy", "opencode"):
        reading = result["providers"][pid]
        assert reading["available"] is False, pid
        assert reading["reason"], pid
        assert reading["tokens"] is None
    claude_status = result["providers"]["claude"]["sources"][0]
    assert "corrompido" in claude_status["reason"]
    assert "formato inesperado" in result["providers"]["codex"]["sources"][1]["reason"]
    assert "ilegível ou bloqueado" in result["providers"]["opencode"]["reason"]

    empty = LocalProviderReader(LocalSourcePaths()).read(now_ms=NOW_MS)
    for reading in empty["providers"].values():
        assert reading["available"] is False and reading["tokens"] is None
        assert reading["context"] is None and reading["cost"] is None


def test_read_limits_tail_file_count_and_lookback(tmp_path: Path) -> None:
    projects = tmp_path / "claude" / "projects" / "p"
    big_lines = [
        _claude_line(f"m{i}", "claude-test-opus", {"input_tokens": 1, "output_tokens": 1}, NOW_MS - 50_000 + i)
        for i in range(2000)
    ]
    _write_lines(projects / "grande.jsonl", big_lines, age_s=10)
    for index in range(5):
        _write_lines(projects / f"pequena-{index}.jsonl", big_lines[:1], age_s=100 + index)
    _write_lines(projects / "antiga.jsonl", big_lines[:1], age_s=5 * 86400)
    assert (projects / "grande.jsonl").stat().st_size > 64 * 1024

    reader = LocalProviderReader(
        LocalSourcePaths(claude_dirs=(("padrao", tmp_path / "claude"),)),
        max_files=3,
        max_tail_bytes=64 * 1024,
    )
    claude = reader.read(now_ms=NOW_MS)["providers"]["claude"]
    assert claude["usage_scope"]["sessions"] == 3
    assert claude["usage_scope"]["truncated"] is True
    assert claude["tokens"]["provenance"] == "estimated"  # lower bound: only the tail of the big file was read
    assert 0 < claude["tokens"]["input_tokens"] < 2000
    assert reader.stats["files_read"] == 3
    assert reader.stats["bytes_read"] <= 64 * 1024 * 3


def test_ttl_cache_and_per_file_cache(tmp_path: Path) -> None:
    clock = [NOW_MS]
    quota = tmp_path / "cota"
    reading = {"perfil": "padrao", "observado_em": NOW_S - 5, "janelas": [{"id": "five_hour", "usado_pct": 10.0, "reset_epoch": NOW_S + 3600}]}
    path = _write_json(quota / "leitura-claude-padrao.json", reading, age_s=5)
    _write_json(quota / "leitura-claude-max.json", dict(reading, perfil="max"), age_s=5)
    reader = LocalProviderReader(LocalSourcePaths(quota_dir=quota), clock_ms=lambda: clock[0])

    first = reader.read()
    assert first["providers"]["claude"]["windows"]["five_hour"]["used_percent"] == 10.0
    files_after_first = reader.stats["files_read"]

    reading["janelas"][0]["usado_pct"] = 55.0
    reading["observado_em"] = NOW_S + 1
    path.write_text(json.dumps(reading), encoding="utf-8")
    os.utime(path, (NOW_S + 1, NOW_S + 1))
    clock[0] = NOW_MS + 10_000
    assert reader.read() is first

    clock[0] = NOW_MS + LOCAL_CACHE_TTL_MS + 1
    second = reader.read()
    assert second is not first
    assert second["providers"]["claude"]["windows"]["five_hour"]["used_percent"] == 55.0
    assert reader.stats["files_read"] == files_after_first + 1  # the unchanged file came from the per-file cache
    assert reader.stats["collections"] == 2
    assert reader.read(force=True) is not second


def test_never_starts_processes_or_touches_the_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("processo ou rede não permitidos")

    paths = _paths(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = LocalProviderReader(paths).read(now_ms=NOW_MS)
    assert all(result["providers"][pid]["available"] for pid in ("claude", "codex", "agy", "opencode", "openrouter"))


def test_discovery_follows_environment_and_profile_homes(tmp_path: Path) -> None:
    base = tmp_path / "home"
    profile_home = base / ".env-carvalima"
    (profile_home / ".claude" / "projects").mkdir(parents=True)
    (base / ".env-max" / ".claude" / "projects").mkdir(parents=True)
    (base / ".claude" / "projects").mkdir(parents=True)
    (base / ".codex" / "sessions").mkdir(parents=True)
    (base / ".env-max" / ".local" / "share" / "opencode").mkdir(parents=True)
    (base / ".env-max" / ".local" / "share" / "opencode" / "opencode.db").write_bytes(b"")
    quota = base / ".local" / "share" / "ai-coordinator-runtime" / "state" / "cota"
    other = tmp_path / "outro-perfil"
    (other / ".claude" / "projects").mkdir(parents=True)
    _write_json(quota / "leitura-claude-extra.json", {"perfil": "extra", "userprofile": str(other)})

    found = discover_local_source_paths(env={}, home=profile_home)
    assert found.quota_dir == quota
    assert [p for p, _ in found.claude_dirs] == ["extra", "carvalima", "padrao", "max"]
    assert found.codex_homes == (("padrao", base / ".codex"),)
    assert found.opencode_dbs == (("max", base / ".env-max" / ".local" / "share" / "opencode" / "opencode.db"),)

    custom_quota = tmp_path / "cota-config"
    custom_quota.mkdir()
    custom_codex = tmp_path / "codex-config"
    (custom_codex / "sessions").mkdir(parents=True)
    xdg = tmp_path / "xdg"
    (xdg / "opencode").mkdir(parents=True)
    (xdg / "opencode" / "opencode.db").write_bytes(b"")
    found = discover_local_source_paths(
        env={"AVISO_COTA_DIR": str(custom_quota), "CODEX_HOME": str(custom_codex), "XDG_DATA_HOME": str(xdg)},
        home=profile_home,
    )
    assert found.quota_dir == custom_quota
    assert found.codex_homes[0][1] == custom_codex
    assert found.opencode_dbs[0][1] == xdg / "opencode" / "opencode.db"

    source = Path(provider_adapters.__file__).read_text(encoding="utf-8")
    assert "10864" not in source and "C:\\\\Users" not in source and "C:/Users" not in source


def test_snapshot_merges_local_readings_without_breaking_the_contract(tmp_path: Path) -> None:
    local = LocalProviderReader(_paths(tmp_path)).read(now_ms=NOW_MS)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snap = collect_providers_telemetry(
        profile_name="operador",
        vault_keys=["OPENROUTER_API_KEY"],
        data_dir=data_dir,
        agents_data={"agents": [{"client": "claude", "available": True}]},
        routes_data={"routes": []},
        now_ms=NOW_MS,
        local_readings=local,
    )
    assert snap["sources"]["local_sources_checked"] is True
    assert snap["sources"]["local_providers"] == {pid: True for pid in ("claude", "codex", "agy", "opencode", "openrouter")}
    assert snap["sources"]["local_read_at_unix_ms"] == NOW_MS
    assert snap["data_origin"] == "offline_cache"
    by_id = {p["provider"]: p for p in snap["providers"]}

    claude = by_id["claude"]
    assert claude["windows"]["five_hour"]["used_percent"] == 24.0
    assert claude["tokens"]["total_tokens"] == 3367
    assert claude["coordinator_tokens"]["provenance"] == "unknown"
    assert claude["usage_scope"]["sessions"] == 2
    assert claude["context"]["used_tokens"] == 2005
    assert len(claude["accounts"]) == 2
    assert claude["local_available"] is True
    assert claude["observed_at_unix_ms"] == NOW_MS - 60_000 and claude["stale"] is False
    assert claude["models"][0]["model"] == "claude-test-opus"

    codex = by_id["codex"]
    assert codex["context"]["limit_tokens"] == 1000 and codex["plan"]["plan_type"] == "team"

    agy = by_id["agy"]
    assert agy["availability_status"] == "unknown"
    assert agy["exhausted_model"] == "gemini-test-high" and agy["exhausted_until_iso"]
    assert len(agy["extra_windows"]) == 2

    opencode = by_id["opencode"]
    assert opencode["windows_supported"] is False
    assert opencode["windows"]["weekly"]["supported"] is False
    assert opencode["cost_credits"]["spent_usd"] == 0.5
    assert opencode["cost_credits"]["provenance"] == "measured"

    openrouter = by_id["openrouter"]
    assert openrouter["has_vault_key"] is True
    assert openrouter["cost_credits"]["spent_usd"] == 0.0
    assert openrouter["extra_windows"][0]["used_percent"] == 2.0
    assert openrouter["remote_lookup"]["enabled"] is False

    for item in snap["providers"]:
        for key in ("five_hour", "weekly"):
            assert item["windows"][key]["provenance"] in ("measured", "estimated", "unknown")
            assert isinstance(item["windows"][key]["stale"], bool)
    _assert_no_leak(snap, tmp_path)


def test_exhausted_mark_without_model_blocks_the_provider_until_it_expires(tmp_path: Path) -> None:
    quota = tmp_path / "cota"
    reading = {
        "perfil": "padrao",
        "observado_em": NOW_S - 30,
        "janelas": [{"id": "five_hour", "usado_pct": 100.0, "reset_epoch": NOW_S + 1800}],
        "esgotada": {"em": NOW_S - 60, "ate": NOW_S + 1800, "texto": "texto-da-esgotada"},
    }
    _write_json(quota / "leitura-claude-padrao.json", reading)
    local = LocalProviderReader(LocalSourcePaths(quota_dir=quota)).read(now_ms=NOW_MS)
    assert local["providers"]["claude"]["exhausted"] is True
    snap = collect_providers_telemetry(
        data_dir=tmp_path, agents_data={"agents": []}, routes_data={"routes": []}, now_ms=NOW_MS, local_readings=local
    )
    claude = next(p for p in snap["providers"] if p["provider"] == "claude")
    assert claude["availability_status"] == "quota_exhausted"
    assert claude["availability_retry_at_iso"] == claude["exhausted_until_iso"]
    assert claude["exhausted_model"] is None

    reading["esgotada"]["ate"] = NOW_S - 1
    _write_json(quota / "leitura-claude-padrao.json", reading, age_s=1)
    local = LocalProviderReader(LocalSourcePaths(quota_dir=quota)).read(now_ms=NOW_MS)
    assert local["providers"]["claude"]["exhausted"] is False
    _assert_no_leak(local, tmp_path)


def test_snapshot_without_local_readings_keeps_previous_behaviour(tmp_path: Path) -> None:
    snap = collect_providers_telemetry(
        data_dir=tmp_path,
        agents_data={"agents": []},
        routes_data={"routes": []},
        now_ms=NOW_MS,
    )
    assert snap["sources"]["local_sources_checked"] is False
    assert snap["sources"]["local_providers"] == {}
    for item in snap["providers"]:
        assert item["tokens"]["provenance"] == "unknown"
        assert item["accounts"] == [] and item["extra_windows"] == [] and item["context"] is None
        assert item["windows_supported"] is True


class _DummyTransport(BaseTransport):
    def read_message(self):
        return None

    def write_message(self, _message: str) -> bool:
        return True

    def log_diagnostic(self, _text: str) -> None:
        pass


def test_ipc_handler_feeds_local_readings_and_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reader = LocalProviderReader(_paths(tmp_path))
    monkeypatch.setattr(provider_adapters, "read_local_provider_telemetry", lambda now_ms=None: reader.read(now_ms=NOW_MS))
    profiles = DesktopProfileManager(root_dir=tmp_path / "profiles")
    authorizer = DesktopAuthorizer(profile_manager=profiles)
    token = authorizer.issue_session("default")
    server = SecureIPCServer(transport=_DummyTransport(), terminal_manager=TerminalManager(authorizer=authorizer), session_token=token)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "session_token": token, "method": "telemetry.providers_snapshot", "params": {}})

    ok = server.handle_raw_payload(payload)
    assert ok is not None and ok.error is None
    by_id = {p["provider"]: p for p in ok.result["providers"]}
    assert by_id["claude"]["local_available"] is True
    assert by_id["codex"]["context"]["used_tokens"] == 420
    _assert_no_leak(ok.result, tmp_path)

    def broken(now_ms: Any = None) -> Any:
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(provider_adapters, "read_local_provider_telemetry", broken)
    degraded = server.handle_raw_payload(payload)
    assert degraded is not None and degraded.error is None
    assert degraded.result["sources"]["local_sources_checked"] is False
    assert len(degraded.result["providers"]) >= 5
