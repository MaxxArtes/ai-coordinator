"""Real OS process-tree & system resource collector for AI Coordinator Desktop (Phase 4D.4.1).

Collects host/core/webview/terminal/agent/child and system-wide resource usage with:
- Clear differentiation between Private Working Set (exclusive physical RAM), Working Set total, and shared RAM
  (never presenting summed resident/Working Set pages as exclusive physical memory).
- Temporal CPU sampling guarded by a configurable minimum interval (`min_sample_interval_ms`, default 200 ms).
- Explicit provenance (`measured`, `estimated`, `unknown`) and data origin (`live_os`, `offline_cache`, `test_fixture`).
- Stable `(pid, create_time_ns)` process identity to protect against PID reuse, exits, and restarts.
- Safe AI agent identification via allowlisted executable names, the command line of known wrappers (node, bun,
  python, cmd, PowerShell; read once per `(pid, create_time_ns)`) + governed session tracking.
- System-wide CPU, RAM, storage capacity (separate from disk I/O activity), network throughput, and GPU when supported.
"""

from __future__ import annotations

import atexit
import ctypes
import glob
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from desktop.ipc.protocol import DataOrigin, MetricProvenance, TELEMETRY_SCHEMA_VERSION

DEFAULT_MIN_SAMPLE_INTERVAL_MS = 200
MIN_ALLOWED_SAMPLE_INTERVAL_MS = 50
MAX_ALLOWED_SAMPLE_INTERVAL_MS = 60_000
DEFAULT_TELEMETRY_TTL_MS = 10_000
VALID_ROLES = ("host", "core", "webview", "terminal", "agent", "child")

SHARED_RAM_NON_ADDITIVE_NOTE = (
    "Memória compartilhada somada por processo não é aditiva entre processos "
    "(páginas compartilhadas como DLLs do WebView2 aparecem em múltiplos processos)."
)

# AI agent detection. Same tables and rule as `detect_agent_kind` in desktop/src-tauri/src/main.rs
# (tests/test_phase4d4_agent_detection.py compares both).
# Executable stems: exact or followed by "-"/"_" (opencode-cli.exe, codex-code-mode-host.exe). Scripts run by a wrapper
# must match exactly (codex.js, claude.ps1, aider.exe): gemini_web.py or claude-notes.js are not agents.
_AGENT_EXECUTABLE_MAP: Tuple[Tuple[str, str], ...] = (
    ("claude", "claude"),
    ("codex", "codex"),
    ("opencode", "opencode"),
    ("antigravity", "agy"),
    ("agy", "agy"),
    ("gemini", "gemini"),
    ("aider", "aider"),
)
# Exact stems only: ai-coordinator-desktop.exe is the host, not an agent.
_AGENT_EXACT_STEMS: Tuple[Tuple[str, str], ...] = (("ai-coordinator", "ai-coordinator"),)
# npm packages: bare spec for npx/bunx (optionally @version) or a path inside node_modules.
_AGENT_PACKAGE_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("@anthropic-ai/claude-code", "claude"),
    ("@openai/codex", "codex"),
    ("opencode-ai", "opencode"),
    ("@google/gemini-cli", "gemini"),
)
_AGENT_PYTHON_MODULES: Tuple[Tuple[str, str], ...] = (("aider", "aider"), ("ai_coordinator", "ai-coordinator"))
_AGENT_NAME_EXTENSIONS: Tuple[str, ...] = (".exe", ".cmd", ".bat", ".bin", ".ps1", ".js", ".mjs", ".cjs", ".py", ".pyw")
# Wrappers whose command line is inspected; every other process is identified by its executable name alone.
_AGENT_JS_LAUNCHERS: Tuple[str, ...] = ("node", "nodejs", "bun", "bunx")
_AGENT_PYTHON_LAUNCHERS: Tuple[str, ...] = ("py", "pyw")
_AGENT_CMD_LAUNCHERS: Tuple[str, ...] = ("cmd",)
_AGENT_POWERSHELL_LAUNCHERS: Tuple[str, ...] = ("pwsh", "powershell")
_AGENT_JS_INLINE_FLAGS: Tuple[str, ...] = ("-e", "--eval", "-p", "--print", "-")
_AGENT_JS_VALUE_FLAGS: Tuple[str, ...] = (
    "-r",
    "--require",
    "--import",
    "--loader",
    "--experimental-loader",
    "--cwd",
    "--env-file",
)
_AGENT_BUN_SUBCOMMANDS: Tuple[str, ...] = ("run", "x", "exec")
_AGENT_PYTHON_VALUE_FLAGS: Tuple[str, ...] = ("-X", "-W", "--check-hash-based-pycs")
_AGENT_CMD_SWITCHES: Tuple[str, ...] = ("/c", "/k", "/r")
_AGENT_POWERSHELL_FILE_FLAGS: Tuple[str, ...] = ("-file", "-f")
_AGENT_POWERSHELL_COMMAND_FLAGS: Tuple[str, ...] = ("-command", "-c")
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _normalize_agent_token(raw: str) -> str:
    return (raw or "").strip().strip("\"'").strip().replace("\\", "/").translate(_ASCII_LOWER)


def _strip_agent_extension(base: str) -> str:
    for ext in _AGENT_NAME_EXTENSIONS:
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def _agent_kind_from_token(token: str, exact_only: bool) -> Optional[str]:
    stem = _strip_agent_extension(token.rsplit("/", 1)[-1])
    if not stem or "." in stem:
        return None
    for exact, kind in _AGENT_EXACT_STEMS:
        if stem == exact:
            return kind
    for prefix, kind in _AGENT_EXECUTABLE_MAP:
        if stem == prefix:
            return kind
        if not exact_only and (stem.startswith(f"{prefix}-") or stem.startswith(f"{prefix}_")):
            return kind
    return None


def _agent_kind_from_package(token: str) -> Optional[str]:
    for marker, kind in _AGENT_PACKAGE_MARKERS:
        if (
            token == marker
            or token.startswith(f"{marker}@")
            or token.startswith(f"{marker}/")
            or f"node_modules/{marker}/" in token
        ):
            return kind
    return None


def _agent_kind_from_script(raw: str) -> Optional[str]:
    token = _normalize_agent_token(raw)
    if not token:
        return None
    return _agent_kind_from_package(token) or _agent_kind_from_token(token, True)


def _agent_kind_from_module(raw: str) -> Optional[str]:
    root = _normalize_agent_token(raw).split(".", 1)[0]
    for module, kind in _AGENT_PYTHON_MODULES:
        if root == module:
            return kind
    return None


def _first_command_token(text: str) -> str:
    s = text.strip()
    if s.startswith("&"):
        s = s[1:].strip()
    quoted = s[:1] in ("\"", "'")
    s = s.lstrip("\"'")
    for idx, ch in enumerate(s):
        if ch in "\"'" or (not quoted and ch.isspace()):
            return s[:idx]
    return s


def _agent_kind_from_js_args(args: Sequence[str], allow_subcommands: bool) -> Optional[str]:
    i = 0
    while i < len(args):
        arg = args[i].strip()
        low = arg.translate(_ASCII_LOWER)
        if low in _AGENT_JS_INLINE_FLAGS:
            return None
        if low in _AGENT_JS_VALUE_FLAGS:
            i += 2
            continue
        if low.startswith("-") or (allow_subcommands and low in _AGENT_BUN_SUBCOMMANDS):
            i += 1
            continue
        return _agent_kind_from_script(arg)
    return None


def _agent_kind_from_python_args(args: Sequence[str]) -> Optional[str]:
    i = 0
    while i < len(args):
        arg = args[i].strip()
        if arg == "-m":
            return _agent_kind_from_module(args[i + 1]) if i + 1 < len(args) else None
        if arg.startswith("-m") and len(arg) > 2:
            return _agent_kind_from_module(arg[2:])
        if arg in ("-c", "-"):
            return None
        if arg in _AGENT_PYTHON_VALUE_FLAGS:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return _agent_kind_from_script(arg)
    return None


