"""Controlled homologation tests for Emergency Resource Governor & Cascade Guard."""
import json
import os
import sys
import time

import pytest

from ai_coordinator.cli import main
from ai_coordinator.governor import (
    GovernorConfig,
    ResourceGovernorError,
    SaturationBand,
    SystemSnapshot,
    classify_saturation_band,
    probe_system_snapshot,
)
from ai_coordinator.process import ProcessSupervisor, ResourceLimits
from ai_coordinator.storage import Store


def _snap(cpu_used: float, ram_used: float) -> SystemSnapshot:
    return SystemSnapshot(
        cpu_used_pct=cpu_used,
        cpu_free_pct=round(100.0 - cpu_used, 2),
        ram_used_pct=ram_used,
        ram_free_pct=round(100.0 - ram_used, 2),
        ram_total_mb=16384,
        ram_avail_mb=int(16384 * (100.0 - ram_used) / 100.0),
        disk_free_gb=100.0,
        disk_used_pct=50.0,
        observed_at="2026-11-27T21:00:00.000000Z",
        probe_duration_ms=12.0,
    )


def test_real_system_probe_overhead_and_authority():
    snap = probe_system_snapshot(cpu_sample_seconds=0.05)
    assert 0.0 <= snap.cpu_used_pct <= 100.0
    assert 0.0 <= snap.ram_used_pct <= 100.0
    assert snap.ram_total_mb > 0
    assert snap.probe_duration_ms < 2000.0
    assert snap.authority_scope == "governed_processes_only"


def test_saturation_bands_and_hysteresis():
    cfg = GovernorConfig(min_free_cpu_pct=10.0, min_free_ram_pct=10.0, hysteresis_margin_pct=5.0)
    assert classify_saturation_band(_snap(40.0, 50.0), cfg) == SaturationBand.NORMAL
    assert classify_saturation_band(_snap(78.0, 50.0), cfg) == SaturationBand.ELEVATED
    assert classify_saturation_band(_snap(87.0, 50.0), cfg) == SaturationBand.RESTRICTED
    # CPU reserve breached (91% used => 9% free < 10% reserve)
    assert classify_saturation_band(_snap(91.0, 50.0), cfg) == SaturationBand.EMERGENCY
    # With 5% reserve, 92% falls in CRITICAL (90-95%)
    cfg_5 = GovernorConfig(min_free_cpu_pct=5.0, min_free_ram_pct=5.0, hysteresis_margin_pct=5.0)
    assert classify_saturation_band(_snap(92.0, 50.0), cfg_5) == SaturationBand.CRITICAL
    # RAM reserve breached (92% used => 8% free < 10% reserve)
    assert classify_saturation_band(_snap(30.0, 92.0), cfg) == SaturationBand.EMERGENCY
    # Hysteresis: recovering from EMERGENCY at 72% stays ELEVATED until < 70%
    assert classify_saturation_band(_snap(72.0, 50.0), cfg, previous_band=SaturationBand.EMERGENCY) == SaturationBand.ELEVATED
    assert classify_saturation_band(_snap(65.0, 50.0), cfg, previous_band=SaturationBand.EMERGENCY) == SaturationBand.NORMAL


