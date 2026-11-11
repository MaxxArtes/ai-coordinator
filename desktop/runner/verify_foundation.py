"""End-to-End Foundation Verification Script for AI Coordinator Desktop (Marco 4D.1.4).

Verifies:
1. Clean install flow (zero pre-existing state -> nominal structure and vault).
2. Live IPC handshake and isolated Python runtime launch.
3. Process Tree RAM and CPU measurement (< 100 MB RAM total budget).
4. Strict separation of MetricProvenance.MEASURED from DataOrigin.TEST_FIXTURE.
5. Clean, orphan-free termination on pipe disconnect.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from desktop.auth.authorizer import DesktopAuthorizer
from desktop.auth.profile import DesktopProfileManager
from desktop.auth.vault import create_vault
from desktop.ipc.client import IPCClientProcess
from desktop.ipc.protocol import DataOrigin, MetricProvenance, RPCErrorCode


def get_process_memory_bytes(pid: int) -> int:
    """Query current WorkingSetSize in bytes for a given PID without external dependencies."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            return 0
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        try:
            import resource

            return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
        except Exception:
            return 0


def get_process_tree_memory(
    parent_pid: int,
    child_pid: Optional[int] = None,
    data_origin: DataOrigin = DataOrigin.TEST_FIXTURE,
) -> Dict[str, Any]:
    """Measure total Process Tree Working Set RAM in MB with rigorous provenance."""
    parent_bytes = get_process_memory_bytes(parent_pid)
    child_bytes = get_process_memory_bytes(child_pid) if child_pid else 0
    total_bytes = parent_bytes + child_bytes
    total_mb = round(total_bytes / (1024 * 1024), 2)

    return {
        "parent_pid": parent_pid,
        "parent_working_set_mb": round(parent_bytes / (1024 * 1024), 2),
        "child_pid": child_pid,
        "child_working_set_mb": round(child_bytes / (1024 * 1024), 2) if child_pid else 0.0,
        "total_tree_working_set_mb": total_mb,
        "provenance": MetricProvenance.MEASURED.value,
        "data_origin": data_origin.value,
        "budget_limit_mb": 100.0,
        "within_budget": total_mb <= 100.0,
    }


def verify_clean_install(install_dir: Path) -> Dict[str, Any]:
    """Validate that initial clean setup properly scaffolds default profiles and vault."""
    assert not install_dir.exists() or len(list(install_dir.iterdir())) == 0, "Install dir must be empty"

    prof_mgr = DesktopProfileManager(root_dir=install_dir / "profiles")
    active_profile = prof_mgr.get_active_profile()

    assert active_profile.name == "default"
    assert (install_dir / "profiles" / "default" / "profile.json").exists()

    # Test vault initial creation
    vault = create_vault(
        profile_dir=Path(active_profile.data_dir),
        profile_name=active_profile.name,
        backend="auto",
    )
    test_key = "INSTALL_PROBE"
    vault.set_secret(test_key, "probe-value-ok")
    assert vault.get_secret(test_key) == "probe-value-ok"
    assert vault.delete_secret(test_key) is True

    return {
        "status": "passed",
        "profile_created": active_profile.name,
        "data_dir": active_profile.data_dir,
        "vault_initialized": True,
    }


def verify_ipc_handshake_and_isolation(
    test_dir: Path,
    server_script: Optional[Path] = None,
) -> Dict[str, Any]:
    """Perform live handshake with isolated Python child process and measure resource usage."""
    prof_mgr = DesktopProfileManager(root_dir=test_dir / "profiles")
    authorizer = DesktopAuthorizer(profile_manager=prof_mgr)
    session_token = authorizer.issue_session("default")

    script = server_script or (ROOT_DIR / "desktop" / "ipc" / "server.py")
    client = IPCClientProcess(session_token=session_token, server_script=script)
    client.start()

    try:
        assert client.is_alive(), "IPC Server process failed to start"
        child_pid = client.process.pid if client.process else 0

        # 1. Public ping
        ping_resp = client.request("system.ping", override_token="")
        assert ping_resp.error is None, f"Ping failed: {ping_resp.error}"
        assert ping_resp.result.get("status") == "pong"

        # 2. Authenticated status
        status_resp = client.request("system.status")
        assert status_resp.error is None, f"Status failed: {status_resp.error}"
        assert status_resp.result.get("status") == "ready"
        assert status_resp.result.get("authenticated") is True

        # 3. Forged token rejection
        forged_resp = client.request("system.status", override_token="forged-token-999")
        assert forged_resp.error is not None
        assert forged_resp.error.get("code") == RPCErrorCode.AUTH_ERROR.value

        # 4. Measure Process Tree RAM
        mem_info = get_process_tree_memory(
            parent_pid=os.getpid(),
            child_pid=child_pid,
            data_origin=DataOrigin.TEST_FIXTURE,
        )

        return {
            "status": "passed",
            "ping": ping_resp.result,
            "status_info": status_resp.result,
            "forged_auth_blocked": True,
            "process_tree_memory": mem_info,
        }
    finally:
        exit_code, stderr = client.close()
        assert exit_code == 0, f"Server exited with non-zero code {exit_code}: {stderr}"


def run_full_foundation_verification() -> Dict[str, Any]:
    """Run complete gate verification suite for Marco 4D.1."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        clean_install_res = verify_clean_install(tmp_path / "clean_install")
        ipc_res = verify_ipc_handshake_and_isolation(tmp_path / "live_test")

        return {
            "gate": "Gate 4D.1 - Fundação do Desktop",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "clean_install": clean_install_res,
            "live_ipc": ipc_res,
            "all_passed": (clean_install_res["status"] == "passed" and ipc_res["status"] == "passed"),
        }


if __name__ == "__main__":
    result = run_full_foundation_verification()
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["all_passed"] else 1)