def _agent_kind_from_cmd_args(args: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(args):
        if arg.strip().translate(_ASCII_LOWER) in _AGENT_CMD_SWITCHES:
            return _agent_kind_from_script(_first_command_token(" ".join(args[i + 1 :])))
    return None


def _agent_kind_from_powershell_args(args: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(args):
        low = arg.strip().translate(_ASCII_LOWER)
        if low in _AGENT_POWERSHELL_FILE_FLAGS:
            return _agent_kind_from_script(args[i + 1]) if i + 1 < len(args) else None
        if low in _AGENT_POWERSHELL_COMMAND_FLAGS:
            return _agent_kind_from_script(_first_command_token(" ".join(args[i + 1 :])))
    return None


def _is_python_launcher(stem: str) -> bool:
    if stem in _AGENT_PYTHON_LAUNCHERS:
        return True
    if not stem.startswith("python"):
        return False
    rest = stem[len("python") :]
    if rest.endswith("w"):
        rest = rest[:-1]
    return all(ch in "0123456789." for ch in rest)


def _agent_launcher_stem(process_name: str) -> str:
    return _strip_agent_extension(_normalize_agent_token(process_name).rsplit("/", 1)[-1])


def agent_needs_cmdline(process_name: str) -> bool:
    """True for wrappers (node, bun, python, cmd, PowerShell) whose agent only shows in the command line."""
    if detect_agent_kind(process_name) is not None:
        return False
    stem = _agent_launcher_stem(process_name)
    return (
        stem in _AGENT_JS_LAUNCHERS
        or _is_python_launcher(stem)
        or stem in _AGENT_CMD_LAUNCHERS
        or stem in _AGENT_POWERSHELL_LAUNCHERS
    )


def detect_agent_kind(process_name: str, cmdline: Optional[Sequence[str]] = None) -> Optional[str]:
    """Identify AI agents by executable name, then by a known wrapper's command line (argv[0] skipped).

    Only the kind is derived; arguments are never kept or returned. A wrapper whose command line could not be read
    (no permission, process gone) stays unidentified.
    """
    token = _normalize_agent_token(process_name)
    if not token:
        return None
    kind = _agent_kind_from_token(token, False)
    if kind is not None:
        return kind
    args = list(cmdline or ())[1:]
    if not args:
        return None
    stem = _agent_launcher_stem(process_name)
    if stem in _AGENT_JS_LAUNCHERS:
        return _agent_kind_from_js_args(args, stem.startswith("bun"))
    if _is_python_launcher(stem):
        return _agent_kind_from_python_args(args)
    if stem in _AGENT_CMD_LAUNCHERS:
        return _agent_kind_from_cmd_args(args)
    if stem in _AGENT_POWERSHELL_LAUNCHERS:
        return _agent_kind_from_powershell_args(args)
    return None


@dataclass(frozen=True)
class RawProcessSample:
    """Single OS observation for one process at one point in time."""

    pid: int
    ppid: Optional[int]
    name: str
    create_time_ns: Optional[int]
    ram_bytes: Optional[int]  # Total resident / Working Set bytes
    cpu_time_seconds: Optional[float]
    private_ram_bytes: Optional[int] = None
    shared_ram_bytes: Optional[int] = None


@dataclass(frozen=True)
class StorageVolumeMetrics:
    """Storage capacity and utilization for one mounted volume (distinct from disk I/O activity)."""

    name: str
    mount_point: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    usage_percent: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "mount_point": self.mount_point,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "available_bytes": self.available_bytes,
            "usage_percent": self.usage_percent,
        }


@dataclass(frozen=True)
class SystemResourcesMetrics:
    """Host computer general resources (strictly separated from the AI Coordinator process tree)."""

    logical_cpu_count: int
    cpu_usage_percent: Optional[float] = None
    cpu_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    ram_total_bytes: int = 0
    ram_used_bytes: int = 0
    ram_available_bytes: int = 0
    ram_usage_percent: Optional[float] = None
    ram_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    storage_total_bytes: int = 0
    storage_used_bytes: int = 0
    storage_available_bytes: int = 0
    storage_usage_percent: Optional[float] = None
    storage_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    storage_volumes: List[StorageVolumeMetrics] = field(default_factory=list)
    disk_activity_percent: Optional[float] = None
    disk_activity_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    disk_read_bytes_per_sec: Optional[int] = None
    disk_write_bytes_per_sec: Optional[int] = None
    disk_io_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    network_rx_bytes_per_sec: Optional[int] = None
    network_tx_bytes_per_sec: Optional[int] = None
    network_total_bytes_per_sec: Optional[int] = None
    network_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    gpu_usage_percent: Optional[float] = None
    gpu_provenance: MetricProvenance = MetricProvenance.UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logical_cpu_count": self.logical_cpu_count,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_provenance": self.cpu_provenance.value,
            "ram_total_bytes": self.ram_total_bytes,
            "ram_used_bytes": self.ram_used_bytes,
            "ram_available_bytes": self.ram_available_bytes,
            "ram_usage_percent": self.ram_usage_percent,
            "ram_provenance": self.ram_provenance.value,
            "storage_total_bytes": self.storage_total_bytes,
            "storage_used_bytes": self.storage_used_bytes,
            "storage_available_bytes": self.storage_available_bytes,
            "storage_usage_percent": self.storage_usage_percent,
            "storage_provenance": self.storage_provenance.value,
            "storage_volumes": [v.to_dict() for v in self.storage_volumes],
            "disk_activity_percent": self.disk_activity_percent,
            "disk_activity_provenance": self.disk_activity_provenance.value,
            "disk_read_bytes_per_sec": self.disk_read_bytes_per_sec,
            "disk_write_bytes_per_sec": self.disk_write_bytes_per_sec,
            "disk_io_provenance": self.disk_io_provenance.value,
            "network_rx_bytes_per_sec": self.network_rx_bytes_per_sec,
            "network_tx_bytes_per_sec": self.network_tx_bytes_per_sec,
            "network_total_bytes_per_sec": self.network_total_bytes_per_sec,
            "network_provenance": self.network_provenance.value,
            "gpu_usage_percent": self.gpu_usage_percent,
            "gpu_provenance": self.gpu_provenance.value,
        }


@dataclass(frozen=True)
class ProcessNodeMetrics:
    """Measured/unknown resource telemetry for one deduplicated process node."""

    pid: int
    ppid: Optional[int]
    create_time_ns: Optional[int]
    role: str
    name: str
    ram_bytes: Optional[int]
    ram_provenance: MetricProvenance
    cpu_usage_percent: Optional[float]
    cpu_provenance: MetricProvenance
    private_ram_bytes: Optional[int] = None
    private_ram_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    working_set_ram_bytes: Optional[int] = None
    shared_ram_bytes: Optional[int] = None
    agent_kind: Optional[str] = None
    session_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "create_time_ns": self.create_time_ns,
            "role": self.role,
            "name": self.name,
            "ram_bytes": self.ram_bytes,
            "ram_provenance": self.ram_provenance.value,
            "private_ram_bytes": self.private_ram_bytes,
            "private_ram_provenance": self.private_ram_provenance.value,
            "working_set_ram_bytes": self.working_set_ram_bytes,
            "shared_ram_bytes": self.shared_ram_bytes,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_provenance": self.cpu_provenance.value,
            "agent_kind": self.agent_kind,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class CategoryResourceMetrics:
    """Non-overlapping breakdown for one process role category."""

    role: str
    process_count: int
    ram_bytes: int
    ram_provenance: MetricProvenance
    cpu_usage_percent: Optional[float]
    cpu_provenance: MetricProvenance
    private_ram_bytes: Optional[int] = None
    private_ram_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    working_set_ram_bytes: int = 0
    shared_ram_bytes: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "process_count": self.process_count,
            "ram_bytes": self.ram_bytes,
            "ram_provenance": self.ram_provenance.value,
            "private_ram_bytes": self.private_ram_bytes,
            "private_ram_provenance": self.private_ram_provenance.value,
            "working_set_ram_bytes": self.working_set_ram_bytes,
            "shared_ram_bytes": self.shared_ram_bytes,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_provenance": self.cpu_provenance.value,
        }


@dataclass(frozen=True)
class ProcessTreeMetricsSnapshot:
    """Typed telemetry snapshot for the desktop process tree and optional system host metrics."""

    root_pid: Optional[int]
    host_pid: Optional[int]
    core_pid: Optional[int]
    host_ram_bytes: int
    total_tree_ram_bytes: int
    ram_provenance: MetricProvenance
    cpu_usage_percent: Optional[float]
    cpu_provenance: MetricProvenance
    sample_interval_ms: Optional[int]
    process_count: int
    sampled_at_unix_ms: int
    categories: Dict[str, CategoryResourceMetrics] = field(default_factory=dict)
    nodes: List[ProcessNodeMetrics] = field(default_factory=list)
    schema_version: int = TELEMETRY_SCHEMA_VERSION
    data_origin: DataOrigin = DataOrigin.LIVE_OS
    ttl_ms: int = DEFAULT_TELEMETRY_TTL_MS
    logical_cpu_count: int = 1
    host_private_ram_bytes: Optional[int] = None
    total_private_ram_bytes: Optional[int] = None
    private_ram_provenance: MetricProvenance = MetricProvenance.UNKNOWN
    total_working_set_ram_bytes: int = 0
    total_shared_ram_bytes: Optional[int] = None
    shared_ram_note: str = SHARED_RAM_NON_ADDITIVE_NOTE
    system: Optional[SystemResourcesMetrics] = None

    def to_dict(self, *, include_nodes: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "data_origin": self.data_origin.value,
            "ttl_ms": self.ttl_ms,
            "stale": False,
            "root_pid": self.root_pid,
            "host_pid": self.host_pid,
            "core_pid": self.core_pid,
            "host_ram_bytes": self.host_ram_bytes,
            "host_private_ram_bytes": self.host_private_ram_bytes,
            "total_tree_ram_bytes": self.total_tree_ram_bytes,
            "ram_provenance": self.ram_provenance.value,
            "total_private_ram_bytes": self.total_private_ram_bytes,
            "private_ram_provenance": self.private_ram_provenance.value,
            "total_working_set_ram_bytes": self.total_working_set_ram_bytes,
            "total_shared_ram_bytes": self.total_shared_ram_bytes,
            "shared_ram_note": self.shared_ram_note,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_provenance": self.cpu_provenance.value,
            "logical_cpu_count": self.logical_cpu_count,
            "sample_interval_ms": self.sample_interval_ms,
            "process_count": self.process_count,
            "sampled_at_unix_ms": self.sampled_at_unix_ms,
            "categories": {k: v.to_dict() for k, v in self.categories.items()},
            "system": self.system.to_dict() if self.system is not None else None,
        }
        if include_nodes:
            payload["nodes"] = [node.to_dict() for node in self.nodes]
        return payload


# ---------------------------------------------------------------------------
# Cross-platform OS Process & System Inspection (stdlib + native OS APIs only)
# ---------------------------------------------------------------------------

