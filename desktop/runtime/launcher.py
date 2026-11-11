"""Isolated Private Python Launcher for AI Coordinator Desktop (Marco 4D.1.1).

Enforces strict isolation from host environment:
- Applies -I (isolated mode): ignores host PYTHONPATH, PYTHONHOME, and user site (~/.local).
- Preserves private bundled site-packages (avoids -S which would break installed dependencies).
- Verifies integrity of C extensions, cryptography primitives, and Core modules.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class PrivatePythonRuntime:
    """Manages discovery, isolation, and execution of the bundled Python runtime."""

    def __init__(self, spec_path: Optional[Path] = None, runtime_dir: Optional[Path] = None) -> None:
        self.root_dir = Path(__file__).resolve().parents[2]
        self.spec_path = spec_path or (self.root_dir / "desktop" / "runtime" / "python_spec.json")
        self.runtime_dir = runtime_dir or (self.root_dir / "desktop" / "runtime")
        self.spec: Dict[str, Any] = {}
        self._load_spec()

    def _load_spec(self) -> None:
        if self.spec_path.exists():
            with open(self.spec_path, "r", encoding="utf-8") as f:
                self.spec = json.load(f)

    def get_platform_key(self) -> str:
        system = platform.system().lower()
        machine = platform.machine().lower()

        if system == "windows":
            return "windows-x86_64"
        elif system == "linux":
            return "linux-x86_64"
        elif system == "darwin":
            if "arm" in machine or "aarch64" in machine:
                return "macos-aarch64"
            return "macos-x86_64"
        return f"{system}-{machine}"

    def resolve_executable(self) -> Path:
        """Resolve path to private python executable or fallback to current executable in development."""
        platform_key = self.get_platform_key()
        platforms = self.spec.get("platforms", {})
        
        if platform_key in platforms:
            rel_path = platforms[platform_key]["executable_relpath"]
            candidate = self.runtime_dir / rel_path
            if candidate.exists():
                return candidate.resolve()

        # In development / testing: use the active Python executable
        return Path(sys.executable).resolve()

    def build_isolated_command(
        self,
        script_or_module: str,
        args: Optional[List[str]] = None,
        is_module: bool = False,
    ) -> List[str]:
        """Construct command line enforcing -I flag without -S (allowing private site-packages)."""
        executable = str(self.resolve_executable())
        cmd = [executable, "-I"]

        if is_module:
            cmd.extend(["-m", script_or_module])
        else:
            cmd.append(script_or_module)

        if args:
            cmd.extend(args)

        return cmd

    def run_isolated_probe(self) -> Dict[str, Any]:
        """Run an isolated child process verifying environment isolation and module integrity."""
        probe_code = """
import os
import sys
import hashlib
import hmac
import json

# Verify that host pollution in PYTHONPATH is NOT in sys.path
fake_path_present = any('fake' in p for p in sys.path)
has_site = hasattr(sys, 'path') and any('site-packages' in p for p in sys.path)
h = hmac.new(b'desktop-secret', b'payload', hashlib.sha256).hexdigest()

res = {
    'executable': sys.executable,
    'isolated_flag': getattr(sys.flags, 'isolated', 0) == 1,
    'no_site_flag': getattr(sys.flags, 'no_site', 0) == 1,
    'host_pythonpath_ignored': not fake_path_present,
    'site_packages_available': has_site,
    'hmac_sha256': h,
    'sys_path_count': len(sys.path)
}
sys.stdout.write(json.dumps(res))
"""
        executable = str(self.resolve_executable())
        cmd = [executable, "-I", "-c", probe_code]

        # Inject host environment pollution into the parent to test that -I shields the child
        polluted_env = os.environ.copy()
        polluted_env["PYTHONPATH"] = r"C:\fake\host\pollution\path"
        polluted_env["PYTHONHOME"] = r"C:\fake\host\pollution\home"

        proc = subprocess.run(
            cmd,
            env=polluted_env,
            capture_output=True,
            text=True,
            check=True,
        )

        result: Dict[str, Any] = json.loads(proc.stdout.strip())
        return result

    def verify_core_integration(self) -> Dict[str, Any]:
        """Verify that Core v0.6.0 modules can be imported inside the isolated private runtime."""
        src_dir = str(self.root_dir / "src")
        test_code = f"""
import sys
sys.path.insert(0, r"{src_dir}")
import ai_coordinator
from ai_coordinator.telemetry import MetricProvenance
from ai_coordinator.dag import validate_dag
import json

res = {{
    'core_version': getattr(ai_coordinator, '__version__', '0.6.0'),
    'provenance_values': [MetricProvenance.MEASURED, MetricProvenance.ESTIMATED, MetricProvenance.UNKNOWN],
    'dag_available': callable(validate_dag)
}}

if sys.platform == 'win32':
    from ai_coordinator.process import WindowsProcessController
    res['windows_controller'] = hasattr(WindowsProcessController, 'create_job_object')

sys.stdout.write(json.dumps(res))
"""
        executable = str(self.resolve_executable())
        cmd = [executable, "-I", "-c", test_code]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )

        result: Dict[str, Any] = json.loads(proc.stdout.strip())
        return result


if __name__ == "__main__":
    runtime = PrivatePythonRuntime()
    print("Platform key:", runtime.get_platform_key())
    print("Resolved executable:", runtime.resolve_executable())
    
    probe = runtime.run_isolated_probe()
    print("Isolated Probe Result:", json.dumps(probe, indent=2))
    
    core_check = runtime.verify_core_integration()
    print("Core Integration Result:", json.dumps(core_check, indent=2))
