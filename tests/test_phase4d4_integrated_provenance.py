"""Phase 4D.4.4 integrated provenance, fixture segregation, fail-closed, and endurance tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.auth.authorizer import DesktopAuthorizer  # noqa: E402
from desktop.auth.profile import DesktopProfileManager  # noqa: E402
from desktop.ipc.collector import (  # noqa: E402
    DEFAULT_TELEMETRY_TTL_MS,
    ProcessTreeCollector,
    RawProcessSample,
    measure_collector_overhead,
)
from desktop.ipc.protocol import (  # noqa: E402
    DataOrigin,
    MetricProvenance,
    RPCErrorCode,
    TELEMETRY_SCHEMA_VERSION,
)
from desktop.ipc.providers_telemetry import (  # noqa: E402
    DEFAULT_PROVIDER_TTL_MS,
    collect_providers_telemetry,
)
from desktop.ipc.server import BaseTransport, SecureIPCServer  # noqa: E402
from desktop.terminal.manager import TerminalManager  # noqa: E402


class _DummyTransport(BaseTransport):
    def read_message(self):
        return None

    def write_message(self, _message: str) -> bool:
        return True

    def log_diagnostic(self, _text: str) -> None:
        pass


def _make_ipc_server(tmp_dir: str, collector: ProcessTreeCollector) -> tuple[SecureIPCServer, str]:
    prof_mgr = DesktopProfileManager(root_dir=Path(tmp_dir))
    auth = DesktopAuthorizer(profile_manager=prof_mgr)
    token = auth.issue_session("default")
    mgr = TerminalManager(authorizer=auth)
    return (
        SecureIPCServer(_DummyTransport(), session_token=token, terminal_manager=mgr, process_collector=collector),
        token,
    )


def test_end_to_end_provenance_origin_ttl_contracts() -> None:
    """Verify schema, provenance, origin, TTL, stale flag, and session auth over SecureIPCServer."""
    clock = [1_000_000_000]
    root_pid = os.getpid()
    samples = [
        RawProcessSample(
            pid=root_pid,
            ppid=1,
            name="python.exe",
            create_time_ns=10,
            ram_bytes=40_000_000,
            cpu_time_seconds=2.0,
            private_ram_bytes=25_000_000,
            shared_ram_bytes=15_000_000,
        ),
        RawProcessSample(
            pid=root_pid + 100000,
            ppid=root_pid,
            name="claude.exe",
            create_time_ns=11,
            ram_bytes=60_000_000,
            cpu_time_seconds=4.0,
            private_ram_bytes=45_000_000,
            shared_ram_bytes=15_000_000,
        ),
    ]
    col = ProcessTreeCollector(
        core_pid=root_pid,
        process_lister=lambda _r: samples,
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 4,
        system_sampler=lambda _e, _c: None,
        data_origin=DataOrigin.LIVE_OS,
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        srv, token = _make_ipc_server(tmp, col)
        unauth = srv.handle_raw_payload(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "session_token": "wrong",
                    "method": "telemetry.process_tree_metrics",
                    "params": {},
                }
            )
        )
        assert unauth is not None and unauth.error is not None
        assert unauth.error["code"] == RPCErrorCode.AUTH_ERROR.value

        clock[0] = 2_000_000_000
        samples[0] = RawProcessSample(root_pid, 1, "python.exe", 10, 40_000_000, 2.2, 25_000_000, 15_000_000)
        ok_tree = srv.handle_raw_payload(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "session_token": token,
                    "method": "telemetry.process_tree_metrics",
                    "params": {},
                }
            )
        )
        assert ok_tree is not None and ok_tree.error is None and ok_tree.result is not None
        res = ok_tree.result
        assert res["schema_version"] == TELEMETRY_SCHEMA_VERSION
        assert res["data_origin"] == DataOrigin.LIVE_OS.value
        assert res["ttl_ms"] == DEFAULT_TELEMETRY_TTL_MS
        assert res["ram_provenance"] == MetricProvenance.MEASURED.value
        assert res["private_ram_provenance"] == MetricProvenance.MEASURED.value
        assert res["cpu_provenance"] == MetricProvenance.UNKNOWN.value  # baseline just set on 1st call
        assert "shared_ram_note" in res and res["shared_ram_note"]

        ok_prov = srv.handle_raw_payload(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "session_token": token,
                    "method": "telemetry.providers_snapshot",
                    "params": {},
                }
            )
        )
        assert ok_prov is not None and ok_prov.error is None and ok_prov.result is not None
        pres = ok_prov.result
        assert pres["schema_version"] == TELEMETRY_SCHEMA_VERSION
        assert pres["ttl_ms"] == DEFAULT_PROVIDER_TTL_MS
        assert isinstance(pres["stale"], bool)
        assert len(pres["providers"]) >= 5
        for item in pres["providers"]:
            assert item["windows"]["five_hour"]["provenance"] in ("measured", "estimated", "unknown")
            assert isinstance(item["windows"]["five_hour"]["stale"], bool)


def test_fixture_segregation_never_live() -> None:
    """Fixture collectors and provider snapshots must declare test_fixture and never live_os/live_network."""
    col = ProcessTreeCollector(
        core_pid=900,
        process_lister=lambda _r: [RawProcessSample(900, 1, "python.exe", 10, 20_000_000, 1.0, 15_000_000)],
        clock_ns=lambda: 1_000_000_000,
        cpu_count_fn=lambda: 2,
        system_sampler=lambda _e, _c: None,
        data_origin=DataOrigin.TEST_FIXTURE,
    )
    snap = col.collect(core_pid=900).to_dict()
    assert snap["data_origin"] == DataOrigin.TEST_FIXTURE.value
    assert snap["data_origin"] != DataOrigin.LIVE_OS.value

    now_ms = 1_760_000_000_000
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "provider_quotas.json").write_text(
            json.dumps(
                {
                    "data_origin": "test_fixture",
                    "observed_at_unix_ms": now_ms - 1_000,
                    "ttl_ms": 60_000,
                    "providers": {
                        "claude": {
                            "quota_kind": "subscription_quota",
                            "windows": {
                                "five_hour": {"used_percent": 42.0, "provenance": "measured"},
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        pt = collect_providers_telemetry(data_dir=data_dir, now_ms=now_ms)
        assert pt["data_origin"] == DataOrigin.TEST_FIXTURE.value
        assert pt["data_origin"] not in (DataOrigin.LIVE_OS.value, DataOrigin.LIVE_NETWORK.value)
        claude = next(p for p in pt["providers"] if p["provider"] == "claude")
        assert claude["data_origin"] == DataOrigin.TEST_FIXTURE.value
        assert claude["windows"]["five_hour"]["used_percent"] == 42.0


def test_corrupted_sqlite_and_quota_json_fail_closed_and_scrub_secrets() -> None:
    """Corrupt SQLite/JSON inputs never crash, mark sources unavailable with notice, and scrub secrets."""
    secret_tok = "sk-ant-api03-VERYSECRETVALUE9876543210"
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "coordination.sqlite").write_bytes(b"NOT A SQLITE FILE \x00\xff\xfe")
        (data_dir / "provider_quotas.json").write_text("{corrupt json", encoding="utf-8")

        pt = collect_providers_telemetry(
            profile_name="isolated-check",
            vault_keys=[f"ANTHROPIC_API_KEY={secret_tok}", "OPENROUTER_API_KEY"],
            data_dir=data_dir,
            now_ms=1_760_000_000_000,
        )
        assert pt["sources"]["sqlite_available"] is False
        assert pt["sources"]["quota_snapshot_available"] is False
        assert pt.get("notice")
        serialized = json.dumps(pt)
        assert secret_tok not in serialized
        for p in pt["providers"]:
            assert p["tokens"]["total_tokens"] is None
            assert p["tokens"]["provenance"] == MetricProvenance.UNKNOWN.value


def test_pid_exit_and_reuse_invalidates_cpu_baseline() -> None:
    """Disappearing PID or changed create_time_ns on reused PID resets CPU baseline to unknown."""
    clock = [1_000_000_000]
    current_samples = [
        RawProcessSample(600, 1, "python.exe", 100, 20_000_000, 5.0, 15_000_000),
        RawProcessSample(601, 600, "pwsh.exe", 200, 30_000_000, 10.0, 20_000_000),
    ]
    col = ProcessTreeCollector(
        core_pid=600,
        process_lister=lambda _r: list(current_samples),
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 4,
        system_sampler=lambda _e, _c: None,
    )
    col.prime(core_pid=600)

    # Normal interval: both measured
    clock[0] = 2_000_000_000
    current_samples[0] = RawProcessSample(600, 1, "python.exe", 100, 20_000_000, 5.4, 15_000_000)
    current_samples[1] = RawProcessSample(601, 600, "pwsh.exe", 200, 30_000_000, 10.8, 20_000_000)
    s1 = col.collect(core_pid=600)
    assert next(n for n in s1.nodes if n.pid == 601).cpu_provenance == MetricProvenance.MEASURED

    # PID 601 reused with brand-new create_time_ns=999 and lower CPU time
    clock[0] = 3_000_000_000
    current_samples[0] = RawProcessSample(600, 1, "python.exe", 100, 20_000_000, 5.8, 15_000_000)
    current_samples[1] = RawProcessSample(601, 600, "pwsh.exe", 999, 25_000_000, 0.2, 18_000_000)
    s2 = col.collect(core_pid=600)
    reused = next(n for n in s2.nodes if n.pid == 601)
    assert reused.cpu_usage_percent is None
    assert reused.cpu_provenance == MetricProvenance.UNKNOWN

    # PID 601 exits completely
    clock[0] = 4_000_000_000
    current_samples.pop(1)
    current_samples[0] = RawProcessSample(600, 1, "python.exe", 100, 20_000_000, 6.2, 15_000_000)
    s3 = col.collect(core_pid=600)
    assert [n.pid for n in s3.nodes] == [600]


def test_sustained_rest_overhead_below_1_percent_and_active_child_load() -> None:
    """Measure collector overhead across sustained 1s intervals and verify live child load telemetry."""
    overhead = measure_collector_overhead(iterations=4, interval_ms=1000)
    assert overhead["iterations"] == 4
    assert overhead["wall_elapsed_ms"] >= 3500.0
    assert overhead["overhead_system_percent"] < 1.5
    assert overhead["last_process_count"] >= 1

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; buf = bytearray(12 * 1024 * 1024); x = sum(range(400000)); time.sleep(2.5)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.35)
        col = ProcessTreeCollector(core_pid=os.getpid(), min_sample_interval_ms=200)
        col.prime()
        time.sleep(0.3)
        snap = col.collect()
        by_pid = {n.pid: n for n in snap.nodes}
        assert proc.pid in by_pid
        child = by_pid[proc.pid]
        assert child.working_set_ram_bytes is not None and child.working_set_ram_bytes >= 12 * 1024 * 1024
        if sys.platform == "win32" or sys.platform.startswith("linux"):
            assert child.private_ram_bytes is not None
            assert child.private_ram_bytes >= 10 * 1024 * 1024
            assert child.private_ram_bytes <= child.working_set_ram_bytes
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_windows_pdh_private_ws_matches_collector_on_current_process() -> None:
    """On Windows, cross-check ProcessTreeCollector private_ram_bytes against independent PDH English query."""
    if sys.platform != "win32":
        return

    from desktop.ipc.collector import _WindowsPdhHelper

    helper = _WindowsPdhHelper()
    try:
        independent_map = helper.sample()["private_by_pid"]
    finally:
        helper.close()
    pid = os.getpid()
    assert pid in independent_map
    assert independent_map[pid] > 0

    col = ProcessTreeCollector(core_pid=pid, min_sample_interval_ms=200)
    snap = col.collect()
    me = next((n for n in snap.nodes if n.pid == pid), None)
    assert me is not None
    assert me.private_ram_bytes is not None and me.private_ram_bytes > 0
    assert me.working_set_ram_bytes is not None and me.private_ram_bytes <= me.working_set_ram_bytes
    ratio = me.private_ram_bytes / independent_map[pid]
    assert 0.5 <= ratio <= 1.5