_GPU_ENGINE_RE = re.compile(r"(luid_0x[0-9a-fA-F]+_0x[0-9a-fA-F]+_phys_\d+_eng_\d+)")


def aggregate_gpu_engine_values(samples: Iterable[Tuple[str, float]]) -> Optional[float]:
    """Sum utilization across PIDs per hardware GPU engine, then take the max engine utilization (0..100%)."""
    per_engine: Dict[str, float] = {}
    any_valid = False
    for name, val in samples:
        if val is None or val != val or val < 0.0:
            continue
        any_valid = True
        m = _GPU_ENGINE_RE.search(name or "")
        key = m.group(1) if m else (name or "gpu")
        per_engine[key] = per_engine.get(key, 0.0) + float(val)
    if not any_valid or not per_engine:
        return None
    peak = max(per_engine.values())
    return round(min(100.0, max(0.0, peak)), 2)


if os.name == "nt":
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    # Windows FILETIME epoch (1601-01-01) -> Unix epoch (1970-01-01) in 100-ns ticks
    _WINDOWS_TO_UNIX_EPOCH_TICKS = 116_444_736_000_000_000
    _PDH_FMT_DOUBLE = 0x00000200
    _PDH_FMT_LARGE = 0x00000400
    _PDH_MORE_DATA = 0x800007D2

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
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

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    class _PDH_FMT_COUNTERVALUE_U(ctypes.Union):
        _fields_ = [
            ("longValue", ctypes.c_long),
            ("doubleValue", ctypes.c_double),
            ("largeValue", ctypes.c_longlong),
            ("AnsiStringValue", ctypes.c_char_p),
            ("WideStringValue", ctypes.c_wchar_p),
        ]

    class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
        _fields_ = [("CStatus", wintypes.DWORD), ("u", _PDH_FMT_COUNTERVALUE_U)]

    class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
        _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _PDH_FMT_COUNTERVALUE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetSystemTimes.argtypes = [
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.GetSystemTimes.restype = wintypes.BOOL
    _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    _kernel32.GetLogicalDrives.argtypes = []
    _kernel32.GetLogicalDrives.restype = wintypes.DWORD
    _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetDriveTypeW.restype = wintypes.UINT
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    try:
        _psapi = ctypes.WinDLL("psapi", use_last_error=True)
        _get_process_memory_info = _psapi.GetProcessMemoryInfo
    except OSError:
        _get_process_memory_info = _kernel32.K32GetProcessMemoryInfo
    _get_process_memory_info.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    ]
    _get_process_memory_info.restype = wintypes.BOOL

    try:
        _pdh = ctypes.WinDLL("pdh", use_last_error=True)
        _pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        _pdh.PdhOpenQueryW.restype = wintypes.LONG
        _pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
        _pdh.PdhAddEnglishCounterW.restype = wintypes.LONG
        _pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
        _pdh.PdhCollectQueryData.restype = wintypes.LONG
        _pdh.PdhGetFormattedCounterArrayW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        _pdh.PdhGetFormattedCounterArrayW.restype = wintypes.LONG
        _pdh.PdhGetFormattedCounterValue.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_PDH_FMT_COUNTERVALUE),
        ]
        _pdh.PdhGetFormattedCounterValue.restype = wintypes.LONG
        _pdh.PdhCloseQuery.argtypes = [wintypes.HANDLE]
        _pdh.PdhCloseQuery.restype = wintypes.LONG
    except OSError:
        _pdh = None

    class _WindowsPdhHelper:
        """Persistent PDH query reading Windows Private Working Set, Disk I/O, and GPU utilization."""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._query: Optional[wintypes.HANDLE] = None
            self._c_pid: Optional[wintypes.HANDLE] = None
            self._c_priv: Optional[wintypes.HANDLE] = None
            self._c_disk_pct: Optional[wintypes.HANDLE] = None
            self._c_disk_read: Optional[wintypes.HANDLE] = None
            self._c_disk_write: Optional[wintypes.HANDLE] = None
            self._c_gpu: Optional[wintypes.HANDLE] = None
            self._collect_count = 0
            if _pdh is not None:
                self._init_query()

        def _add_counter(self, path: str) -> Optional[wintypes.HANDLE]:
            if _pdh is None or not self._query:
                return None
            handle = wintypes.HANDLE()
            status = _pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(handle)) & 0xFFFFFFFF
            return handle if status == 0 else None

        def _init_query(self) -> None:
            q = wintypes.HANDLE()
            if (_pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) & 0xFFFFFFFF) != 0:
                return
            self._query = q
            self._c_pid = self._add_counter(r"\Process(*)\ID Process")
            self._c_priv = self._add_counter(r"\Process(*)\Working Set - Private")
            self._c_disk_pct = self._add_counter(r"\PhysicalDisk(_Total)\% Disk Time")
            self._c_disk_read = self._add_counter(r"\PhysicalDisk(_Total)\Disk Read Bytes/sec")
            self._c_disk_write = self._add_counter(r"\PhysicalDisk(_Total)\Disk Write Bytes/sec")
            self._c_gpu = self._add_counter(r"\GPU Engine(*)\Utilization Percentage")
            if (_pdh.PdhCollectQueryData(self._query) & 0xFFFFFFFF) == 0:
                self._collect_count = 1

        def close(self) -> None:
            with self._lock:
                if _pdh is not None and self._query:
                    try:
                        _pdh.PdhCloseQuery(self._query)
                    except Exception:
                        pass
                    self._query = None

        @staticmethod
        def _read_array(counter: Optional[wintypes.HANDLE], fmt: int) -> List[Tuple[str, float]]:
            if _pdh is None or not counter:
                return []
            sz = wintypes.DWORD(0)
            cnt = wintypes.DWORD(0)
            st = _pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(sz), ctypes.byref(cnt), None) & 0xFFFFFFFF
            if st != _PDH_MORE_DATA or sz.value == 0:
                return []
            buf = ctypes.create_string_buffer(sz.value)
            st2 = _pdh.PdhGetFormattedCounterArrayW(counter, fmt, ctypes.byref(sz), ctypes.byref(cnt), buf) & 0xFFFFFFFF
            if st2 != 0 or cnt.value == 0:
                return []
            arr = (_PDH_FMT_COUNTERVALUE_ITEM_W * cnt.value).from_buffer(buf)
            out: List[Tuple[str, float]] = []
            for idx in range(cnt.value):
                item = arr[idx]
                if item.FmtValue.CStatus != 0:
                    continue
                val = (
                    float(item.FmtValue.u.largeValue)
                    if fmt == _PDH_FMT_LARGE
                    else float(item.FmtValue.u.doubleValue)
                )
                out.append((item.szName or "", val))
            return out

        @staticmethod
        def _read_scalar(counter: Optional[wintypes.HANDLE]) -> Optional[float]:
            if _pdh is None or not counter:
                return None
            val = _PDH_FMT_COUNTERVALUE()
            st = _pdh.PdhGetFormattedCounterValue(counter, _PDH_FMT_DOUBLE, None, ctypes.byref(val)) & 0xFFFFFFFF
            if st != 0 or val.CStatus != 0:
                return None
            num = float(val.u.doubleValue)
            return num if num == num else None

        def sample(self) -> Dict[str, Any]:
            with self._lock:
                if _pdh is None or not self._query:
                    return {"private_by_pid": {}}
                if (_pdh.PdhCollectQueryData(self._query) & 0xFFFFFFFF) != 0:
                    return {"private_by_pid": {}}
                self._collect_count += 1

                # Pair PID and Private Working Set collected in the same PDH snapshot.
                priv_map: Dict[int, int] = {}
                if self._c_pid and self._c_priv:
                    pids = self._read_array(self._c_pid, _PDH_FMT_LARGE)
                    privs = self._read_array(self._c_priv, _PDH_FMT_LARGE)
                    for (p_name, p_val), (m_name, m_val) in zip(pids, privs):
                        if p_name == m_name and p_val > 0 and m_val >= 0:
                            priv_map[int(p_val)] = int(m_val)

                # Rate counters (% Disk Time, Bytes/sec, GPU %) require >= 2 collects.
                if self._collect_count < 2:
                    return {"private_by_pid": priv_map}

                disk_pct = self._read_scalar(self._c_disk_pct)
                disk_read = self._read_scalar(self._c_disk_read)
                disk_write = self._read_scalar(self._c_disk_write)
                gpu_arr = self._read_array(self._c_gpu, _PDH_FMT_DOUBLE)
                gpu_pct = aggregate_gpu_engine_values(gpu_arr)

                return {
                    "private_by_pid": priv_map,
                    "disk_activity_percent": None if disk_pct is None else round(min(100.0, max(0.0, disk_pct)), 2),
                    "disk_read_bytes_per_sec": None if disk_read is None else max(0, int(round(disk_read))),
                    "disk_write_bytes_per_sec": None if disk_write is None else max(0, int(round(disk_write))),
                    "gpu_usage_percent": gpu_pct,
                }

    _PDH_HELPER: Optional[_WindowsPdhHelper] = None
    _PDH_HELPER_LOCK = threading.Lock()

    def _get_pdh_helper() -> _WindowsPdhHelper:
        global _PDH_HELPER
        with _PDH_HELPER_LOCK:
            if _PDH_HELPER is None:
                _PDH_HELPER = _WindowsPdhHelper()
                atexit.register(_PDH_HELPER.close)
            return _PDH_HELPER

    def _filetime_to_ticks(ft: _FILETIME) -> int:
        return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)

    def _query_win32_process_metrics(pid: int) -> Tuple[Optional[int], Optional[int], Optional[float], str]:
        if pid <= 0:
            return None, None, None, ""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None, None, None, ""
        try:
            create_ft, exit_ft, kernel_ft, user_ft = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
            create_time_ns: Optional[int] = None
            cpu_time_seconds: Optional[float] = None
            if _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(create_ft),
                ctypes.byref(exit_ft),
                ctypes.byref(kernel_ft),
                ctypes.byref(user_ft),
            ):
                c_ticks = _filetime_to_ticks(create_ft)
                if c_ticks >= _WINDOWS_TO_UNIX_EPOCH_TICKS:
                    create_time_ns = (c_ticks - _WINDOWS_TO_UNIX_EPOCH_TICKS) * 100
                cpu_ticks = _filetime_to_ticks(kernel_ft) + _filetime_to_ticks(user_ft)
                cpu_time_seconds = cpu_ticks / 10_000_000.0

            ram_bytes: Optional[int] = None
            counters = _PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if _get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
                ram_bytes = int(counters.WorkingSetSize)

            name = ""
            buf_len = wintypes.DWORD(260)
            name_buf = ctypes.create_unicode_buffer(260)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, name_buf, ctypes.byref(buf_len)):
                full_path = name_buf.value
                sep = max(full_path.rfind("\\"), full_path.rfind("/"))
                name = full_path[sep + 1 :] if sep >= 0 else full_path
            return create_time_ns, ram_bytes, cpu_time_seconds, name
        finally:
            _kernel32.CloseHandle(handle)

    _PROCESS_COMMAND_LINE_INFORMATION = 60
    _MAX_CMDLINE_BUFFER_BYTES = 1 << 20

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    _CMDLINE_API: Dict[str, Any] = {}
    _CMDLINE_API_LOCK = threading.Lock()

    def _cmdline_api() -> Optional[Tuple[Any, Any]]:
        """NtQueryInformationProcess + CommandLineToArgvW, bound on first use (shell32 is not loaded otherwise)."""
        with _CMDLINE_API_LOCK:
            if "api" not in _CMDLINE_API:
                try:
                    query = ctypes.WinDLL("ntdll").NtQueryInformationProcess
                    query.argtypes = [
                        wintypes.HANDLE,
                        wintypes.ULONG,
                        ctypes.c_void_p,
                        wintypes.ULONG,
                        ctypes.POINTER(wintypes.ULONG),
                    ]
                    query.restype = wintypes.LONG
                    split = ctypes.WinDLL("shell32").CommandLineToArgvW
                    split.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
                    split.restype = ctypes.POINTER(wintypes.LPWSTR)
                    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
                    _kernel32.LocalFree.restype = ctypes.c_void_p
                    _CMDLINE_API["api"] = (query, split)
                except (OSError, AttributeError):
                    _CMDLINE_API["api"] = None
            return _CMDLINE_API["api"]

    def read_process_cmdline(pid: int) -> Optional[List[str]]:
        """Command line of `pid` split like the OS does (CommandLineToArgvW), or None when it cannot be read."""
        if pid <= 0:
            return None
        api = _cmdline_api()
        if api is None:
            return None
        query, split = api
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            needed = wintypes.ULONG(0)
            query(handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(needed))
            size = int(needed.value)
            if size < ctypes.sizeof(_UNICODE_STRING) or size > _MAX_CMDLINE_BUFFER_BYTES:
                return None
            buf = ctypes.create_string_buffer(size)
            if query(handle, _PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(needed)) != 0:
                return None
            info = _UNICODE_STRING.from_buffer(buf)
            if not info.Buffer or info.Length == 0:
                return None
            text = ctypes.wstring_at(info.Buffer, info.Length // 2)
        except (OSError, ValueError):
            return None
        finally:
            _kernel32.CloseHandle(handle)
        argc = ctypes.c_int(0)
        argv = split(text, ctypes.byref(argc))
        if not argv:
            return None
        try:
            return [argv[idx] or "" for idx in range(argc.value)]
        finally:
            _kernel32.LocalFree(argv)

    def _enumerate_windows_topology() -> List[Tuple[int, int]]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
            return []
        rows: List[Tuple[int, int]] = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return rows

    def inspect_os_processes(target_roots: Optional[Iterable[int]] = None) -> List[RawProcessSample]:
        """Inspect active Windows processes with both Working Set and Private Working Set."""
        topology = _enumerate_windows_topology()
        if not topology:
            return []
        if target_roots is not None:
            roots = {int(pid) for pid in target_roots if pid and pid > 0}
            children_map: Dict[int, List[int]] = {}
            ppid_map: Dict[int, int] = {}
            for pid, ppid in topology:
                ppid_map[pid] = ppid
                children_map.setdefault(ppid, []).append(pid)
            subtree_pids: Set[int] = set()
            queue = [pid for pid in roots if pid in ppid_map]
            while queue:
                cur = queue.pop(0)
                if cur in subtree_pids:
                    continue
                subtree_pids.add(cur)
                for child_pid in children_map.get(cur, ()):
                    if child_pid not in subtree_pids:
                        queue.append(child_pid)
            selected = [(pid, ppid_map[pid]) for pid in sorted(subtree_pids)]
        else:
            selected = topology

        pdh_data = _get_pdh_helper().sample()
        private_by_pid: Dict[int, int] = pdh_data.get("private_by_pid", {})

        samples: List[RawProcessSample] = []
        for pid, ppid in selected:
            create_ns, ws_bytes, cpu_sec, name = _query_win32_process_metrics(pid)
            priv_bytes = private_by_pid.get(pid)
            if ws_bytes is not None and priv_bytes is not None:
                priv_capped = min(ws_bytes, max(0, priv_bytes))
                shared_bytes: Optional[int] = max(0, ws_bytes - priv_capped)
            else:
                priv_capped = priv_bytes
                shared_bytes = None
            samples.append(
                RawProcessSample(
                    pid=pid,
                    ppid=ppid,
                    name=name,
                    create_time_ns=create_ns,
                    ram_bytes=ws_bytes,
                    cpu_time_seconds=cpu_sec,
                    private_ram_bytes=priv_capped,
                    shared_ram_bytes=shared_bytes,
                )
            )
        return samples

    def _read_windows_system_times() -> Optional[Tuple[int, int]]:
        idle_ft, kernel_ft, user_ft = _FILETIME(), _FILETIME(), _FILETIME()
        if not _kernel32.GetSystemTimes(ctypes.byref(idle_ft), ctypes.byref(kernel_ft), ctypes.byref(user_ft)):
            return None
        idle = _filetime_to_ticks(idle_ft)
        total = _filetime_to_ticks(kernel_ft) + _filetime_to_ticks(user_ft)
        return idle, total

    def _read_windows_storage_volumes() -> List[StorageVolumeMetrics]:
        volumes: List[StorageVolumeMetrics] = []
        mask = int(_kernel32.GetLogicalDrives())
        for idx, letter in enumerate(string.ascii_uppercase):
            if not (mask & (1 << idx)):
                continue
            root = f"{letter}:\\"
            drive_type = int(_kernel32.GetDriveTypeW(root))
            # 3 = DRIVE_FIXED; avoid stalling on unmounted CD/network drives
            if drive_type != 3:
                continue
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            if usage.total <= 0:
                continue
            used = max(0, usage.total - usage.free)
            pct = round((used / usage.total) * 100.0, 2)
            volumes.append(
                StorageVolumeMetrics(
                    name=f"{letter}:",
                    mount_point=root,
                    total_bytes=usage.total,
                    used_bytes=used,
                    available_bytes=usage.free,
                    usage_percent=pct,
                )
            )
        return volumes

    class _SystemResourceSampler:
        """Stateful host telemetry sampler for Windows."""

        def __init__(self) -> None:
            self._prev_sys_times: Optional[Tuple[int, int]] = None
            self._cached_storage: Optional[Tuple[float, List[StorageVolumeMetrics]]] = None

        def sample(self, elapsed_seconds: Optional[float], cpu_count: int) -> SystemResourcesMetrics:
            # 1) Global CPU %
            cpu_pct: Optional[float] = None
            cpu_prov = MetricProvenance.UNKNOWN
            cur_times = _read_windows_system_times()
            if cur_times is not None and self._prev_sys_times is not None:
                d_idle = cur_times[0] - self._prev_sys_times[0]
                d_total = cur_times[1] - self._prev_sys_times[1]
                if d_total > 0 and d_idle >= 0 and d_idle <= d_total:
                    busy = max(0.0, float(d_total - d_idle))
                    cpu_pct = round(min(100.0, (busy / float(d_total)) * 100.0), 2)
                    cpu_prov = MetricProvenance.MEASURED
            if cur_times is not None:
                self._prev_sys_times = cur_times

            # 2) Global RAM
            ram_total = ram_used = ram_avail = 0
            ram_pct: Optional[float] = None
            ram_prov = MetricProvenance.UNKNOWN
            ms = _MEMORYSTATUSEX()
            ms.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if _kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)) and ms.ullTotalPhys > 0:
                ram_total = int(ms.ullTotalPhys)
                ram_avail = int(ms.ullAvailPhys)
                ram_used = max(0, ram_total - ram_avail)
                ram_pct = round((ram_used / float(ram_total)) * 100.0, 2)
                ram_prov = MetricProvenance.MEASURED

            # 3) Storage capacity (cached 10 s)
            now_m = time.monotonic()
            if self._cached_storage is None or (now_m - self._cached_storage[0]) >= 10.0:
                self._cached_storage = (now_m, _read_windows_storage_volumes())
            volumes = self._cached_storage[1]
            st_total = sum(v.total_bytes for v in volumes)
            st_avail = sum(v.available_bytes for v in volumes)
            st_used = sum(v.used_bytes for v in volumes)
            st_pct = round((st_used / float(st_total)) * 100.0, 2) if st_total > 0 else None
            st_prov = MetricProvenance.MEASURED if volumes else MetricProvenance.UNKNOWN

            # 4) PDH Disk activity + GPU (do not force double sample inside same cycle; rely on persistent query)
            pdh = _get_pdh_helper().sample()
            disk_act = pdh.get("disk_activity_percent")
            disk_r = pdh.get("disk_read_bytes_per_sec")
            disk_w = pdh.get("disk_write_bytes_per_sec")
            gpu_pct = pdh.get("gpu_usage_percent")

            return SystemResourcesMetrics(
                logical_cpu_count=cpu_count,
                cpu_usage_percent=cpu_pct,
                cpu_provenance=cpu_prov,
                ram_total_bytes=ram_total,
                ram_used_bytes=ram_used,
                ram_available_bytes=ram_avail,
                ram_usage_percent=ram_pct,
                ram_provenance=ram_prov,
                storage_total_bytes=st_total,
                storage_used_bytes=st_used,
                storage_available_bytes=st_avail,
                storage_usage_percent=st_pct,
                storage_provenance=st_prov,
                storage_volumes=volumes,
                disk_activity_percent=disk_act,
                disk_activity_provenance=MetricProvenance.MEASURED if disk_act is not None else MetricProvenance.UNKNOWN,
                disk_read_bytes_per_sec=disk_r,
                disk_write_bytes_per_sec=disk_w,
                disk_io_provenance=MetricProvenance.MEASURED if (disk_r is not None and disk_w is not None) else MetricProvenance.UNKNOWN,
                network_rx_bytes_per_sec=None,
                network_tx_bytes_per_sec=None,
                network_total_bytes_per_sec=None,
                network_provenance=MetricProvenance.UNKNOWN,
                gpu_usage_percent=gpu_pct,
                gpu_provenance=MetricProvenance.MEASURED if gpu_pct is not None else MetricProvenance.UNKNOWN,
            )

