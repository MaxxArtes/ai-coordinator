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