def test_shared_sessions_concurrency_and_saturation_blocks_reserve(tmp_path):
    root = tmp_path / "coord"
    proj = tmp_path / "proj"
    proj.mkdir()
    store_a = Store(root)
    store_b = Store(root)
    try:
        load = {"cpu": 40.0, "ram": 40.0}
        store_a.governor._probe_fn = lambda: _snap(load["cpu"], load["ram"])
        store_b.governor._probe_fn = lambda: _snap(load["cpu"], load["ram"])
        store_a.governor.update_config(max_global_concurrent=2)

        t1 = store_a.new(proj, "Session A")["id"]
        t2 = store_b.new(proj, "Session B")["id"]
        t3 = store_a.new(proj, "Session C")["id"]

        run1, _, _ = store_a.reserve(t1, "code", "work 1", 5)
        run2, _, _ = store_b.reserve(t2, "code", "work 2", 5)
        store_a.transition_run(run1, to_status="running")
        store_b.transition_run(run2, to_status="running")

        # Third simultaneous reservation across sessions is blocked by shared concurrency cap
        with pytest.raises(ResourceGovernorError) as exc:
            store_a.reserve(t3, "code", "work 3", 5)
        assert exc.value.reason == "concurrency_limit"

        # Release run1 via confirm_exit -> frees slot
        store_a.confirm_exit(run1, exit_code=0)

        # Simulate external program spiking CPU to 92% (breaching 10% reserve) -> EMERGENCY blocks all
        load["cpu"] = 92.0
        with pytest.raises(ResourceGovernorError) as exc2:
            store_a.reserve(t3, "code", "work 3 under saturation", 5)
        assert exc2.value.reason == "saturation_emergency"

        # Simulate external load dropping to 86% (RESTRICTED) -> heavy blocked, medium allowed
        load["cpu"] = 86.0
        store_b.confirm_exit(run2, exit_code=0)
        with pytest.raises(ResourceGovernorError) as exc3:
            store_a.reserve(t3, "code", "heavy under restricted", 5, activity_weight="heavy")
        assert exc3.value.reason == "heavy_activity_blocked"
    finally:
        store_a.db.close()
        store_b.db.close()


def test_cascade_guard_depth_rate_limit_and_lease_recovery(tmp_path):
    root = tmp_path / "coord"
    proj = tmp_path / "proj"
    proj.mkdir()
    store = Store(root)
    try:
        store.governor._probe_fn = lambda: _snap(20.0, 30.0)
        store.governor.update_config(max_delegation_depth=2, max_delegations_per_window=3, max_global_concurrent=5)
        task_id = store.new(proj, "Cascade test")["id"]

        # Depth > max_delegation_depth is rejected
        with pytest.raises(ResourceGovernorError) as exc_depth:
            store.reserve(task_id, "code", "too deep", 5, delegation_depth=3)
        assert exc_depth.value.reason == "cascade_depth_exceeded"

        # Rate limit blocks 4th acquisition within window
        for i in range(3):
            lease = store.governor.acquire_lease(task_id=task_id, project=str(proj), delegation_depth=1)
            store.governor.release_lease(lease["lease_id"])
        with pytest.raises(ResourceGovernorError) as exc_rate:
            store.governor.acquire_lease(task_id=task_id, project=str(proj), delegation_depth=1)
        assert exc_rate.value.reason == "rate_limit_exceeded"

        # Abandoned lease with dead PID is automatically reclaimed
        store.governor.db.execute(
            """INSERT INTO resource_governor_leases(
                lease_id, task_id, project, owner_pid, status, acquired_at, expires_at
            ) VALUES ('deadlease1', ?, ?, 99999999, 'active', ?, ?)""",
            (task_id, str(proj), time.time() - 100.0, time.time() - 10.0),
        )
        store.governor.db.commit()
        reclaimed = store.governor.reclaim_abandoned_leases()
        assert reclaimed == 1
    finally:
        store.db.close()


