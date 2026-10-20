"""Process lifecycle, identity verification, and safe termination.

Ensures no PID reuse kills strangers, provides Win32 Job Objects with
KILL_ON_JOB_CLOSE for fail-closed workers, Linux pidfd/process group
support, and stable OS creation-time validation.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import datetime as dt
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# Windows ctypes declarations (guarded for cross-platform import)
# ---------------------------------------------------------------------------
if os.name == "nt":
    import ctypes
    import ctypes.wintypes as wintypes

    kernel32 = ctypes.windll.kernel32

    # Access Rights
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000

    # Job Object Constants
    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    # Wait Constants
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102
    WAIT_FAILED = 0xFFFFFFFF
    INFINITE = 0xFFFFFFFF

    # Toolhelp32 Constants
    TH32CS_SNAPPROCESS = 0x00000002

    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryLimit", ctypes.c_size_t),
            ("PeakJobMemoryLimit", ctypes.c_size_t),
        ]

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    # Function signatures to avoid 64-bit pointer truncation
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL

    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE

    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL

    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32First.restype = wintypes.BOOL

    kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    kernel32.Process32Next.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcessIdentity:
    """Stable operating system identity of a process."""
    pid: int
    create_time_ns: int
    system: str = "windows" if os.name == "nt" else "posix"
    start_iso: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "create_time_ns": self.create_time_ns,
            "system": self.system,
            "start_iso": self.start_iso,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessIdentity:
        return cls(
            pid=int(data["pid"]),
            create_time_ns=int(data["create_time_ns"]),
            system=str(data.get("system", "windows" if os.name == "nt" else "posix")),
            start_iso=str(data.get("start_iso", "")),
        )

    def is_alive(self) -> bool:
        """Check if this exact process instance is still running."""
        return safe_process_alive(self.pid, expected_create_time_ns=self.create_time_ns)


@dataclass
class TerminationResult:
    """Detailed result of a process termination attempt."""
    success: bool
    exit_code: Optional[int]
    reason: str  # "normal", "terminated", "timeout", "identity_mismatch", "not_found", "permission_denied"
    duration_seconds: float
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Windows Process Controller
# ---------------------------------------------------------------------------

class WindowsProcessController:
    """Handles Win32 process queries, handle lifetimes, and Job Objects."""

    @staticmethod
    def get_creation_time(pid: int) -> Optional[int]:
        """Query kernel creation time (100ns FILETIME) for a Windows process."""
        if pid <= 0 or os.name != "nt":
            return None
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            ct = FILETIME()
            et = FILETIME()
            kt = FILETIME()
            ut = FILETIME()
            if kernel32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
                return (ct.dwHighDateTime << 32) + ct.dwLowDateTime
            return None
        finally:
            kernel32.CloseHandle(h)

    @classmethod
    def is_running(cls, pid: int, expected_create_time_ns: Optional[int] = None) -> bool:
        """Check if a Windows process is currently running and matches creation time."""
        if pid <= 0 or os.name != "nt":
            return False
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            if expected_create_time_ns is not None:
                ct = FILETIME()
                et = FILETIME()
                kt = FILETIME()
                ut = FILETIME()
                if kernel32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
                    actual = (ct.dwHighDateTime << 32) + ct.dwLowDateTime
                    if actual != expected_create_time_ns:
                        return False
                else:
                    return False
            return kernel32.WaitForSingleObject(h, 0) != WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(h)

    @staticmethod
    def create_job_object(kill_on_close: bool = True) -> Optional[int]:
        """Create a Win32 Job Object with optional KILL_ON_JOB_CLOSE."""
        if os.name != "nt":
            return None
        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            return None
        if kill_on_close:
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                h_job,
                JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                kernel32.CloseHandle(h_job)
                return None
        return int(h_job)

    @staticmethod
    def assign_to_job(job_handle: int, pid: int) -> bool:
        """Assign a process to a Job Object."""
        if os.name != "nt" or not job_handle or pid <= 0:
            return False
        h_proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h_proc:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job_handle, h_proc))
        finally:
            kernel32.CloseHandle(h_proc)

    @staticmethod
    def terminate_job(job_handle: int, exit_code: int = 1) -> bool:
        """Terminate all processes in a Job Object atomically."""
        if os.name != "nt" or not job_handle:
            return False
        return bool(kernel32.TerminateJobObject(job_handle, exit_code))

    @staticmethod
    def close_handle(handle: Optional[int]) -> bool:
        """Safely close a Windows handle."""
        if os.name != "nt" or not handle:
            return False
        return bool(kernel32.CloseHandle(handle))

    @staticmethod
    def find_child_pids(parent_pid: int) -> list[int]:
        """Enumerate direct child PIDs via Toolhelp32 snapshot."""
        if os.name != "nt" or parent_pid <= 0:
            return []
        h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not h_snap or h_snap == WAIT_FAILED:
            return []
        children: list[int] = []
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if kernel32.Process32First(h_snap, ctypes.byref(pe)):
                while True:
                    if pe.th32ParentProcessID == parent_pid and pe.th32ProcessID != parent_pid:
                        children.append(pe.th32ProcessID)
                    if not kernel32.Process32Next(h_snap, ctypes.byref(pe)):
                        break
        finally:
            kernel32.CloseHandle(h_snap)
        return children

    @classmethod
    def terminate_by_handle(
        cls,
        pid: int,
        expected_create_time_ns: Optional[int] = None,
        exit_code: int = 1,
        timeout_seconds: float = 5.0,
    ) -> TerminationResult:
        """Verify identity via stable handle, terminate, and await confirmation."""
        start_time = time.monotonic()
        if pid <= 0:
            return TerminationResult(False, None, "not_found", 0.0, "Invalid PID")

        # Open process handle with terminate, query, and synchronize rights
        h_proc = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not h_proc:
            # Check if process does not exist or permission denied
            err = kernel32.GetLastError()
            if err == 5:  # ERROR_ACCESS_DENIED
                return TerminationResult(False, None, "permission_denied", time.monotonic() - start_time, "Access denied")
            return TerminationResult(True, None, "not_found", time.monotonic() - start_time, "Process does not exist")

        try:
            # 1. Identity Verification
            ct = FILETIME()
            et = FILETIME()
            kt = FILETIME()
            ut = FILETIME()
            if kernel32.GetProcessTimes(h_proc, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
                current_create_time = (ct.dwHighDateTime << 32) + ct.dwLowDateTime
                if expected_create_time_ns is not None and current_create_time != expected_create_time_ns:
                    return TerminationResult(
                        success=False,
                        exit_code=None,
                        reason="identity_mismatch",
                        duration_seconds=time.monotonic() - start_time,
                        detail=f"PID {pid} was recycled (expected={expected_create_time_ns}, actual={current_create_time})",
                    )
            elif expected_create_time_ns is not None:
                return TerminationResult(
                    success=False,
                    exit_code=None,
                    reason="identity_mismatch",
                    duration_seconds=time.monotonic() - start_time,
                    detail="Cannot verify creation time on target process",
                )

            # 2. Check if already completely exited
            exit_code_val = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code_val)):
                if exit_code_val.value != 259 and kernel32.WaitForSingleObject(h_proc, 0) == WAIT_OBJECT_0:
                    return TerminationResult(
                        success=True,
                        exit_code=exit_code_val.value,
                        reason="normal",
                        duration_seconds=time.monotonic() - start_time,
                        detail="Process had already exited",
                    )

            # 3. Terminate via handle
            ok = kernel32.TerminateProcess(h_proc, exit_code)
            if not ok:
                err = kernel32.GetLastError()
                # If ERROR_ACCESS_DENIED (5) check if it terminated between checks
                if kernel32.WaitForSingleObject(h_proc, 50) == WAIT_OBJECT_0:
                    final_code = wintypes.DWORD()
                    kernel32.GetExitCodeProcess(h_proc, ctypes.byref(final_code))
                    return TerminationResult(
                        success=True,
                        exit_code=final_code.value,
                        reason="terminated",
                        duration_seconds=time.monotonic() - start_time,
                        detail="Process terminated during action",
                    )
                return TerminationResult(
                    success=False,
                    exit_code=None,
                    reason="permission_denied" if err == 5 else "terminated",
                    duration_seconds=time.monotonic() - start_time,
                    detail=f"TerminateProcess failed with error code {err}",
                )

            # 4. Await confirmed exit
            timeout_ms = max(50, int(timeout_seconds * 1000))
            wait_res = kernel32.WaitForSingleObject(h_proc, timeout_ms)
            duration = time.monotonic() - start_time

            if wait_res == WAIT_OBJECT_0:
                final_code = wintypes.DWORD()
                kernel32.GetExitCodeProcess(h_proc, ctypes.byref(final_code))
                return TerminationResult(
                    success=True,
                    exit_code=final_code.value,
                    reason="terminated",
                    duration_seconds=duration,
                    detail="Process confirmed terminated",
                )
            elif wait_res == WAIT_TIMEOUT:
                return TerminationResult(
                    success=False,
                    exit_code=None,
                    reason="timeout",
                    duration_seconds=duration,
                    detail=f"Process did not exit within {timeout_seconds}s timeout",
                )
            else:
                return TerminationResult(
                    success=False,
                    exit_code=None,
                    reason="unknown",
                    duration_seconds=duration,
                    detail=f"WaitForSingleObject returned {wait_res}",
                )
        finally:
            kernel32.CloseHandle(h_proc)


# ---------------------------------------------------------------------------
# POSIX Process Controller
# ---------------------------------------------------------------------------

class PosixProcessController:
    """Handles Linux/Unix process verification, pidfd, and process groups."""

    @staticmethod
    def get_creation_time(pid: int) -> Optional[int]:
        """Read starttime from /proc/{pid}/stat on Linux, or ps on macOS/Unix."""
        if pid <= 0 or os.name == "nt":
            return None
        stat_file = Path(f"/proc/{pid}/stat")
        if stat_file.exists():
            try:
                content = stat_file.read_text(encoding="ascii", errors="replace")
                rparen = content.rfind(")")
                if rparen != -1:
                    fields = content[rparen + 1:].split()
                    if len(fields) >= 20:
                        return int(fields[19])  # starttime in jiffies
            except Exception:
                pass
        # Fallback for macOS and non-Linux POSIX using `ps`
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "lstart="],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if out:
                parsed_dt = dt.datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
                return int(parsed_dt.timestamp() * 1_000_000_000)
        except Exception:
            pass
        try:
            return Path(f"/proc/{pid}").stat().st_ctime_ns
        except Exception:
            return None

    @staticmethod
    def open_pidfd(pid: int) -> Optional[int]:
        """Open a stable pidfd file descriptor if supported by the kernel and Python."""
        if hasattr(os, "pidfd_open") and pid > 0:
            try:
                return os.pidfd_open(pid, 0)
            except Exception:
                return None
        return None

    @classmethod
    def terminate_process(
        cls,
        pid: int,
        expected_create_time_ns: Optional[int] = None,
        timeout_seconds: float = 5.0,
    ) -> TerminationResult:
        """Verify identity, send SIGTERM then SIGKILL, and await confirmation."""
        start = time.monotonic()
        if pid <= 0:
            return TerminationResult(False, None, "not_found", 0.0, "Invalid PID")

        # Identity check
        if expected_create_time_ns is not None:
            cur_time = cls.get_creation_time(pid)
            if cur_time is None:
                # Conservative principle: identity cannot be verified, fail-closed
                return TerminationResult(
                    success=False,
                    exit_code=None,
                    reason="identity_unknown",
                    duration_seconds=time.monotonic() - start,
                    detail=f"PID {pid} creation time cannot be verified; refusing destructive termination",
                )
            if cur_time != expected_create_time_ns:
                return TerminationResult(
                    success=False,
                    exit_code=None,
                    reason="identity_mismatch",
                    duration_seconds=time.monotonic() - start,
                    detail=f"PID {pid} was recycled",
                )

        # Check if already exited or reapable child
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
                return TerminationResult(True, exit_code, "normal", time.monotonic() - start, "Process already exited")
        except (ChildProcessError, OSError):
            pass

        # Check alive
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return TerminationResult(True, None, "not_found", time.monotonic() - start, "Process does not exist")
        except PermissionError:
            return TerminationResult(False, None, "permission_denied", time.monotonic() - start, "Permission denied")

        # Use pidfd if available and keep it open across the entire termination cycle
        pidfd = cls.open_pidfd(pid)
        try:
            if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                except Exception:
                    os.kill(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            if pidfd is not None:
                os.close(pidfd)
            return TerminationResult(True, None, "normal", time.monotonic() - start, "Exited before signal")

        try:
            # Poll with timeout, reaping child zombies
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                try:
                    wpid, status = os.waitpid(pid, os.WNOHANG)
                    if wpid == pid:
                        exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
                        return TerminationResult(True, exit_code, "terminated", time.monotonic() - start, "Terminated gracefully with SIGTERM")
                except (ChildProcessError, OSError):
                    pass

                try:
                    os.kill(pid, 0)
                    stat_file = Path(f"/proc/{pid}/stat")
                    if stat_file.exists():
                        try:
                            content = stat_file.read_text(encoding="ascii", errors="replace")
                            rparen = content.rfind(")")
                            if rparen != -1 and content[rparen + 1:].split()[0] == "Z":
                                return TerminationResult(True, None, "terminated", time.monotonic() - start, "Terminated gracefully with SIGTERM (zombie)")
                        except Exception:
                            pass
                    time.sleep(0.05)
                except ProcessLookupError:
                    return TerminationResult(True, None, "terminated", time.monotonic() - start, "Terminated gracefully with SIGTERM")

            # Force SIGKILL if still running, re-checking identity to prevent TOCTOU races
            if expected_create_time_ns is not None and pidfd is None:
                cur_time = cls.get_creation_time(pid)
                if cur_time is None:
                    return TerminationResult(
                        success=False,
                        exit_code=None,
                        reason="identity_unknown",
                        duration_seconds=time.monotonic() - start,
                        detail=f"PID {pid} identity cannot be re-verified before SIGKILL; aborting destructive escalation",
                    )
                if cur_time != expected_create_time_ns:
                    return TerminationResult(
                        success=False,
                        exit_code=None,
                        reason="identity_mismatch",
                        duration_seconds=time.monotonic() - start,
                        detail=f"PID {pid} was recycled before SIGKILL escalation",
                    )

            try:
                if pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                    try:
                        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    except Exception:
                        os.kill(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)

                time.sleep(0.1)
                try:
                    wpid, status = os.waitpid(pid, os.WNOHANG)
                    if wpid == pid:
                        exit_code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
                        return TerminationResult(True, exit_code, "terminated", time.monotonic() - start, "Terminated forcefully with SIGKILL")
                except (ChildProcessError, OSError):
                    pass
                try:
                    os.kill(pid, 0)
                    stat_file = Path(f"/proc/{pid}/stat")
                    if stat_file.exists():
                        try:
                            content = stat_file.read_text(encoding="ascii", errors="replace")
                            rparen = content.rfind(")")
                            if rparen != -1 and content[rparen + 1:].split()[0] == "Z":
                                return TerminationResult(True, None, "terminated", time.monotonic() - start, "Terminated forcefully with SIGKILL (zombie)")
                        except Exception:
                            pass
                    return TerminationResult(False, None, "timeout", time.monotonic() - start, "Process required SIGKILL and may linger")
                except ProcessLookupError:
                    return TerminationResult(True, None, "terminated", time.monotonic() - start, "Terminated forcefully with SIGKILL")
            except ProcessLookupError:
                return TerminationResult(True, None, "terminated", time.monotonic() - start, "Terminated forcefully with SIGKILL")
        finally:
            if pidfd is not None:
                os.close(pidfd)

    @classmethod
    def terminate_process_group(cls, pgid: int, timeout_seconds: float = 5.0) -> TerminationResult:
        """Terminate an entire process group safely."""
        start = time.monotonic()
        if pgid <= 0 or os.name == "nt":
            return TerminationResult(False, None, "not_found", 0.0, "Invalid PGID")
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return TerminationResult(True, None, "not_found", time.monotonic() - start, "Process group does not exist")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                return TerminationResult(True, None, "terminated", time.monotonic() - start, "Process group terminated with SIGTERM")

        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return TerminationResult(True, None, "terminated", time.monotonic() - start, "Process group terminated with SIGKILL")


# ---------------------------------------------------------------------------
# High-Level Safe Utilities
# ---------------------------------------------------------------------------

def safe_process_alive(pid: int, expected_create_time_ns: Optional[int] = None) -> bool:
    """Verify if a process is alive and matches the expected OS creation time without reaping."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return WindowsProcessController.is_running(pid, expected_create_time_ns=expected_create_time_ns)
    else:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, ValueError):
            return False
        except PermissionError:
            # Process exists but owned by someone else
            if expected_create_time_ns is not None:
                cur_time = PosixProcessController.get_creation_time(pid)
                return cur_time == expected_create_time_ns
            return True

        # Non-destructive zombie/defunct check on Linux (/proc) and macOS/BSD (ps)
        stat_file = Path(f"/proc/{pid}/stat")
        if stat_file.exists():
            try:
                content = stat_file.read_text(encoding="ascii", errors="replace")
                rparen = content.rfind(")")
                if rparen != -1:
                    state = content[rparen + 1:].split()[0]
                    if state in ("Z", "X", "x"):  # Zombie or dead
                        return False
            except Exception:
                pass
        else:
            # Fallback for macOS and non-Linux POSIX using non-destructive `ps`
            try:
                out = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "state="],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
                if out and out.startswith("Z"):
                    return False
            except Exception:
                pass

        if expected_create_time_ns is not None:
            cur_time = PosixProcessController.get_creation_time(pid)
            if cur_time is not None and cur_time != expected_create_time_ns:
                return False
        return True


