"""Phase 4D.4.1 tests: Real Resource Collector (CPU, RAM, process tree, provenance, overhead)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.ipc.collector import (  # noqa: E402
    DEFAULT_MIN_SAMPLE_INTERVAL_MS,
    ProcessTreeCollector,
    RawProcessSample,
    measure_collector_overhead,
    validate_sample_interval_ms,
)
from desktop.ipc.protocol import MetricProvenance, RPCErrorCode  # noqa: E402
from desktop.ipc.server import BaseTransport, SecureIPCServer  # noqa: E402


class _DummyTransport(BaseTransport):
    def __init__(self) -> None:
        self.diagnostics: List[str] = []

    def read_message(self) -> Optional[str]:
        return None

    def write_message(self, message: str) -> bool:
        return True

    def log_diagnostic(self, text: str) -> None:
        self.diagnostics.append(text)


def test_01_baseline_sample_is_unknown_and_never_fakes_zero_percent() -> None:
    now_ns = 1_000_000_000

    def fake_lister(_roots: Optional[Iterable[int]]) -> List[RawProcessSample]:
        return [
            RawProcessSample(
                pid=100,
                ppid=1,
                name="ai-coordinator-desktop.exe",
                create_time_ns=10_000,
                ram_bytes=42_000_000,
                cpu_time_seconds=0.0,
            ),
            RawProcessSample(
                pid=200,
                ppid=100,
                name="python.exe",
                create_time_ns=10_500,
                ram_bytes=18_000_000,
                cpu_time_seconds=0.0,
            ),
        ]

    collector = ProcessTreeCollector(
        core_pid=200,
        process_lister=fake_lister,
        clock_ns=lambda: now_ns,
        cpu_count_fn=lambda: 4,
    )

    snap = collector.collect(host_pid=100, core_pid=200)
    assert snap.ram_provenance == MetricProvenance.MEASURED
    assert snap.total_tree_ram_bytes == 60_000_000
    assert snap.host_ram_bytes == 42_000_000
    assert snap.cpu_provenance == MetricProvenance.UNKNOWN
    assert snap.cpu_usage_percent is None
    assert snap.sample_interval_ms is None
    assert all(node.cpu_provenance == MetricProvenance.UNKNOWN and node.cpu_usage_percent is None for node in snap.nodes)


def test_02_temporal_sampling_respects_min_interval_and_measures_cpu_delta() -> None:
    clock = [1_000_000_000]
    samples_by_tick: List[List[RawProcessSample]] = [
        [
            RawProcessSample(100, 1, "host.exe", 5000, 30_000_000, 1.00),
            RawProcessSample(200, 100, "python.exe", 5100, 20_000_000, 0.50),
        ],
        # At t + 500ms on a 2-core host: host +0.20s CPU (=20% of 2-core), core +0.10s CPU (=10% of 2-core)
        [
            RawProcessSample(100, 1, "host.exe", 5000, 31_000_000, 1.20),
            RawProcessSample(200, 100, "python.exe", 5100, 21_000_000, 0.60),
        ],
    ]
    tick = [0]

    def fake_lister(_roots: Optional[Iterable[int]]) -> List[RawProcessSample]:
        return samples_by_tick[tick[0]]

    collector = ProcessTreeCollector(
        core_pid=200,
        min_sample_interval_ms=DEFAULT_MIN_SAMPLE_INTERVAL_MS,
        process_lister=fake_lister,
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 2,
    )

    first = collector.collect(host_pid=100, core_pid=200)
    assert first.cpu_provenance == MetricProvenance.UNKNOWN
    assert first.cpu_usage_percent is None

    # Faster than min_sample_interval_ms (100ms < 200ms) returns cached baseline snapshot without dividing tiny interval
    clock[0] += 100_000_000
    tick[0] = 1
    throttled = collector.collect(host_pid=100, core_pid=200)
    assert throttled is first

    # Advance past min_sample_interval_ms (total 500ms from baseline)
    clock[0] += 400_000_000
    measured = collector.collect(host_pid=100, core_pid=200)
    assert measured.sample_interval_ms == 500
    assert measured.cpu_provenance == MetricProvenance.MEASURED
    # 0.30s total CPU / (0.5s * 2 cores) * 100 = 30.0%
    assert measured.cpu_usage_percent == pytest.approx(30.0, abs=0.05)
    assert measured.categories["host"].cpu_usage_percent == pytest.approx(20.0, abs=0.05)
    assert measured.categories["core"].cpu_usage_percent == pytest.approx(10.0, abs=0.05)


def test_03_tree_deduplication_and_non_overlapping_categories() -> None:
    clock = [1_000_000_000]
    samples = [
        RawProcessSample(100, 1, "ai-coordinator-desktop.exe", 11, 50_000_000, 1.0),
        RawProcessSample(100, 1, "ai-coordinator-desktop.exe", 11, 50_000_000, 1.0),  # duplicate row
        RawProcessSample(200, 100, "python.exe", 22, 20_000_000, 0.5),
        RawProcessSample(300, 100, "msedgewebview2.exe", 33, 80_000_000, 2.0),
        RawProcessSample(400, 200, "pwsh.exe", 44, 15_000_000, 0.2),
        RawProcessSample(500, 400, "git.exe", 55, 5_000_000, 0.1),
        RawProcessSample(999, 1, "unrelated-browser.exe", 99, 900_000_000, 50.0),  # outside tree
    ]

    collector = ProcessTreeCollector(
        core_pid=200,
        process_lister=lambda _roots: samples,
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 1,
    )
    collector.prime(host_pid=100, core_pid=200, terminal_pids={400: 44})
    clock[0] += 1_000_000_000
    snap = collector.collect(host_pid=100, core_pid=200, terminal_pids={400: 44})

    assert snap.process_count == 5
    assert snap.total_tree_ram_bytes == 50_000_000 + 20_000_000 + 80_000_000 + 15_000_000 + 5_000_000
    assert sum(cat.ram_bytes for cat in snap.categories.values()) == snap.total_tree_ram_bytes
    assert snap.categories["host"].process_count == 1
    assert snap.categories["core"].process_count == 1
    assert snap.categories["webview"].process_count == 1
    assert snap.categories["terminal"].process_count == 1
    assert snap.categories["child"].process_count == 1
    assert 999 not in {n.pid for n in snap.nodes}


def test_04_pid_reuse_and_exit_protect_against_negative_or_bogus_cpu() -> None:
    clock = [1_000_000_000]
    phase = [0]

    def fake_lister(_roots: Optional[Iterable[int]]) -> List[RawProcessSample]:
        if phase[0] == 0:
            return [
                RawProcessSample(100, 1, "host.exe", 1000, 10_000_000, 1.0),
                RawProcessSample(200, 100, "python.exe", 2000, 10_000_000, 0.5),
                RawProcessSample(300, 200, "old_worker.exe", 3000, 5_000_000, 10.0),
            ]
        if phase[0] == 1:
            # PID 300 exited and PID 300 was immediately reused by a NEW process with create_time_ns=9999!
            return [
                RawProcessSample(100, 1, "host.exe", 1000, 10_000_000, 1.1),
                RawProcessSample(200, 100, "python.exe", 2000, 10_000_000, 0.6),
                RawProcessSample(300, 200, "new_child.exe", 9999, 6_000_000, 0.05),
            ]
        return [
            RawProcessSample(100, 1, "host.exe", 1000, 10_000_000, 1.2),
            RawProcessSample(200, 100, "python.exe", 2000, 10_000_000, 0.7),
            RawProcessSample(300, 200, "new_child.exe", 9999, 6_000_000, 0.25),
        ]

    collector = ProcessTreeCollector(
        core_pid=200,
        process_lister=fake_lister,
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 1,
    )
    collector.prime(host_pid=100, core_pid=200)

    clock[0] += 1_000_000_000
    phase[0] = 1
    reused = collector.collect(host_pid=100, core_pid=200)
    reused_node = {n.pid: n for n in reused.nodes}[300]
    assert reused_node.cpu_provenance == MetricProvenance.UNKNOWN
    assert reused_node.cpu_usage_percent is None
    assert reused.cpu_provenance == MetricProvenance.ESTIMATED
    assert reused.cpu_usage_percent == pytest.approx(20.0, abs=0.05)

    clock[0] += 1_000_000_000
    phase[0] = 2
    settled = collector.collect(host_pid=100, core_pid=200)
    settled_node = {n.pid: n for n in settled.nodes}[300]
    assert settled_node.cpu_provenance == MetricProvenance.MEASURED
    assert settled_node.cpu_usage_percent == pytest.approx(20.0, abs=0.05)
    assert settled.cpu_provenance == MetricProvenance.MEASURED
    assert settled.cpu_usage_percent == pytest.approx(40.0, abs=0.05)


def test_05_real_os_child_tree_and_under_one_percent_overhead() -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "end = time.monotonic() + 2.0\n"
            "x = 0\n"
            "while time.monotonic() < end:\n"
            "    x = (x + 1) % 1000\n"
            "    time.sleep(0.005)\n",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        collector = ProcessTreeCollector(core_pid=os.getpid(), min_sample_interval_ms=200)
        baseline = collector.prime()
        assert baseline.ram_provenance == MetricProvenance.MEASURED
        assert baseline.total_tree_ram_bytes > 0
        assert baseline.cpu_provenance == MetricProvenance.UNKNOWN
        assert baseline.cpu_usage_percent is None

        time.sleep(0.25)
        snap = collector.collect()
        pids = {n.pid for n in snap.nodes}
        assert os.getpid() in pids
        assert proc.pid in pids
        assert snap.total_tree_ram_bytes > 0
        assert snap.cpu_provenance == MetricProvenance.MEASURED
        assert snap.cpu_usage_percent is not None
        assert 0.0 <= snap.cpu_usage_percent <= 100.0
        assert snap.categories["child"].process_count >= 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Overhead check at rest using realistic polling cadence (>= 1s; app polls at 2s)
    report = measure_collector_overhead(iterations=2, interval_ms=1000)
    assert report["overhead_system_percent"] < 1.0, f"Excessive collector overhead: {report}"


def test_06_ipc_telemetry_handler_contract_auth_and_validation() -> None:
    token = "secret-session-token-4d41"
    fake_session = SimpleNamespace(
        pid=os.getpid(),
        identity=SimpleNamespace(create_time_ns=None),
    )
    fake_mgr = SimpleNamespace(sessions={"s1": fake_session})
    transport = _DummyTransport()
    server = SecureIPCServer(transport=transport, session_token=token, terminal_manager=fake_mgr)

    # Rejects missing/invalid session token
    unauth = server.handle_raw_payload(
        '{"jsonrpc":"2.0","id":1,"method":"telemetry.process_tree_metrics","params":{}}'
    )
    assert unauth is not None and unauth.error is not None
    assert unauth.error["code"] == RPCErrorCode.AUTH_ERROR.value

    # Rejects out-of-range interval cleanly as INVALID_PARAMS (-32602)
    bad_interval = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":2,"session_token":"{token}","method":"telemetry.process_tree_metrics","params":{{"min_sample_interval_ms":10}}}}'
    )
    assert bad_interval is not None and bad_interval.error is not None
    assert bad_interval.error["code"] == RPCErrorCode.INVALID_PARAMS.value
    assert bad_interval.error["data"]["field"] == "min_sample_interval_ms"

    # Baseline valid call returns measured RAM, unknown CPU (never 0.0 fake!)
    first = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":3,"session_token":"{token}","method":"telemetry.process_tree_metrics","params":{{"include_nodes":false}}}}'
    )
    assert first is not None and first.error is None and first.result is not None
    res1 = first.result
    assert res1["ram_provenance"] == "measured"
    assert res1["total_tree_ram_bytes"] > 0
    assert res1["cpu_provenance"] == "unknown"
    assert res1["cpu_usage_percent"] is None
    assert "nodes" not in res1

    time.sleep(0.22)
    second = server.handle_raw_payload(
        f'{{"jsonrpc":"2.0","id":4,"session_token":"{token}","method":"telemetry.process_tree_metrics","params":{{"min_sample_interval_ms":200}}}}'
    )
    assert second is not None and second.error is None and second.result is not None
    res2 = second.result
    assert res2["cpu_provenance"] == "measured"
    assert isinstance(res2["cpu_usage_percent"], (int, float))
    assert "nodes" in res2
    assert isinstance(validate_sample_interval_ms(200), int)


def test_07_rust_host_collector_contract_and_safety() -> None:
    main_rs = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")
    required_symbols = [
        "MIN_CPU_SAMPLE_INTERVAL_MS",
        "CORE_PYTHON_PID",
        "PROCESS_COLLECTOR",
        "struct ProcessTreeCollectorState",
        "struct ProcessNodeMetrics",
        "struct CategoryResourceMetrics",
        "struct ProcessTreeCategories",
        "fn prime_process_collector()",
        "fn measure_process_tree() -> ProcessTreeMetrics",
        "proc.start_time()",
        "available_parallelism",
        "\"unknown\"",
        "\"measured\"",
    ]
    for symbol in required_symbols:
        assert symbol in main_rs, f"Missing Rust 4D.4.1 collector symbol: {symbol}"

    # Ensure previous IPC & updater structural ordering guarantees remain intact
    assert main_rs.index("fn get_runtime_status(") < main_rs.index("fn get_process_tree_metrics(")

    bloco1_symbols = [
        "struct SystemResourcesMetrics",
        "struct StorageVolumeMetrics",
        "private_ram_bytes",
        "private_ram_provenance",
        "working_set_ram_bytes",
        "shared_ram_bytes",
        "agent_kind",
        "PdhAddEnglishCounterW",
        "Working Set - Private",
        "Disks::new_with_refreshed_list",
        "Networks::new_with_refreshed_list",
        "global_cpu_info",
    ]
    for symbol in bloco1_symbols:
        assert symbol in main_rs, f"Missing Rust 4D.4.1 refined collector symbol: {symbol}"


def test_08_private_working_set_vs_working_set_and_shared_ram() -> None:
    from desktop.ipc.collector import SystemResourcesMetrics

    clock = [1_000_000_000]
    samples_all_private = [
        RawProcessSample(100, 1, "ai-coordinator-desktop.exe", 11, 50_000_000, 1.0, private_ram_bytes=35_000_000),
        RawProcessSample(200, 100, "msedgewebview2.exe", 22, 180_000_000, 2.0, private_ram_bytes=55_000_000),
        RawProcessSample(300, 100, "msedgewebview2.exe", 33, 180_000_000, 1.5, private_ram_bytes=50_000_000),
    ]
    samples_missing_private = [
        RawProcessSample(100, 1, "host.exe", 11, 50_000_000, 1.0, private_ram_bytes=None),
        RawProcessSample(200, 100, "webview.exe", 22, 180_000_000, 2.0, private_ram_bytes=None),
    ]
    mode = ["all"]

    col = ProcessTreeCollector(
        core_pid=999,
        process_lister=lambda _r: samples_all_private if mode[0] == "all" else samples_missing_private,
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 2,
        system_sampler=lambda _e, cpus: SystemResourcesMetrics(logical_cpu_count=cpus),
    )
    snap = col.collect(host_pid=100, core_pid=999)
    assert snap.total_working_set_ram_bytes == 410_000_000
    assert snap.total_private_ram_bytes == 140_000_000
    assert snap.private_ram_provenance == MetricProvenance.MEASURED
    assert snap.host_private_ram_bytes == 35_000_000
    # Shared RAM per process: 15M + 125M + 130M = 270M (explicitly annotated as non-additive)
    assert snap.total_shared_ram_bytes == 270_000_000
    assert "não é aditiva" in snap.shared_ram_note

    # Fail-closed: if OS cannot report Private Working Set, never pretend Working Set is private RAM
    mode[0] = "none"
    clock[0] += 1_000_000_000
    snap_unknown = col.collect(host_pid=100, core_pid=999)
    assert snap_unknown.total_working_set_ram_bytes == 230_000_000
    assert snap_unknown.total_private_ram_bytes is None
    assert snap_unknown.private_ram_provenance == MetricProvenance.UNKNOWN


def test_09_ai_agent_detection_vs_generic_child_processes() -> None:
    from desktop.ipc.collector import detect_agent_kind

    assert detect_agent_kind("claude.exe") == "claude"
    assert detect_agent_kind("codex") == "codex"
    assert detect_agent_kind("opencode-cli.exe") == "opencode"
    assert detect_agent_kind("antigravity.exe") == "agy"
    assert detect_agent_kind("git.exe") is None
    assert detect_agent_kind("pwsh.exe") is None

    clock = [1_000_000_000]
    samples = [
        RawProcessSample(100, 1, "ai-coordinator-desktop.exe", 10, 40_000_000, 1.0, private_ram_bytes=30_000_000),
        RawProcessSample(200, 100, "python.exe", 20, 30_000_000, 0.5, private_ram_bytes=20_000_000),
        RawProcessSample(300, 200, "pwsh.exe", 30, 25_000_000, 0.3, private_ram_bytes=18_000_000),
        RawProcessSample(400, 300, "claude.exe", 40, 120_000_000, 2.0, private_ram_bytes=95_000_000),
        RawProcessSample(500, 300, "git.exe", 50, 10_000_000, 0.1, private_ram_bytes=7_000_000),
    ]
    col = ProcessTreeCollector(
        core_pid=200,
        process_lister=lambda _r: samples,
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 2,
    )
    snap = col.collect(
        host_pid=100,
        core_pid=200,
        terminal_pids={300: 30},
        agent_pids={400: (40, "claude", "sess-agent-1")},
    )
    by_pid = {n.pid: n for n in snap.nodes}
    assert by_pid[300].role == "terminal"
    assert by_pid[400].role == "agent" and by_pid[400].agent_kind == "claude" and by_pid[400].session_id == "sess-agent-1"
    assert by_pid[500].role == "child" and by_pid[500].agent_kind is None
    assert snap.categories["agent"].process_count == 1
    assert snap.categories["agent"].private_ram_bytes == 95_000_000
    assert snap.categories["child"].process_count == 1
    assert sum(c.ram_bytes for c in snap.categories.values()) == snap.total_tree_ram_bytes
    assert sum(c.private_ram_bytes or 0 for c in snap.categories.values()) == snap.total_private_ram_bytes


def test_10_system_resources_separation_and_gpu_engine_aggregation() -> None:
    from desktop.ipc.collector import StorageVolumeMetrics, SystemResourcesMetrics, aggregate_gpu_engine_values

    gpu = aggregate_gpu_engine_values(
        [
            ("pid_100_luid_0x00000000_0x00010000_phys_0_eng_0_engtype_3D", 14.5),
            ("pid_200_luid_0x00000000_0x00010000_phys_0_eng_0_engtype_3D", 9.0),
            ("pid_100_luid_0x00000000_0x00010000_phys_0_eng_1_engtype_Copy", 4.0),
        ]
    )
    assert gpu == pytest.approx(23.5, abs=0.01)
    assert aggregate_gpu_engine_values([]) is None

    sys_metrics = SystemResourcesMetrics(
        logical_cpu_count=8,
        cpu_usage_percent=42.5,
        cpu_provenance=MetricProvenance.MEASURED,
        ram_total_bytes=32 * 1024**3,
        ram_used_bytes=20 * 1024**3,
        ram_available_bytes=12 * 1024**3,
        ram_usage_percent=62.5,
        ram_provenance=MetricProvenance.MEASURED,
        storage_total_bytes=1000 * 1024**3,
        storage_used_bytes=600 * 1024**3,
        storage_available_bytes=400 * 1024**3,
        storage_usage_percent=60.0,
        storage_provenance=MetricProvenance.MEASURED,
        storage_volumes=[StorageVolumeMetrics("C:", "C:\\", 1000 * 1024**3, 600 * 1024**3, 400 * 1024**3, 60.0)],
        disk_activity_percent=7.2,
        disk_activity_provenance=MetricProvenance.MEASURED,
        disk_read_bytes_per_sec=1_200_000,
        disk_write_bytes_per_sec=400_000,
        disk_io_provenance=MetricProvenance.MEASURED,
        network_rx_bytes_per_sec=500_000,
        network_tx_bytes_per_sec=100_000,
        network_total_bytes_per_sec=600_000,
        network_provenance=MetricProvenance.MEASURED,
        gpu_usage_percent=gpu,
        gpu_provenance=MetricProvenance.MEASURED,
    )
    col = ProcessTreeCollector(
        core_pid=200,
        process_lister=lambda _r: [RawProcessSample(200, 1, "python.exe", 20, 30_000_000, 1.0, 20_000_000)],
        clock_ns=lambda: 2_000_000_000,
        cpu_count_fn=lambda: 8,
        system_sampler=lambda _e, _c: sys_metrics,
    )
    snap_dict = col.collect(core_pid=200).to_dict()
    assert snap_dict["system"]["storage_usage_percent"] == 60.0
    assert snap_dict["system"]["disk_activity_percent"] == 7.2
    assert snap_dict["system"]["network_total_bytes_per_sec"] == 600_000
    assert snap_dict["system"]["gpu_usage_percent"] == 23.5
    assert snap_dict["data_origin"] == "live_os"


def test_11_live_os_private_vs_working_set_consistency() -> None:
    # Real OS test validating private RAM <= Working Set and system telemetry on current host
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; buf = bytearray(12 * 1024 * 1024); time.sleep(2.5)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.35)
        col = ProcessTreeCollector(core_pid=os.getpid(), min_sample_interval_ms=200)
        col.prime()
        time.sleep(0.25)
        snap = col.collect()
        by_pid = {n.pid: n for n in snap.nodes}
        assert proc.pid in by_pid
        child = by_pid[proc.pid]
        assert child.working_set_ram_bytes is not None and child.working_set_ram_bytes >= 12 * 1024 * 1024
        if sys.platform == "win32" or sys.platform.startswith("linux"):
            assert child.private_ram_bytes is not None and child.private_ram_bytes >= 10 * 1024 * 1024
            assert child.private_ram_bytes <= child.working_set_ram_bytes
            assert child.private_ram_provenance == MetricProvenance.MEASURED
        assert snap.system is not None
        assert snap.system.ram_total_bytes > 0
        assert snap.system.storage_total_bytes > 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