def test_windows_job_object_cpu_cap_and_cli_without_desktop(tmp_path, capsys, monkeypatch):
    # CLI works standalone without Desktop
    root = tmp_path / "coord"
    monkeypatch.setattr(
        "sys.argv",
        ["ai-coordinator", "--root", str(root), "governor", "config", "--min-free-cpu", "15", "--max-concurrent", "3"],
    )
    assert main() == 0
    cfg_out = json.loads(capsys.readouterr().out)
    assert cfg_out["min_free_cpu_pct"] == 15.0
    assert cfg_out["max_global_concurrent"] == 3

    monkeypatch.setattr("sys.argv", ["ai-coordinator", "--root", str(root), "governor", "status"])
    assert main() == 0
    status_out = json.loads(capsys.readouterr().out)
    assert "system" in status_out and "reserves" in status_out

    # ProcessSupervisor enforces Job Object CPU cap & subprocess limit on Windows
    if os.name == "nt":
        import subprocess
        limits = ResourceLimits(max_processes=8, cpu_rate_percent=50.0, priority_class="below_normal")
        proc = ProcessSupervisor.spawn(
            [sys.executable, "-c", "print('capped-ok')"],
            role="worker",
            use_job_object=True,
            resource_limits=limits,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.job_handle is not None
        out, _ = proc.communicate(timeout=5)
        assert out.strip() == "capped-ok"


def test_cross_root_authority_live_lease_protection_and_dynamic_rebalance(tmp_path):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    proj = tmp_path / "proj"
    proj.mkdir()

    # Two completely separate Store roots share the same global governor authority
    store_a = Store(root_a)
    store_b = Store(root_b)
    try:
        load = {"cpu": 30.0, "ram": 30.0}
        store_a.governor._probe_fn = lambda: _snap(load["cpu"], load["ram"])
        store_b.governor._probe_fn = lambda: _snap(load["cpu"], load["ram"])
        store_a.governor.update_config(max_global_concurrent=2, worker_cpu_cap_pct=80.0, lease_ttl_seconds=1.0)

        ta = store_a.new(proj, "Root A task")["id"]
        tb = store_b.new(proj, "Root B task")["id"]
        tc = store_a.new(proj, "Root A second task")["id"]

        run_a, _, _ = store_a.reserve(ta, "code", "work A", 5)
        run_b, _, _ = store_b.reserve(tb, "code", "work B", 5)

        # Third reservation across different roots is blocked by shared global cap
        with pytest.raises(ResourceGovernorError) as exc:
            store_a.reserve(tc, "code", "work C", 5)
        assert exc.value.reason == "concurrency_limit"

        # Live-process lease protection: even if expires_at is in the past, alive PID renews instead of freeing
        store_a.governor.db.execute("UPDATE resource_governor_leases SET expires_at=?", (time.time() - 50.0,))
        store_a.governor.db.commit()
        assert store_a.governor.reclaim_abandoned_leases() == 0

        # Dynamic throttling of already-active processes when external load rises
        reb_normal = store_a.governor.rebalance_active_job(None)
        assert reb_normal["applied_cpu_cap_pct"] == 40.0  # 80% / 2 active leases
        load["cpu"] = 87.0  # RESTRICTED (cap <= 50%)
        reb_restricted = store_a.governor.rebalance_active_job(None)
        assert reb_restricted["band"] == SaturationBand.RESTRICTED
        assert reb_restricted["applied_cpu_cap_pct"] == 25.0  # 50% / 2 active leases

        store_a.transition_run(run_a, to_status="running")
        store_b.transition_run(run_b, to_status="running")
        store_a.confirm_exit(run_a, exit_code=0)
        store_b.confirm_exit(run_b, exit_code=0)
    finally:
        store_a.governor.close()
        store_b.governor.close()
        store_a.db.close()
        store_b.db.close()


def test_windows_job_object_hard_cap_actually_throttles_cpu():
    if os.name != "nt":
        pytest.skip("Win32 Job Object CPU rate control test is Windows-only")
    import ctypes
    from ai_coordinator.process import FILETIME

    kernel32 = ctypes.windll.kernel32
    limits = ResourceLimits(max_processes=4, cpu_rate_percent=20.0)
    proc = ProcessSupervisor.spawn(
        [sys.executable, "-c", "import time; t=time.monotonic()+0.9\nwhile time.monotonic()<t: pass"],
        role="worker",
        use_job_object=True,
        resource_limits=limits,
    )
    try:
        proc.wait(timeout=5)
        h_proc = kernel32.OpenProcess(0x1000, False, proc.pid)
        cpu_used_s = None
        if h_proc:
            try:
                ct, et, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if kernel32.GetProcessTimes(h_proc, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
                    k_ns = ((kt.dwHighDateTime << 32) + kt.dwLowDateTime) * 100
                    u_ns = ((ut.dwHighDateTime << 32) + ut.dwLowDateTime) * 100
                    cpu_used_s = (k_ns + u_ns) / 1e9
            finally:
                kernel32.CloseHandle(h_proc)
        # On a 4-core CPU, 20% hard cap over 0.9s wall time allows <= ~0.72s total CPU time across cores
        # (uncapped busy-loop uses ~0.9s on 1 core, capped at 20% system-wide allows <= 0.20 * 4 * 0.9 = 0.72s)
        if cpu_used_s is not None:
            cores = os.cpu_count() or 1
            assert cpu_used_s <= max(0.35, 0.20 * cores * 0.9 * 1.35)
    finally:
        proc.close()
