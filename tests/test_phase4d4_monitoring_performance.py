"""Fase 4D.4 r2, Bloco 5: cheaper monitoring readings without losing what is on screen.

- The process tree collector (Core and host) takes include_system / include_nodes: the status bar asks only for the
  tree CPU and RAM; Monitoramento keeps asking for everything. Both default to true, so older callers are unchanged.
- Rates (network, disk) of a reading that follows skipped ones are measured since the last reading that sampled the
  system, never divided by the shorter interval since the last reading of any kind.
- The interface polls through usePolling (one reading at a time, pause while hidden, backoff) and the provider
  telemetry at the pace of its source, which the snapshot now states (sources.local_cache_ttl_ms).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.ipc.collector import ProcessTreeCollector, RawProcessSample, SystemResourcesMetrics  # noqa: E402
from desktop.ipc.protocol import MetricProvenance, RPCErrorCode  # noqa: E402
from desktop.ipc.providers_telemetry import collect_providers_telemetry  # noqa: E402
from desktop.ipc.server import BaseTransport, SecureIPCServer  # noqa: E402

MAIN_RS = ROOT / "desktop" / "src-tauri" / "src" / "main.rs"
APP_TSX = ROOT / "desktop" / "src" / "App.tsx"
SECOND_NS = 1_000_000_000
TOKEN = "token-bloco5"


class _Transport(BaseTransport):
    def read_message(self) -> Optional[str]:
        return None

    def write_message(self, message: str) -> bool:
        return True

    def log_diagnostic(self, text: str) -> None:
        pass


def _fake_collector(clock: List[int], sampler_calls: List[Optional[float]], root_pid: int = 100) -> ProcessTreeCollector:
    samples = [
        RawProcessSample(root_pid, 1, "host.exe", 11, 50_000_000, 1.0, private_ram_bytes=30_000_000),
        RawProcessSample(root_pid + 1, root_pid, "msedgewebview2.exe", 22, 80_000_000, 2.0, private_ram_bytes=40_000_000),
    ]

    def sampler(elapsed: Optional[float], cpus: int) -> SystemResourcesMetrics:
        sampler_calls.append(elapsed)
        return SystemResourcesMetrics(logical_cpu_count=cpus)

    return ProcessTreeCollector(
        core_pid=999,
        process_lister=lambda _roots: samples,
        clock_ns=lambda: clock[0],
        cpu_count_fn=lambda: 2,
        system_sampler=sampler,
    )


def test_collector_skips_the_system_and_measures_rates_since_the_last_system_reading() -> None:
    clock = [10 * SECOND_NS]
    calls: List[Optional[float]] = []
    col = _fake_collector(clock, calls)

    full = col.collect(host_pid=100)
    assert full.system is not None and calls == [None]

    for _ in range(2):
        clock[0] += 2 * SECOND_NS
        summary = col.collect(host_pid=100, include_system=False)
        assert summary.system is None
        # The tree itself is still measured: RAM and CPU of the status bar.
        assert summary.total_private_ram_bytes == 70_000_000
        assert summary.cpu_provenance == MetricProvenance.MEASURED
    assert calls == [None]

    clock[0] += 2 * SECOND_NS
    after_gap = col.collect(host_pid=100)
    assert after_gap.system is not None
    assert calls[-1] == pytest.approx(6.0)  # since the last system reading, not the 2 s since the last reading

    clock[0] += 2 * SECOND_NS
    col.collect(host_pid=100)
    assert calls[-1] == pytest.approx(2.0)


def test_ipc_handler_takes_include_system_and_include_nodes() -> None:
    clock = [10 * SECOND_NS]
    calls: List[Optional[float]] = []
    server = SecureIPCServer(
        transport=_Transport(),
        session_token=TOKEN,
        terminal_manager=SimpleNamespace(sessions={}),
        process_collector=_fake_collector(clock, calls, root_pid=os.getpid()),
    )

    def call(request_id: int, params: str):
        return server.handle_raw_payload(
            f'{{"jsonrpc":"2.0","id":{request_id},"session_token":"{TOKEN}",'
            f'"method":"telemetry.process_tree_metrics","params":{params}}}'
        )

    bad = call(1, '{"include_system":"sim"}')
    assert bad is not None and bad.error is not None
    assert bad.error["code"] == RPCErrorCode.INVALID_PARAMS.value
    assert bad.error["data"]["field"] == "include_system"

    summary = call(2, '{"include_system":false,"include_nodes":false}')
    assert summary is not None and summary.error is None
    assert summary.result["system"] is None
    assert "nodes" not in summary.result
    assert summary.result["total_tree_ram_bytes"] > 0
    assert calls == []

    clock[0] += 2 * SECOND_NS
    full = call(3, "{}")
    assert full is not None and full.error is None
    assert full.result["system"] is not None and isinstance(full.result["nodes"], list)
    assert len(calls) == 1

    # Same instant: the reading is reused (no CPU window yet), but what was not asked for is not sent.
    reused = call(4, '{"include_system":false}')
    assert reused is not None and reused.error is None
    assert reused.result["system"] is None
    assert reused.result["sampled_at_unix_ms"] == full.result["sampled_at_unix_ms"]
    assert len(calls) == 1


def test_provider_snapshot_states_the_pace_of_its_local_source(tmp_path: Path) -> None:
    now = 1_700_000_000_000
    common = dict(profile_name="operador", vault_keys=[], data_dir=tmp_path, agents_data={"agents": []},
                  routes_data={"routes": []}, now_ms=now)
    with_local = collect_providers_telemetry(
        **common, local_readings={"read_at_unix_ms": now - 5_000, "cache_ttl_ms": 30_000, "providers": {}}
    )
    assert with_local["sources"]["local_read_at_unix_ms"] == now - 5_000
    assert with_local["sources"]["local_cache_ttl_ms"] == 30_000
    without_local = collect_providers_telemetry(**common)
    assert without_local["sources"]["local_sources_checked"] is False
    assert without_local["sources"]["local_cache_ttl_ms"] is None


def _rust_fn(main_rs: str, header: str) -> str:
    start = main_rs.index(header)
    end = main_rs.find("\n}\n", start)
    return main_rs[start:end]


def test_rust_host_reads_the_system_only_when_asked() -> None:
    main_rs = MAIN_RS.read_text(encoding="utf-8")
    command = _rust_fn(main_rs, "async fn get_process_tree_metrics(")
    assert re.search(r"include_system: Option<bool>,\s*include_nodes: Option<bool>,", command)
    assert "include_system.unwrap_or(true)" in command and "include_nodes.unwrap_or(true)" in command
    assert "spawn_blocking(move || measure_process_tree_scoped(include_system, include_nodes))" in command

    assert "pub system: Option<SystemResourcesMetrics>," in main_rs
    assert '#[serde(skip_serializing_if = "Option::is_none")]\n    pub nodes: Option<Vec<ProcessNodeMetrics>>,' in main_rs
    scoped = _rust_fn(main_rs, "fn measure_process_tree_scoped(include_system: bool, include_nodes: bool)")
    assert "guard.collect_scoped(include_system)" in scoped
    assert "snapshot.system = None;" in scoped and "snapshot.nodes = None;" in scoped
    # The start-up priming keeps the full reading (CPU baseline and the PDH system query).
    assert "    fn collect(&mut self) -> ProcessTreeMetrics {\n        self.collect_scoped(true)\n    }" in main_rs

    collect = main_rs[main_rs.index("    fn collect_scoped(&mut self, include_system: bool) -> ProcessTreeMetrics {"):]
    collect = collect[: collect.index("\nfn collector_instance()")]
    assert "if include_system {\n            self.networks.refresh();\n        }" in collect
    assert "self.pdh.sample(include_system)" in collect
    assert "if include_system { self.refresh_storage_if_needed(now) } else { Vec::new() }" in collect
    assert "if include_system { read_linux_disk_counters() } else { self.prev_disk_counters }" in collect
    assert "system: include_system.then_some(system)," in collect
    assert ".last_system_sample_at\n" in collect and "self.last_system_sample_at = Some(now);" in collect
    # The CPU refresh stays on every reading: the status bar's tree CPU depends on it.
    assert "self.sys.refresh_cpu();" in collect

    # Disk and GPU counters live in their own PDH query, collected only when the system is asked for.
    for counter in (r"\PhysicalDisk(_Total)\% Disk Time", r"\GPU Engine(*)\Utilization Percentage"):
        assert f'Self::add_counter(system_query, r"{counter}")' in main_rs, counter
    for counter in (r"\Process(*)\ID Process", r"\Process(*)\Working Set - Private"):
        assert f'Self::add_counter(query, r"{counter}")' in main_rs, counter
    sample = _rust_fn(main_rs, "    fn sample(&mut self, include_system: bool) -> WindowsPdhSample {")
    assert "if !include_system || self.system_query == 0 || unsafe { PdhCollectQueryData(self.system_query) } != 0 {" in sample
    assert "let _ = PdhCloseQuery(self.system_query);" in main_rs


def test_interface_polls_through_the_adaptive_poller() -> None:
    app = APP_TSX.read_text(encoding="utf-8")
    assert "setInterval" not in app
    assert app.count("usePolling({") == 2
    assert 'callTauri("get_process_tree_metrics", { includeSystem: detail, includeNodes: detail })' in app
    assert "return providersRefreshDelayMs(snap, Date.now());" in app
    assert 'enabled: isTauri && connectionStatus === "connected" && mainView === "monitoramento",' in app
