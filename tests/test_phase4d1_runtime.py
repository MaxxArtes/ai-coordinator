"""Unit and failure tests for Marco 4D.1.1: Private Python Runtime & Isolation.

Verifies:
1. Platform detection and resolution of python_spec.json.
2. Enforcement of -I (isolated mode) shielding from host PYTHONPATH/PYTHONHOME.
3. Preservation of private site-packages (no -S flag).
4. Cryptographic primitives and Core v0.6.0 integration.
5. Strict MetricProvenance contract compliance (MEASURED, ESTIMATED, UNKNOWN).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "src"))

from desktop.runtime.launcher import PrivatePythonRuntime
from ai_coordinator.telemetry import MetricProvenance


def test_python_spec_schema_and_hashes():
    """Verify python_spec.json exists, has all 4 platforms and valid 64-char SHA256 hashes."""
    spec_path = ROOT_DIR / "desktop" / "runtime" / "python_spec.json"
    assert spec_path.exists(), f"Spec file missing: {spec_path}"

    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    assert spec["target_version"] == "3.11.9"
    assert "platforms" in spec

    platforms = spec["platforms"]
    expected_keys = ["windows-x86_64", "linux-x86_64", "macos-aarch64", "macos-x86_64"]
    for key in expected_keys:
        assert key in platforms, f"Missing platform {key} in python_spec.json"
        entry = platforms[key]
        assert "sha256" in entry
        assert len(entry["sha256"]) == 64, f"Invalid SHA256 length for {key}"
        assert "url" in entry and entry["url"].startswith("https://")
        assert "executable_relpath" in entry


def test_platform_key_detection():
    """Test platform key resolution matches the current host platform."""
    runtime = PrivatePythonRuntime()
    key = runtime.get_platform_key()
    system = platform.system().lower()

    if system == "windows":
        assert key == "windows-x86_64"
    elif system == "linux":
        assert key == "linux-x86_64"
    elif system == "darwin":
        assert key.startswith("macos-")


def test_build_isolated_command_flags():
    """Verify build_isolated_command applies -I and does NOT apply -S."""
    runtime = PrivatePythonRuntime()
    cmd = runtime.build_isolated_command("my_script.py", args=["--arg1", "val1"])

    assert "-I" in cmd, "Flag -I must be present for environment isolation"
    assert "-S" not in cmd, "Flag -S must NOT be present to allow private site-packages"
    assert "my_script.py" in cmd
    assert "--arg1" in cmd


def test_runtime_probe_isolated_from_host_pollution():
    """Verify that host environment variables (PYTHONPATH, PYTHONHOME) are ignored by child process."""
    runtime = PrivatePythonRuntime()
    probe = runtime.run_isolated_probe()

    assert probe["isolated_flag"] is True, "Child process was not started in isolated mode (-I)"
    assert probe["no_site_flag"] is False, "Child process incorrectly disabled site-packages (-S)"
    assert probe["host_pythonpath_ignored"] is True, "Host PYTHONPATH polluted child sys.path"
    assert probe["site_packages_available"] is True, "Private site-packages missing from sys.path"
    assert len(probe["hmac_sha256"]) == 64, "Crypto HMAC primitive calculation failed"


def test_core_v060_integration_in_isolated_runtime():
    """Verify Core v0.6.0 imports cleanly and MetricProvenance matches canonical values."""
    runtime = PrivatePythonRuntime()
    res = runtime.verify_core_integration()

    assert res["core_version"] == "0.6.0"
    assert res["provenance_values"] == ["measured", "estimated", "unknown"]
    assert res["dag_available"] is True
    if sys.platform == "win32":
        assert res.get("windows_controller") is True