def safe_terminate_tree(
    target: Union[subprocess.Popen, SupervisedProcess, int],
    expected_create_time_ns: Optional[int] = None,
    timeout_seconds: float = 5.0,
) -> TerminationResult:
    """Safely terminate a process or tree, preventing PID recycling accidents."""
    if isinstance(target, SupervisedProcess):
        return target.terminate(timeout_seconds=timeout_seconds)

    pid = target.pid if isinstance(target, subprocess.Popen) else target
    if pid <= 0:
        return TerminationResult(False, None, "not_found", 0.0, "Invalid target")

    if os.name == "nt":
        # First enumerate child PIDs and capture their creation timestamps while parent is still open
        child_pids = WindowsProcessController.find_child_pids(pid)
        child_records = [
            (c_pid, WindowsProcessController.get_creation_time(c_pid))
            for c_pid in child_pids
        ]
        res = WindowsProcessController.terminate_by_handle(
            pid,
            expected_create_time_ns=expected_create_time_ns,
            timeout_seconds=timeout_seconds,
        )
        # Terminate any surviving children using verified creation times to prevent PID recycling accidents
        for c_pid, c_time in child_records:
            try:
                WindowsProcessController.terminate_by_handle(
                    c_pid,
                    expected_create_time_ns=c_time,
                    timeout_seconds=1.0,
                )
            except Exception:
                pass
        return res
    else:
        # On POSIX: terminate entire process group first to prevent orphan descendants, then leader
        try:
            PosixProcessController.terminate_process_group(pid, timeout_seconds=timeout_seconds / 2)
        except Exception:
            pass
        return PosixProcessController.terminate_process(
            pid,
            expected_create_time_ns=expected_create_time_ns,
            timeout_seconds=timeout_seconds / 2,
        )