else:
    import subprocess

    def _read_proc_btime_ns() -> int:
        try:
            with open("/proc/stat", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return int(line.split()[1]) * 1_000_000_000
        except OSError:
            pass
        return 0

    def _parse_ps_cpu_seconds(text: str) -> Optional[float]:
        """Parse `ps` TIME field (`[[dd-]hh:]mm:ss(.ff)` or `mm:ss.ff`)."""
        s = text.strip()
        if not s:
            return None
        days = 0
        if "-" in s:
            d_part, s = s.split("-", 1)
            days = int(d_part)
        parts = s.split(":")
        try:
            if len(parts) == 2:
                minutes, seconds = int(parts[0]), float(parts[1])
                hours = 0
            elif len(parts) == 3:
                hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
            else:
                return None
            return days * 86400.0 + hours * 3600.0 + minutes * 60.0 + seconds
        except ValueError:
            return None

    def _parse_ps_lstart_ns(tokens: Sequence[str]) -> Optional[int]:
        """Parse `ps` lstart 5-token C timestamp (`Mon Nov 23 19:41:55 2026`)."""
        if len(tokens) != 5:
            return None
        try:
            st = time.strptime(" ".join(tokens), "%a %b %d %H:%M:%S %Y")
            return int(time.mktime(st)) * 1_000_000_000
        except (ValueError, OverflowError):
            return None

    def _inspect_ps_processes() -> List[RawProcessSample]:
        """Fallback POSIX process inspection via `ps` for platforms without `/proc` (e.g., macOS)."""
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        try:
            proc = subprocess.Popen(
                ["ps", "-axo", "pid=,ppid=,rss=,time=,lstart=,comm="],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            ps_pid = proc.pid
            try:
                stdout_text, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                return []
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []

        samples: List[RawProcessSample] = []
        for raw_line in stdout_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 9)
            if len(parts) < 10:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
                rss_kb = int(parts[2])
            except ValueError:
                continue
            if pid == ps_pid:
                continue  # Exclude ephemeral helper `ps` spawned by the collector itself
            cpu_sec = _parse_ps_cpu_seconds(parts[3])
            create_ns = _parse_ps_lstart_ns(parts[4:9])
            comm_full = parts[9].strip()
            sep = comm_full.rfind("/")
            name = comm_full[sep + 1 :] if sep >= 0 else comm_full
            samples.append(
                RawProcessSample(
                    pid=pid,
                    ppid=ppid,
                    name=name,
                    create_time_ns=create_ns,
                    ram_bytes=max(0, rss_kb) * 1024,
                    cpu_time_seconds=cpu_sec,
                    private_ram_bytes=None,
                    shared_ram_bytes=None,
                )
            )
        return samples

    def _read_linux_private_shared_bytes(pid: int) -> Tuple[Optional[int], Optional[int]]:
        """Read Linux Private vs Shared resident memory from `/proc/<pid>/smaps_rollup` if readable."""
        try:
            with open(f"/proc/{pid}/smaps_rollup", "r", encoding="utf-8", errors="replace") as sfh:
                priv_kb = 0
                shared_kb = 0
                found_priv = False
                found_shared = False
                for line in sfh:
                    if line.startswith(("Private_Clean:", "Private_Dirty:")):
                        priv_kb += int(line.split()[1])
                        found_priv = True
                    elif line.startswith(("Shared_Clean:", "Shared_Dirty:")):
                        shared_kb += int(line.split()[1])
                        found_shared = True
                return (
                    priv_kb * 1024 if found_priv else None,
                    shared_kb * 1024 if found_shared else None,
                )
        except (OSError, ValueError, IndexError):
            return None, None

    def _inspect_proc_processes() -> List[RawProcessSample]:
        try:
            clk_tck = os.sysconf("SC_CLK_TCK")
            page_size = os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError):
            clk_tck, page_size = 100, 4096

        btime_ns = _read_proc_btime_ns()
        all_samples: List[RawProcessSample] = []
        try:
            proc_entries = [name for name in os.listdir("/proc") if name.isdigit()]
        except OSError:
            return []

        for pid_str in proc_entries:
            pid = int(pid_str)
            try:
                with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
                    stat_line = fh.read().strip()
                lparen = stat_line.find("(")
                rparen = stat_line.rfind(")")
                if lparen < 0 or rparen <= lparen:
                    continue
                comm = stat_line[lparen + 1 : rparen]
                rest = stat_line[rparen + 2 :].split()
                if len(rest) < 20:
                    continue
                ppid = int(rest[1])
                utime = int(rest[11])
                stime = int(rest[12])
                starttime = int(rest[19])
                cpu_time_seconds = (utime + stime) / float(clk_tck)
                create_time_ns = btime_ns + int((starttime * 1_000_000_000) / clk_tck)

                ram_bytes: Optional[int] = None
                shared_bytes: Optional[int] = None
                private_bytes: Optional[int] = None
                try:
                    with open(f"/proc/{pid}/statm", "r", encoding="utf-8") as mfh:
                        parts = mfh.read().split()
                        if len(parts) >= 2:
                            ram_bytes = int(parts[1]) * page_size
                        if len(parts) >= 3 and ram_bytes is not None:
                            shared_statm = int(parts[2]) * page_size
                            shared_bytes = min(ram_bytes, max(0, shared_statm))
                            private_bytes = max(0, ram_bytes - shared_bytes)
                except OSError:
                    pass

                # Prefer precise smaps_rollup when available (permits same-UID reads cleanly)
                s_priv, s_shared = _read_linux_private_shared_bytes(pid)
                if s_priv is not None:
                    private_bytes = s_priv
                if s_shared is not None:
                    shared_bytes = s_shared

                all_samples.append(
                    RawProcessSample(
                        pid=pid,
                        ppid=ppid,
                        name=comm,
                        create_time_ns=create_time_ns,
                        ram_bytes=ram_bytes,
                        cpu_time_seconds=cpu_time_seconds,
                        private_ram_bytes=private_bytes,
                        shared_ram_bytes=shared_bytes,
                    )
                )
            except (OSError, ValueError):
                continue
        return all_samples

    _MAX_CMDLINE_READ_BYTES = 128 * 1024

    def read_process_cmdline(pid: int) -> Optional[List[str]]:
        """Command line of `pid` (`/proc/<pid>/cmdline`, or `ps` elsewhere), or None when it cannot be read."""
        if pid <= 0:
            return None
        if os.path.isdir("/proc"):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    raw = fh.read(_MAX_CMDLINE_READ_BYTES)
            except OSError:
                return None
            parts = raw.split(b"\0")
            if parts and parts[-1] == b"":
                parts.pop()
            return [p.decode("utf-8", errors="replace") for p in parts] or None
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        try:
            result = subprocess.run(
                ["ps", "-ww", "-o", "args=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.split() or None

    def inspect_os_processes(target_roots: Optional[Iterable[int]] = None) -> List[RawProcessSample]:
        """Inspect POSIX processes via `/proc` (Linux) or `ps` fallback (macOS/BSD)."""
        all_samples = _inspect_proc_processes() if os.path.isdir("/proc") else _inspect_ps_processes()

        if target_roots is None:
            return all_samples

        roots = {int(pid) for pid in target_roots if pid and pid > 0}
        by_pid = {s.pid: s for s in all_samples}
        children_map: Dict[int, List[int]] = {}
        for s in all_samples:
            if s.ppid is not None:
                children_map.setdefault(s.ppid, []).append(s.pid)

        subtree: Set[int] = set()
        queue = [pid for pid in roots if pid in by_pid]
        while queue:
            cur = queue.pop(0)
            if cur in subtree:
                continue
            subtree.add(cur)
            for child_pid in children_map.get(cur, ()):
                if child_pid not in subtree:
                    queue.append(child_pid)
        return [by_pid[pid] for pid in sorted(subtree)]

    class _SystemResourceSampler:
        """Stateful host telemetry sampler for Linux (`/proc`) and POSIX (`shutil.disk_usage`)."""

        def __init__(self) -> None:
            self._prev_cpu: Optional[Tuple[int, int]] = None
            self._prev_net: Optional[Tuple[int, int]] = None
            self._cached_darwin_ram_total: Optional[int] = None

        def sample(self, elapsed_seconds: Optional[float], cpu_count: int) -> SystemResourcesMetrics:
            cpu_pct: Optional[float] = None
            cpu_prov = MetricProvenance.UNKNOWN
            if os.path.isfile("/proc/stat"):
                try:
                    with open("/proc/stat", "r", encoding="utf-8") as fh:
                        first = fh.readline().split()
                    if first and first[0] == "cpu" and len(first) >= 5:
                        vals = [int(x) for x in first[1:]]
                        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                        total = sum(vals)
                        if self._prev_cpu is not None:
                            dt_idle = idle - self._prev_cpu[0]
                            dt_total = total - self._prev_cpu[1]
                            if dt_total > 0 and 0 <= dt_idle <= dt_total:
                                cpu_pct = round(min(100.0, ((dt_total - dt_idle) / float(dt_total)) * 100.0), 2)
                                cpu_prov = MetricProvenance.MEASURED
                        self._prev_cpu = (idle, total)
                except (OSError, ValueError):
                    pass

            ram_total = ram_used = ram_avail = 0
            ram_pct: Optional[float] = None
            ram_prov = MetricProvenance.UNKNOWN
            if os.path.isfile("/proc/meminfo"):
                try:
                    info: Dict[str, int] = {}
                    with open("/proc/meminfo", "r", encoding="utf-8") as fh:
                        for line in fh:
                            parts = line.split()
                            if len(parts) >= 2:
                                info[parts[0].rstrip(":")] = int(parts[1]) * 1024
                    if info.get("MemTotal", 0) > 0:
                        ram_total = info["MemTotal"]
                        ram_avail = info.get("MemAvailable", info.get("MemFree", 0))
                        ram_used = max(0, ram_total - ram_avail)
                        ram_pct = round((ram_used / float(ram_total)) * 100.0, 2)
                        ram_prov = MetricProvenance.MEASURED
                except (OSError, ValueError):
                    pass
            elif sys.platform == "darwin":
                if self._cached_darwin_ram_total is None:
                    total_mem: Optional[int] = None
                    try:
                        libc = ctypes.CDLL("libc.dylib")
                        val = ctypes.c_uint64(0)
                        sz = ctypes.c_size_t(ctypes.sizeof(val))
                        rc = libc.sysctlbyname(
                            b"hw.memsize", ctypes.byref(val), ctypes.byref(sz), None, 0
                        )
                        if rc == 0 and val.value > 0:
                            total_mem = int(val.value)
                    except (OSError, AttributeError, ValueError):
                        total_mem = None
                    if total_mem is None:
                        try:
                            out = subprocess.run(
                                ["sysctl", "-n", "hw.memsize"],
                                capture_output=True,
                                text=True,
                                timeout=2,
                                check=False,
                            )
                            if out.returncode == 0 and out.stdout.strip().isdigit():
                                parsed = int(out.stdout.strip())
                                if parsed > 0:
                                    total_mem = parsed
                        except (OSError, subprocess.SubprocessError, ValueError):
                            total_mem = None
                    if total_mem is not None and total_mem > 0:
                        self._cached_darwin_ram_total = total_mem
                ram_total = self._cached_darwin_ram_total or 0
                if ram_total > 0:
                    try:
                        vm = subprocess.run(
                            ["vm_stat"],
                            capture_output=True,
                            text=True,
                            timeout=2,
                            check=False,
                        )
                        if vm.returncode == 0:
                            page_size = 4096
                            free_pages = inactive_pages = speculative_pages = 0
                            m_ps = re.search(r"page size of (\d+) bytes", vm.stdout)
                            if m_ps:
                                page_size = max(1, int(m_ps.group(1)))
                            for line in vm.stdout.splitlines():
                                if ":" not in line:
                                    continue
                                k, v = line.split(":", 1)
                                digits = "".join(ch for ch in v if ch.isdigit())
                                if not digits:
                                    continue
                                count = int(digits)
                                kl = k.strip().lower()
                                if kl == "pages free":
                                    free_pages = count
                                elif kl == "pages inactive":
                                    inactive_pages = count
                                elif kl == "pages speculative":
                                    speculative_pages = count
                            ram_avail = min(ram_total, (free_pages + inactive_pages + speculative_pages) * page_size)
                            ram_used = max(0, ram_total - ram_avail)
                            ram_pct = round((ram_used / float(ram_total)) * 100.0, 2)
                            ram_prov = MetricProvenance.MEASURED
                    except (OSError, subprocess.SubprocessError, ValueError):
                        pass

            volumes: List[StorageVolumeMetrics] = []
            try:
                du = shutil.disk_usage("/")
                if du.total > 0:
                    used = max(0, du.total - du.free)
                    volumes.append(
                        StorageVolumeMetrics(
                            name="/",
                            mount_point="/",
                            total_bytes=du.total,
                            used_bytes=used,
                            available_bytes=du.free,
                            usage_percent=round((used / float(du.total)) * 100.0, 2),
                        )
                    )
            except OSError:
                pass
            st_total = sum(v.total_bytes for v in volumes)
            st_avail = sum(v.available_bytes for v in volumes)
            st_used = sum(v.used_bytes for v in volumes)
            st_pct = round((st_used / float(st_total)) * 100.0, 2) if st_total > 0 else None
            st_prov = MetricProvenance.MEASURED if volumes else MetricProvenance.UNKNOWN

            rx_bps = tx_bps = tot_bps = None
            net_prov = MetricProvenance.UNKNOWN
            if os.path.isfile("/proc/net/dev"):
                try:
                    rx_tot = tx_tot = 0
                    with open("/proc/net/dev", "r", encoding="utf-8") as fh:
                        for line in fh.readlines()[2:]:
                            if ":" not in line:
                                continue
                            iface, rest = line.split(":", 1)
                            if iface.strip() == "lo":
                                continue
                            cols = rest.split()
                            if len(cols) >= 9:
                                rx_tot += int(cols[0])
                                tx_tot += int(cols[8])
                    if self._prev_net is not None and elapsed_seconds and elapsed_seconds > 0:
                        drx = rx_tot - self._prev_net[0]
                        dtx = tx_tot - self._prev_net[1]
                        if drx >= 0 and dtx >= 0:
                            rx_bps = int(round(drx / elapsed_seconds))
                            tx_bps = int(round(dtx / elapsed_seconds))
                            tot_bps = rx_bps + tx_bps
                            net_prov = MetricProvenance.MEASURED
                    self._prev_net = (rx_tot, tx_tot)
                except (OSError, ValueError):
                    pass

            gpu_pct: Optional[float] = None
            gpu_prov = MetricProvenance.UNKNOWN
            for path in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
                try:
                    val = float(open(path, "r", encoding="utf-8").read().strip())
                    if 0.0 <= val <= 100.0:
                        gpu_pct = round(max(gpu_pct or 0.0, val), 2)
                        gpu_prov = MetricProvenance.MEASURED
                except (OSError, ValueError):
                    continue

            return SystemResourcesMetrics(
                logical_cpu_count=cpu_count,
                cpu_usage_percent=cpu_pct,
                cpu_provenance=cpu_prov,
                ram_total_bytes=ram_total,
                ram_used_bytes=ram_used,
                ram_available_bytes=ram_avail,
                ram_usage_percent=ram_pct,
                ram_provenance=ram_prov,
                storage_total_bytes=st_total,
                storage_used_bytes=st_used,
                storage_available_bytes=st_avail,
                storage_usage_percent=st_pct,
                storage_provenance=st_prov,
                storage_volumes=volumes,
                gpu_usage_percent=gpu_pct,
                gpu_provenance=gpu_prov,
                network_rx_bytes_per_sec=rx_bps,
                network_tx_bytes_per_sec=tx_bps,
                network_total_bytes_per_sec=tot_bps,
                network_provenance=net_prov,
            )


def validate_sample_interval_ms(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("min_sample_interval_ms must be an integer")
    if value < MIN_ALLOWED_SAMPLE_INTERVAL_MS or value > MAX_ALLOWED_SAMPLE_INTERVAL_MS:
        raise ValueError(
            f"min_sample_interval_ms must be between {MIN_ALLOWED_SAMPLE_INTERVAL_MS} and {MAX_ALLOWED_SAMPLE_INTERVAL_MS}"
        )
    return value


class ProcessTreeCollector:
    """Stateful collector maintaining temporal CPU baseline, memory breakdown, and process identities."""

    def __init__(
        self,
        *,
        core_pid: Optional[int] = None,
        min_sample_interval_ms: int = DEFAULT_MIN_SAMPLE_INTERVAL_MS,
        process_lister: Optional[Callable[[Optional[Iterable[int]]], List[RawProcessSample]]] = None,
        clock_ns: Optional[Callable[[], int]] = None,
        cpu_count_fn: Optional[Callable[[], int]] = None,
        system_sampler: Optional[Callable[[Optional[float], int], Optional[SystemResourcesMetrics]]] = None,
        data_origin: DataOrigin = DataOrigin.LIVE_OS,
        cmdline_reader: Optional[Callable[[int], Optional[Sequence[str]]]] = None,
    ) -> None:
        if not isinstance(data_origin, DataOrigin):
            raise ValueError(f"Invalid data_origin: {data_origin!r}")
        self._core_pid = os.getpid() if core_pid is None else core_pid
        self._min_sample_interval_ms = validate_sample_interval_ms(min_sample_interval_ms)
        self._process_lister = process_lister or inspect_os_processes
        # An injected lister describes processes that are not the live ones: never read real command lines for them.
        self._cmdline_reader = cmdline_reader or (read_process_cmdline if process_lister is None else None)
        self._cmdline_kind_by_identity: Dict[Tuple[int, int], Optional[str]] = {}
        self._clock_ns = clock_ns or time.monotonic_ns
        self._cpu_count_fn = cpu_count_fn or (lambda: os.cpu_count() or 1)
        self._default_system_sampler = _SystemResourceSampler() if system_sampler is None else None
        self._system_sampler = system_sampler or self._default_system_sampler.sample
        self._data_origin = data_origin

        self._lock = threading.Lock()
        self._last_sample_at_ns: Optional[int] = None
        self._last_system_sample_at_ns: Optional[int] = None
        self._previous_cpu_by_identity: Dict[Tuple[int, Optional[int]], float] = {}
        self._last_snapshot: Optional[ProcessTreeMetricsSnapshot] = None

    @property
    def min_sample_interval_ms(self) -> int:
        with self._lock:
            return self._min_sample_interval_ms

    def prime(
        self,
        *,
        host_pid: Optional[int] = None,
        core_pid: Optional[int] = None,
        terminal_pids: Optional[Dict[int, Optional[int]]] = None,
        agent_pids: Optional[Dict[int, Tuple[Optional[int], Optional[str], Optional[str]]]] = None,
    ) -> ProcessTreeMetricsSnapshot:
        """Establish initial baseline (returns `unknown` CPU until temporal delta exists)."""
        return self.collect(
            host_pid=host_pid,
            core_pid=core_pid,
            terminal_pids=terminal_pids,
            agent_pids=agent_pids,
        )

    def _agent_kind(
        self,
        sample: RawProcessSample,
        next_cmdline_kinds: Dict[Tuple[int, int], Optional[str]],
    ) -> Optional[str]:
        """Name first; a wrapper's command line is read once per `(pid, create_time_ns)` and only its kind is kept."""
        kind = detect_agent_kind(sample.name)
        if kind is not None or sample.create_time_ns is None or not agent_needs_cmdline(sample.name):
            return kind
        identity = (sample.pid, sample.create_time_ns)
        if identity in self._cmdline_kind_by_identity:
            kind = self._cmdline_kind_by_identity[identity]
        elif self._cmdline_reader is not None:
            try:
                cmdline = self._cmdline_reader(sample.pid)
            except Exception:
                cmdline = None
            kind = detect_agent_kind(sample.name, cmdline) if cmdline else None
        next_cmdline_kinds[identity] = kind
        return kind

    def _classify_role(
        self,
        sample: RawProcessSample,
        *,
        host_pid: Optional[int],
        core_pid: Optional[int],
        terminal_pids: Dict[int, Optional[int]],
        agent_pids: Dict[int, Tuple[Optional[int], Optional[str], Optional[str]]],
        next_cmdline_kinds: Dict[Tuple[int, int], Optional[str]],
    ) -> Tuple[str, Optional[str], Optional[str]]:
        if host_pid is not None and sample.pid == host_pid:
            return "host", None, None
        if core_pid is not None and sample.pid == core_pid:
            return "core", None, None
        if "webview" in sample.name.lower():
            return "webview", None, None

        detected = self._agent_kind(sample, next_cmdline_kinds)
        if sample.pid in agent_pids:
            expected_ct, kind_hint, sess_id = agent_pids[sample.pid]
            if expected_ct is None or sample.create_time_ns is None or expected_ct == sample.create_time_ns:
                return "agent", kind_hint or detected, sess_id

        if detected is not None:
            return "agent", detected, None

        if sample.pid in terminal_pids:
            expected_ct = terminal_pids[sample.pid]
            if expected_ct is None or sample.create_time_ns is None or expected_ct == sample.create_time_ns:
                return "terminal", None, None
        return "child", None, None

    def _collect_subtree(
        self,
        all_samples: Sequence[RawProcessSample],
        roots: Sequence[int],
    ) -> List[RawProcessSample]:
        by_pid: Dict[int, RawProcessSample] = {}
        children_by_ppid: Dict[int, List[int]] = {}
        for sample in all_samples:
            if sample.pid in by_pid:
                continue  # Deduplicate PID rows if a custom/OS snapshot includes duplicates
            by_pid[sample.pid] = sample
            if sample.ppid is not None and sample.ppid != sample.pid:
                children_by_ppid.setdefault(sample.ppid, []).append(sample.pid)

        visited: Set[int] = set()
        queue: List[int] = [pid for pid in roots if pid in by_pid]
        ordered: List[RawProcessSample] = []

        while queue:
            pid = queue.pop(0)
            if pid in visited:
                continue
            visited.add(pid)
            ordered.append(by_pid[pid])
            for child_pid in sorted(children_by_ppid.get(pid, ())):
                if child_pid not in visited:
                    queue.append(child_pid)

        return ordered

    def _aggregate_cpu_provenance(
        self,
        cpu_values: Sequence[Optional[float]],
        provenances: Sequence[MetricProvenance],
    ) -> Tuple[Optional[float], MetricProvenance]:
        measured_values = [
            val for val, prov in zip(cpu_values, provenances)
            if val is not None and prov in (MetricProvenance.MEASURED, MetricProvenance.ESTIMATED)
        ]
        if not measured_values:
            return None, MetricProvenance.UNKNOWN

        total = round(min(100.0, max(0.0, sum(measured_values))), 2)
        if len(measured_values) < len(provenances) or MetricProvenance.ESTIMATED in provenances:
            return total, MetricProvenance.ESTIMATED
        return total, MetricProvenance.MEASURED

    @staticmethod
    def _aggregate_optional_ram(
        values: Sequence[Optional[int]],
        node_count: int,
    ) -> Tuple[Optional[int], MetricProvenance]:
        known = [v for v in values if v is not None]
        if node_count == 0 or not known:
            return None, MetricProvenance.UNKNOWN
        total = sum(known)
        if len(known) == node_count:
            return total, MetricProvenance.MEASURED
        return total, MetricProvenance.ESTIMATED

    def collect(
        self,
        *,
        host_pid: Optional[int] = None,
        core_pid: Optional[int] = None,
        terminal_pids: Optional[Dict[int, Optional[int]]] = None,
        agent_pids: Optional[Dict[int, Tuple[Optional[int], Optional[str], Optional[str]]]] = None,
        min_sample_interval_ms: Optional[int] = None,
        include_system: bool = True,
    ) -> ProcessTreeMetricsSnapshot:
        with self._lock:
            interval_ms = (
                validate_sample_interval_ms(min_sample_interval_ms)
                if min_sample_interval_ms is not None
                else self._min_sample_interval_ms
            )
            now_ns = self._clock_ns()
            elapsed_ns = None if self._last_sample_at_ns is None else max(0, now_ns - self._last_sample_at_ns)

            # Reuse last snapshot when sampled faster than the configured minimum interval.
            if (
                self._last_snapshot is not None
                and elapsed_ns is not None
                and elapsed_ns < interval_ms * 1_000_000
            ):
                return self._last_snapshot

            effective_core_pid = self._core_pid if core_pid is None else core_pid
            term_map = dict(terminal_pids or {})
            agent_map = dict(agent_pids or {})

            roots: List[int] = []
            for candidate in (host_pid, effective_core_pid, *sorted(term_map.keys()), *sorted(agent_map.keys())):
                if candidate is not None and candidate > 0 and candidate not in roots:
                    roots.append(candidate)

            raw_samples = self._process_lister(roots if roots else None)
            subtree = self._collect_subtree(raw_samples, roots) if roots else list(raw_samples)

            cpu_count = max(1, int(self._cpu_count_fn()))
            can_compute_delta = (
                self._last_sample_at_ns is not None
                and elapsed_ns is not None
                and elapsed_ns > 0
                and elapsed_ns >= interval_ms * 1_000_000
            )
            elapsed_seconds = (elapsed_ns / 1_000_000_000.0) if (can_compute_delta and elapsed_ns) else None

            nodes: List[ProcessNodeMetrics] = []
            next_cpu_by_identity: Dict[Tuple[int, Optional[int]], float] = {}
            next_cmdline_kinds: Dict[Tuple[int, int], Optional[str]] = {}

            for sample in subtree:
                identity = (sample.pid, sample.create_time_ns)
                if sample.cpu_time_seconds is not None and sample.cpu_time_seconds >= 0.0:
                    next_cpu_by_identity[identity] = sample.cpu_time_seconds

                ws_bytes = sample.ram_bytes
                ram_prov = MetricProvenance.MEASURED if ws_bytes is not None else MetricProvenance.UNKNOWN
                priv_bytes = sample.private_ram_bytes
                if ws_bytes is not None and priv_bytes is not None:
                    priv_bytes = min(ws_bytes, max(0, priv_bytes))
                priv_prov = MetricProvenance.MEASURED if priv_bytes is not None else MetricProvenance.UNKNOWN

                shared_bytes = sample.shared_ram_bytes
                if shared_bytes is None and ws_bytes is not None and priv_bytes is not None:
                    shared_bytes = max(0, ws_bytes - priv_bytes)

                cpu_pct: Optional[float] = None
                cpu_prov = MetricProvenance.UNKNOWN

                if (
                    elapsed_seconds is not None
                    and elapsed_seconds > 0.0
                    and sample.create_time_ns is not None
                    and sample.cpu_time_seconds is not None
                    and identity in self._previous_cpu_by_identity
                ):
                    delta_cpu = sample.cpu_time_seconds - self._previous_cpu_by_identity[identity]
                    if delta_cpu >= 0.0:
                        raw_pct = (delta_cpu / (elapsed_seconds * cpu_count)) * 100.0
                        cpu_pct = round(min(100.0, max(0.0, raw_pct)), 2)
                        cpu_prov = MetricProvenance.MEASURED

                role, agent_kind, session_id = self._classify_role(
                    sample,
                    host_pid=host_pid,
                    core_pid=effective_core_pid,
                    terminal_pids=term_map,
                    agent_pids=agent_map,
                    next_cmdline_kinds=next_cmdline_kinds,
                )
                nodes.append(
                    ProcessNodeMetrics(
                        pid=sample.pid,
                        ppid=sample.ppid,
                        create_time_ns=sample.create_time_ns,
                        role=role,
                        name=sample.name,
                        ram_bytes=ws_bytes,
                        ram_provenance=ram_prov,
                        private_ram_bytes=priv_bytes,
                        private_ram_provenance=priv_prov,
                        working_set_ram_bytes=ws_bytes,
                        shared_ram_bytes=shared_bytes,
                        cpu_usage_percent=cpu_pct,
                        cpu_provenance=cpu_prov,
                        agent_kind=agent_kind,
                        session_id=session_id,
                    )
                )

            categories: Dict[str, CategoryResourceMetrics] = {}
            for role_name in VALID_ROLES:
                role_nodes = [n for n in nodes if n.role == role_name]
                if not role_nodes:
                    categories[role_name] = CategoryResourceMetrics(
                        role=role_name,
                        process_count=0,
                        ram_bytes=0,
                        ram_provenance=MetricProvenance.UNKNOWN,
                        private_ram_bytes=None,
                        private_ram_provenance=MetricProvenance.UNKNOWN,
                        working_set_ram_bytes=0,
                        shared_ram_bytes=None,
                        cpu_usage_percent=None,
                        cpu_provenance=MetricProvenance.UNKNOWN,
                    )
                    continue

                known_ram = [n.ram_bytes for n in role_nodes if n.ram_bytes is not None]
                if not known_ram:
                    cat_ram = 0
                    cat_ram_prov = MetricProvenance.UNKNOWN
                elif len(known_ram) == len(role_nodes):
                    cat_ram = sum(known_ram)
                    cat_ram_prov = MetricProvenance.MEASURED
                else:
                    cat_ram = sum(known_ram)
                    cat_ram_prov = MetricProvenance.ESTIMATED

                cat_priv, cat_priv_prov = self._aggregate_optional_ram(
                    [n.private_ram_bytes for n in role_nodes], len(role_nodes)
                )
                cat_shared, _ = self._aggregate_optional_ram(
                    [n.shared_ram_bytes for n in role_nodes], len(role_nodes)
                )
                cat_cpu, cat_cpu_prov = self._aggregate_cpu_provenance(
                    [n.cpu_usage_percent for n in role_nodes],
                    [n.cpu_provenance for n in role_nodes],
                )
                categories[role_name] = CategoryResourceMetrics(
                    role=role_name,
                    process_count=len(role_nodes),
                    ram_bytes=cat_ram,
                    ram_provenance=cat_ram_prov,
                    private_ram_bytes=cat_priv,
                    private_ram_provenance=cat_priv_prov,
                    working_set_ram_bytes=cat_ram,
                    shared_ram_bytes=cat_shared,
                    cpu_usage_percent=cat_cpu,
                    cpu_provenance=cat_cpu_prov,
                )

            known_tree_ram = [n.ram_bytes for n in nodes if n.ram_bytes is not None]
            if not nodes or not known_tree_ram:
                total_ram = 0
                ram_provenance = MetricProvenance.UNKNOWN
            elif len(known_tree_ram) == len(nodes):
                total_ram = sum(known_tree_ram)
                ram_provenance = MetricProvenance.MEASURED
            else:
                total_ram = sum(known_tree_ram)
                ram_provenance = MetricProvenance.ESTIMATED

            total_priv_ram, priv_provenance = self._aggregate_optional_ram(
                [n.private_ram_bytes for n in nodes], len(nodes)
            )
            total_shared_ram, _ = self._aggregate_optional_ram(
                [n.shared_ram_bytes for n in nodes], len(nodes)
            )

            host_ram_bytes = 0
            host_priv_bytes: Optional[int] = None
            if host_pid is not None:
                for n in nodes:
                    if n.pid == host_pid:
                        if n.ram_bytes is not None:
                            host_ram_bytes = n.ram_bytes
                        host_priv_bytes = n.private_ram_bytes
                        break

            tree_cpu_pct, tree_cpu_prov = self._aggregate_cpu_provenance(
                [n.cpu_usage_percent for n in nodes],
                [n.cpu_provenance for n in nodes],
            )

            sys_metrics: Optional[SystemResourcesMetrics] = None
            if include_system:
                # Rates (network) are measured since the last reading that sampled the system, not since the last one.
                sys_elapsed_seconds = (
                    (now_ns - self._last_system_sample_at_ns) / 1_000_000_000.0
                    if can_compute_delta and self._last_system_sample_at_ns is not None
                    else None
                )
                sys_metrics = self._system_sampler(sys_elapsed_seconds, cpu_count)
            sample_interval = int(round(elapsed_ns / 1_000_000)) if can_compute_delta and elapsed_ns is not None else None
            snapshot = ProcessTreeMetricsSnapshot(
                root_pid=host_pid if host_pid is not None else effective_core_pid,
                host_pid=host_pid,
                core_pid=effective_core_pid,
                host_ram_bytes=host_ram_bytes,
                host_private_ram_bytes=host_priv_bytes,
                total_tree_ram_bytes=total_ram,
                ram_provenance=ram_provenance,
                total_private_ram_bytes=total_priv_ram,
                private_ram_provenance=priv_provenance,
                total_working_set_ram_bytes=total_ram,
                total_shared_ram_bytes=total_shared_ram,
                cpu_usage_percent=tree_cpu_pct,
                cpu_provenance=tree_cpu_prov,
                logical_cpu_count=cpu_count,
                sample_interval_ms=sample_interval,
                process_count=len(nodes),
                sampled_at_unix_ms=int(time.time() * 1000),
                categories=categories,
                nodes=nodes,
                data_origin=self._data_origin,
                system=sys_metrics,
            )

            self._previous_cpu_by_identity = next_cpu_by_identity
            self._cmdline_kind_by_identity = next_cmdline_kinds
            self._last_sample_at_ns = now_ns
            if include_system:
                self._last_system_sample_at_ns = now_ns
            self._last_snapshot = snapshot
            return snapshot


def measure_collector_overhead(
    collector: Optional[ProcessTreeCollector] = None,
    *,
    iterations: int = 3,
    interval_ms: int = 1000,
    host_pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Measure real collector CPU overhead over multiple intervals at rest."""
    col = collector or ProcessTreeCollector(min_sample_interval_ms=interval_ms)
    col.prime(host_pid=host_pid)
    sleep_s = max(interval_ms, 50) / 1000.0
    cpu_count = max(1, os.cpu_count() or 1)

    wall_start = time.monotonic()
    cpu_start = time.process_time()
    last_snapshot: Optional[ProcessTreeMetricsSnapshot] = None
    for _ in range(iterations):
        time.sleep(sleep_s)
        last_snapshot = col.collect(host_pid=host_pid, min_sample_interval_ms=interval_ms)
    wall_elapsed = max(1e-6, time.monotonic() - wall_start)
    cpu_elapsed = max(0.0, time.process_time() - cpu_start)

    overhead_single_core_pct = (cpu_elapsed / wall_elapsed) * 100.0
    overhead_system_pct = (cpu_elapsed / (wall_elapsed * cpu_count)) * 100.0
    return {
        "iterations": iterations,
        "interval_ms": interval_ms,
        "wall_elapsed_ms": round(wall_elapsed * 1000.0, 2),
        "collector_cpu_ms": round(cpu_elapsed * 1000.0, 3),
        "overhead_single_core_percent": round(overhead_single_core_pct, 4),
        "overhead_system_percent": round(overhead_system_pct, 4),
        "last_process_count": last_snapshot.process_count if last_snapshot else 0,
        "last_cpu_provenance": last_snapshot.cpu_provenance.value if last_snapshot else MetricProvenance.UNKNOWN.value,
    }
