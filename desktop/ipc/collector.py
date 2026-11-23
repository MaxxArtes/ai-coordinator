"""Real OS process-tree CPU and RAM collector for AI Coordinator Desktop (Phase 4D.4.1).

Collects host/core/webview/terminal/child resource usage with:
- Temporal CPU sampling guarded by a configurable minimum interval (`min_sample_interval_ms`, default 200 ms).
- Explicit provenance (`measured`, `estimated`, `unknown`); never emits 0% when CPU is unavailable or unprimed.
- Stable `(pid, create_time_ns)` process identity to protect against PID reuse, exits, and restarts.
- Deduplicated tree traversal and non-overlapping per-category breakdown.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from desktop.ipc.protocol import MetricProvenance

DEFAULT_MIN_SAMPLE_INTERVAL_MS = 200
MIN_ALLOWED_SAMPLE_INTERVAL_MS = 50
MAX_ALLOWED_SAMPLE_INTERVAL_MS = 60_000
VALID_ROLES = ("host", "core", "webview", "terminal", "child")


@dataclass(frozen=True)
class RawProcessSample:
    """Single OS observation for one process at one point in time."""

    pid: int
    ppid: Optional[int]
    name: str
    create_time_ns: Optional[int]
    ram_bytes: Optional[int]
    cpu_time_seconds: Optional[float]


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "create_time_ns": self.create_time_ns,
            "role": self.role,
            "name": self.name,
            "ram_bytes": self.ram_bytes,
            "ram_provenance": self.ram_provenance.value,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_provenance": self.cpu_provenance.value,
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "process_count": self.process_count,
            "ram_bytes": self.ram_bytes,
            "ram_provenance": self.ram_provenance.value,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_provenance": self.cpu_provenance.value,
        }


@dataclass(frozen=True)
class ProcessTreeMetricsSnapshot:
    """Typed telemetry snapshot for the desktop process tree."""

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

    def to_dict(self, *, include_nodes: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "root_pid": self.root_pid,
            "host_pid": self.host_pid,
            "core_pid": self.core_pid,
            "host_ram_bytes": self.host_ram_bytes,
            "total_tree_ram_bytes": self.total_tree_ram_bytes,
            "ram_provenance": self.ram_provenance.value,
            "cpu_usage_percent": self.cpu_usage_percent,
            "cpu_provenance": self.cpu_provenance.value,
            "sample_interval_ms": self.sample_interval_ms,
            "process_count": self.process_count,
            "sampled_at_unix_ms": self.sampled_at_unix_ms,
            "categories": {k: v.to_dict() for k, v in self.categories.items()},
        }
        if include_nodes:
            payload["nodes"] = [node.to_dict() for node in self.nodes]
        return payload


# ---------------------------------------------------------------------------
# Cross-platform OS Process Inspection (stdlib only; zero external packages)
# ---------------------------------------------------------------------------

if os.name == "nt":
    from ctypes import wintypes

    _TH32CS_SNAPPROCESS = 0x00000002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    # Windows FILETIME epoch (1601-01-01) -> Unix epoch (1970-01-01) in 100-ns ticks
    _WINDOWS_TO_UNIX_EPOCH_TICKS = 116_444_736_000_000_000

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
        """Inspect active Windows processes; if roots are provided, query detailed metrics only for the subtree."""
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

        samples: List[RawProcessSample] = []
        for pid, ppid in selected:
            create_ns, ram_bytes, cpu_sec, name = _query_win32_process_metrics(pid)
            samples.append(
                RawProcessSample(
                    pid=pid,
                    ppid=ppid,
                    name=name,
                    create_time_ns=create_ns,
                    ram_bytes=ram_bytes,
                    cpu_time_seconds=cpu_sec,
                )
            )
        return samples

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
            res = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,rss=,time=,lstart=,comm="],
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if res.returncode != 0:
            return []

        samples: List[RawProcessSample] = []
        for raw_line in res.stdout.splitlines():
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
                )
            )
        return samples

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
                try:
                    with open(f"/proc/{pid}/statm", "r", encoding="utf-8") as mfh:
                        parts = mfh.read().split()
                        if len(parts) >= 2:
                            ram_bytes = int(parts[1]) * page_size
                except OSError:
                    pass

                all_samples.append(
                    RawProcessSample(
                        pid=pid,
                        ppid=ppid,
                        name=comm,
                        create_time_ns=create_time_ns,
                        ram_bytes=ram_bytes,
                        cpu_time_seconds=cpu_time_seconds,
                    )
                )
            except (OSError, ValueError):
                continue
        return all_samples

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


def validate_sample_interval_ms(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("min_sample_interval_ms must be an integer")
    if value < MIN_ALLOWED_SAMPLE_INTERVAL_MS or value > MAX_ALLOWED_SAMPLE_INTERVAL_MS:
        raise ValueError(
            f"min_sample_interval_ms must be between {MIN_ALLOWED_SAMPLE_INTERVAL_MS} and {MAX_ALLOWED_SAMPLE_INTERVAL_MS}"
        )
    return value


class ProcessTreeCollector:
    """Stateful collector maintaining temporal CPU baseline and process identities."""

    def __init__(
        self,
        *,
        core_pid: Optional[int] = None,
        min_sample_interval_ms: int = DEFAULT_MIN_SAMPLE_INTERVAL_MS,
        process_lister: Optional[Callable[[Optional[Iterable[int]]], List[RawProcessSample]]] = None,
        clock_ns: Optional[Callable[[], int]] = None,
        cpu_count_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        self._core_pid = os.getpid() if core_pid is None else core_pid
        self._min_sample_interval_ms = validate_sample_interval_ms(min_sample_interval_ms)
        self._process_lister = process_lister or inspect_os_processes
        self._clock_ns = clock_ns or time.monotonic_ns
        self._cpu_count_fn = cpu_count_fn or (lambda: os.cpu_count() or 1)

        self._lock = threading.Lock()
        self._last_sample_at_ns: Optional[int] = None
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
    ) -> ProcessTreeMetricsSnapshot:
        """Establish initial baseline (returns `unknown` CPU until temporal delta exists)."""
        return self.collect(
            host_pid=host_pid,
            core_pid=core_pid,
            terminal_pids=terminal_pids,
        )

    def _classify_role(
        self,
        sample: RawProcessSample,
        *,
        host_pid: Optional[int],
        core_pid: Optional[int],
        terminal_pids: Dict[int, Optional[int]],
    ) -> str:
        if host_pid is not None and sample.pid == host_pid:
            return "host"
        if core_pid is not None and sample.pid == core_pid:
            return "core"
        if "webview" in sample.name.lower():
            return "webview"
        if sample.pid in terminal_pids:
            expected_ct = terminal_pids[sample.pid]
            if expected_ct is None or sample.create_time_ns is None or expected_ct == sample.create_time_ns:
                return "terminal"
        return "child"

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

    def collect(
        self,
        *,
        host_pid: Optional[int] = None,
        core_pid: Optional[int] = None,
        terminal_pids: Optional[Dict[int, Optional[int]]] = None,
        min_sample_interval_ms: Optional[int] = None,
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

            roots: List[int] = []
            for candidate in (host_pid, effective_core_pid, *sorted(term_map.keys())):
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

            for sample in subtree:
                identity = (sample.pid, sample.create_time_ns)
                if sample.cpu_time_seconds is not None and sample.cpu_time_seconds >= 0.0:
                    next_cpu_by_identity[identity] = sample.cpu_time_seconds

                ram_prov = MetricProvenance.MEASURED if sample.ram_bytes is not None else MetricProvenance.UNKNOWN
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

                role = self._classify_role(
                    sample,
                    host_pid=host_pid,
                    core_pid=effective_core_pid,
                    terminal_pids=term_map,
                )
                nodes.append(
                    ProcessNodeMetrics(
                        pid=sample.pid,
                        ppid=sample.ppid,
                        create_time_ns=sample.create_time_ns,
                        role=role,
                        name=sample.name,
                        ram_bytes=sample.ram_bytes,
                        ram_provenance=ram_prov,
                        cpu_usage_percent=cpu_pct,
                        cpu_provenance=cpu_prov,
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

                cat_cpu, cat_cpu_prov = self._aggregate_cpu_provenance(
                    [n.cpu_usage_percent for n in role_nodes],
                    [n.cpu_provenance for n in role_nodes],
                )
                categories[role_name] = CategoryResourceMetrics(
                    role=role_name,
                    process_count=len(role_nodes),
                    ram_bytes=cat_ram,
                    ram_provenance=cat_ram_prov,
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

            host_ram_bytes = 0
            if host_pid is not None:
                for n in nodes:
                    if n.pid == host_pid and n.ram_bytes is not None:
                        host_ram_bytes = n.ram_bytes
                        break

            tree_cpu_pct, tree_cpu_prov = self._aggregate_cpu_provenance(
                [n.cpu_usage_percent for n in nodes],
                [n.cpu_provenance for n in nodes],
            )

            sample_interval = int(round(elapsed_ns / 1_000_000)) if can_compute_delta and elapsed_ns is not None else None
            snapshot = ProcessTreeMetricsSnapshot(
                root_pid=host_pid if host_pid is not None else effective_core_pid,
                host_pid=host_pid,
                core_pid=effective_core_pid,
                host_ram_bytes=host_ram_bytes,
                total_tree_ram_bytes=total_ram,
                ram_provenance=ram_provenance,
                cpu_usage_percent=tree_cpu_pct,
                cpu_provenance=tree_cpu_prov,
                sample_interval_ms=sample_interval,
                process_count=len(nodes),
                sampled_at_unix_ms=int(time.time() * 1000),
                categories=categories,
                nodes=nodes,
            )

            self._previous_cpu_by_identity = next_cpu_by_identity
            self._last_sample_at_ns = now_ns
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
