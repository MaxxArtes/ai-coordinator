"""End-to-End Integration and Gate Tests for Marco 4D.1: Desktop Foundation (Marco 4D.1.4).

Verifies:
- Clean installation scaffolding in pristine environment.
- Live IPC handshake with isolated Python runtime and process tree memory measurement.
- Core v0.6.0 telemetry invariants (MetricProvenance) preserved without enum corruption.
- Full Gate 4D.1 readiness verification.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import pytest

from ai_coordinator.telemetry import MetricProvenance as CoreProvenance
from desktop.auth.authorizer import DesktopAuthorizer, SecurityAuthorizationError
from desktop.auth.profile import DesktopProfileManager
from desktop.ipc.protocol import DataOrigin, MetricProvenance as DesktopProvenance
from desktop.runner.verify_foundation import (
    run_full_foundation_verification,
    verify_clean_install,
    verify_ipc_handshake_and_isolation,
)


def test_e2e_clean_installation():
    with tempfile.TemporaryDirectory() as tmpdir:
        res = verify_clean_install(Path(tmpdir) / "pristine_env")
        assert res["status"] == "passed"
        assert res["profile_created"] == "default"
        assert res["vault_initialized"] is True


def test_e2e_live_ipc_and_tree_memory():
    with tempfile.TemporaryDirectory() as tmpdir:
        res = verify_ipc_handshake_and_isolation(Path(tmpdir))
        assert res["status"] == "passed"
        assert res["ping"]["status"] == "pong"
        assert res["status_info"]["status"] == "ready"
        assert res["forged_auth_blocked"] is True

        mem = res["process_tree_memory"]
        assert mem["provenance"] == CoreProvenance.MEASURED
        assert mem["data_origin"] == DataOrigin.TEST_FIXTURE.value
        assert mem["total_tree_working_set_mb"] < 100.0
        assert mem["within_budget"] is True


def test_process_memory_is_measured_per_pid():
    """Each pid is measured on its own (regression: POSIX used the caller's rusage for every pid)."""
    import subprocess
    import sys

    from desktop.runner.verify_foundation import get_process_memory_bytes

    alloc_mb = 64
    child = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            # Touch every page so the allocation is resident on every OS, then report and wait.
            f"import sys, time; b = bytearray(b'\\x01') * ({alloc_mb} * 1024 * 1024); "
            "print('READY', flush=True); sys.stdin.readline()",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        child_bytes = get_process_memory_bytes(child.pid)
        self_bytes = get_process_memory_bytes(os.getpid())
        assert self_bytes > 0
        # The child holds >= 64 MB resident; it must be reported for the child's pid itself.
        assert child_bytes >= (alloc_mb - 8) * 1024 * 1024
        # Plausible magnitude (bytes, not kB*1024 of bytes as macOS ru_maxrss would give).
        assert child_bytes < 4 * 1024 * 1024 * 1024
    finally:
        child.stdin.close()
        child.wait(timeout=30)


def test_e2e_full_foundation_gate_runner():
    res = run_full_foundation_verification()
    assert res["gate"] == "Gate 4D.1 - Fundação do Desktop"
    assert res["all_passed"] is True
    assert res["clean_install"]["status"] == "passed"
    assert res["live_ipc"]["status"] == "passed"


def test_e2e_core_v060_telemetry_invariants():
    """Ensure Core v0.6.0 telemetry contract is preserved exactly."""
    assert CoreProvenance.MEASURED == DesktopProvenance.MEASURED.value
    assert CoreProvenance.ESTIMATED == DesktopProvenance.ESTIMATED.value
    assert CoreProvenance.UNKNOWN == DesktopProvenance.UNKNOWN.value

    # Provenance classifications do NOT include TEST_FIXTURE (which belongs to DataOrigin)
    assert "test_fixture" not in CoreProvenance.ALL
    assert DesktopProvenance.MEASURED.value in CoreProvenance.ALL


def test_e2e_profile_fencing_against_directory_escape():
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = DesktopProfileManager(root_dir=Path(tmpdir) / "profiles")
        authorizer = DesktopAuthorizer(profile_manager=mgr)
        token = authorizer.issue_session("default")

        authorized_dir = Path(tmpdir) / "allowed_project"
        authorized_dir.mkdir(parents=True, exist_ok=True)
        mgr.authorize_project("default", authorized_dir)

        # Authorized path succeeds
        assert authorizer.authorize_project_access(token, authorized_dir) is True
        assert authorizer.authorize_project_access(token, authorized_dir / "subdir") is True

        # Escaping to sibling directory fails fail-closed
        forbidden_dir = Path(tmpdir) / "forbidden_secrets"
        forbidden_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(SecurityAuthorizationError):
            authorizer.authorize_project_access(token, forbidden_dir)
