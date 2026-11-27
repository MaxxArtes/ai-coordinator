"""Shared isolation for the test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_local_provider_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """The IPC handler's shared reader never discovers the CLI sessions of the machine running the tests."""
    try:
        from desktop.ipc import provider_adapters
    except ImportError:
        return
    monkeypatch.setattr(
        provider_adapters,
        "_DEFAULT_READER",
        provider_adapters.LocalProviderReader(provider_adapters.LocalSourcePaths()),
    )


@pytest.fixture(autouse=True)
def _deterministic_governor_baseline(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Isolate global governor DB per test and keep non-governor tests deterministic."""
    import os
    if not os.environ.get("AI_COORDINATOR_GLOBAL_GOVERNOR_DIR"):
        monkeypatch.setenv("AI_COORDINATOR_GLOBAL_GOVERNOR_DIR", str(tmp_path / "global_governor"))
    if request.path.name == "test_resource_governor.py":
        return
    from ai_coordinator import governor

    monkeypatch.setattr(
        governor,
        "probe_system_snapshot",
        lambda *args, **kwargs: governor.SystemSnapshot(
            cpu_used_pct=25.0,
            cpu_free_pct=75.0,
            ram_used_pct=40.0,
            ram_free_pct=60.0,
            ram_total_mb=16384,
            ram_avail_mb=9830,
            disk_free_gb=100.0,
            disk_used_pct=50.0,
            observed_at="2026-11-27T21:00:00.000000Z",
            probe_duration_ms=1.0,
        ),
    )