# ---------------------------------------------------------------------------
# Supervised Process and Supervisor
# ---------------------------------------------------------------------------

class SupervisedProcess:
    """A managed process instance with verified identity and Job Object protection."""

    def __init__(
        self,
        proc: subprocess.Popen,
        identity: ProcessIdentity,
        job_handle: Optional[int] = None,
        role: str = "worker",
    ):
        self.proc = proc
        self.identity = identity
        self.job_handle = job_handle
        self.role = role
        self._closed = False

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def returncode(self) -> Optional[int]:
        return self.proc.returncode

    @property
    def stdout(self) -> Any:
        return self.proc.stdout

    @property
    def stderr(self) -> Any:
        return self.proc.stderr

    @property
    def stdin(self) -> Any:
        return self.proc.stdin

    def poll(self) -> Optional[int]:
        code = self.proc.poll()
        if code is not None and not self._closed:
            self.close()
        return code

    def wait(self, timeout: Optional[float] = None) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
            return code
        finally:
            self.close()

    def communicate(
        self,
        input: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[str, str]:
        try:
            return self.proc.communicate(input=input, timeout=timeout)
        finally:
            if self.proc.poll() is not None:
                self.close()

    def is_alive(self) -> bool:
        if self.proc.poll() is not None:
            return False
        alive = self.identity.is_alive()
        if not alive:
            try:
                self.proc.wait(timeout=0.05)
            except Exception:
                pass
        return alive

    def terminate(self, timeout_seconds: float = 5.0) -> TerminationResult:
        """Terminate this process and all descendants safely."""
        start = time.monotonic()
        if os.name == "nt":
            if self.job_handle:
                # Terminate all processes in the job atomically
                WindowsProcessController.terminate_job(self.job_handle)
                WindowsProcessController.close_handle(self.job_handle)
                self.job_handle = None
            res = WindowsProcessController.terminate_by_handle(
                self.pid,
                expected_create_time_ns=self.identity.create_time_ns,
                timeout_seconds=timeout_seconds,
            )
            try:
                self.proc.wait(timeout=0.1)
            except Exception:
                pass
            self.close()
            return res
        else:
            if self.role == "worker":
                try:
                    PosixProcessController.terminate_process_group(self.pid, timeout_seconds=timeout_seconds / 2)
                except Exception:
                    pass
            res = PosixProcessController.terminate_process(
                self.pid,
                expected_create_time_ns=self.identity.create_time_ns,
                timeout_seconds=timeout_seconds / 2 if self.role == "worker" else timeout_seconds,
            )
            try:
                self.proc.wait(timeout=0.1)
            except Exception:
                pass
            self.close()
            return res

    def close(self) -> None:
        """Clean up Win32 handles."""
        if not self._closed:
            self._closed = True
            if os.name == "nt" and self.job_handle:
                WindowsProcessController.close_handle(self.job_handle)
                self.job_handle = None

    def __enter__(self) -> SupervisedProcess:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.proc.poll() is None:
            self.terminate(timeout_seconds=2.0)
        self.close()


class ProcessSupervisor:
    """Spawns and supervises worker and coordinator processes."""

    @classmethod
    def capture_identity(cls, pid: int) -> ProcessIdentity:
        """Capture the stable identity of a newly started process."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        if os.name == "nt":
            create_ns = WindowsProcessController.get_creation_time(pid) or 0
            return ProcessIdentity(pid=pid, create_time_ns=create_ns, system="windows", start_iso=now_iso)
        else:
            create_ns = PosixProcessController.get_creation_time(pid) or int(time.time_ns())
            return ProcessIdentity(pid=pid, create_time_ns=create_ns, system="posix", start_iso=now_iso)

    @classmethod
    def spawn(
        cls,
        cmd: list[str],
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[dict[str, str]] = None,
        stdin: Optional[Any] = None,
        stdout: Optional[Any] = None,
        stderr: Optional[Any] = None,
        role: str = "worker",
        use_job_object: bool = True,
        text: bool = True,
        encoding: str = "utf-8",
        errors: str = "replace",
    ) -> SupervisedProcess:
        """Spawn a supervised process with fail-closed Job Object on Windows or session on POSIX."""
        job_handle: Optional[int] = None
        creation_flags = 0

        if os.name == "nt":
            creation_flags |= subprocess.CREATE_NO_WINDOW
            if use_job_object and role == "worker":
                job_handle = WindowsProcessController.create_job_object(kill_on_close=True)

        kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd else None,
            "env": env,
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
            "text": text,
            "encoding": encoding,
            "errors": errors,
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = creation_flags
        else:
            if role == "worker":
                kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd, **kwargs)
        identity = cls.capture_identity(proc.pid)

        if os.name == "nt" and job_handle:
            # Assign process to Job Object
            assigned = WindowsProcessController.assign_to_job(job_handle, proc.pid)
            if not assigned and proc.poll() is None:
                # Could not assign and process still runs -> fail closed for safety
                kernel32.CloseHandle(job_handle)
                proc.kill()
                raise RuntimeError(f"Falha ao vincular processo {proc.pid} ao Job Object")

        return SupervisedProcess(proc=proc, identity=identity, job_handle=job_handle, role=role)
