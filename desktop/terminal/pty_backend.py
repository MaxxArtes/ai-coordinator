"""PTY Backend Abstraction for AI Coordinator Desktop (Marco 4D.2).

Provides:
- Native Windows 10/11 ConPTY backend with Job Object containment.
- POSIX PTY backend (Linux/macOS) with process groups.
- Subprocess Pipe backend (cross-platform fallback and testing).
"""

from __future__ import annotations

import abc
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Union

from ai_coordinator.process import (
    PosixProcessController,
    ProcessIdentity,
    ResourceLimits,
    WindowsProcessController,
)

if os.name == "nt":
    import ctypes
    import ctypes.wintypes as w

    # Isolated WinDLL instance prevents mutating argtypes on shared ctypes.windll.kernel32
    # (which breaks src/ai_coordinator/process.py STARTUPINFOW pointer checks in same-process runs).
    # use_last_error: ctypes saves GetLastError right after each call (read via ctypes.get_last_error()),
    # so the value cannot be clobbered by the interpreter between the failing call and the read.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # The private instance does not inherit prototypes declared elsewhere on ctypes.windll.kernel32,
    # so every handle-taking function used here is typed explicitly (64-bit HANDLE, not C int).
    k32.CreatePipe.argtypes = [ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), ctypes.c_void_p, w.DWORD]
    k32.CreatePipe.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.CloseHandle.restype = w.BOOL
    k32.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    k32.ReadFile.restype = w.BOOL
    k32.WriteFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    k32.WriteFile.restype = w.BOOL
    k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k32.GetExitCodeProcess.restype = w.BOOL
    k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k32.WaitForSingleObject.restype = w.DWORD

    class COORD(ctypes.Structure):
        _fields_ = [("X", w.SHORT), ("Y", w.SHORT)]

    HPCON = ctypes.c_void_p

    if hasattr(k32, "CreatePseudoConsole"):
        k32.CreatePseudoConsole.argtypes = [COORD, w.HANDLE, w.HANDLE, w.DWORD, ctypes.POINTER(HPCON)]
        k32.CreatePseudoConsole.restype = ctypes.c_long

        k32.ResizePseudoConsole.argtypes = [HPCON, COORD]
        k32.ResizePseudoConsole.restype = ctypes.c_long

        k32.ClosePseudoConsole.argtypes = [HPCON]
        k32.ClosePseudoConsole.restype = None

        k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)]
        k32.InitializeProcThreadAttributeList.restype = w.BOOL

        k32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            w.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        k32.UpdateProcThreadAttribute.restype = w.BOOL

        k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k32.DeleteProcThreadAttributeList.restype = None

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", w.DWORD),
                ("lpReserved", w.LPWSTR),
                ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR),
                ("dwX", w.DWORD),
                ("dwY", w.DWORD),
                ("dwXSize", w.DWORD),
                ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD),
                ("dwYCountChars", w.DWORD),
                ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD),
                ("wShowWindow", w.WORD),
                ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE),
                ("hStdError", w.HANDLE),
            ]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", w.HANDLE),
                ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD),
                ("dwThreadId", w.DWORD),
            ]

        k32.CreateProcessW.argtypes = [
            w.LPCWSTR,
            w.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            w.BOOL,
            w.DWORD,
            ctypes.c_void_p,
            w.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW),
            ctypes.POINTER(PROCESS_INFORMATION),
        ]
        k32.CreateProcessW.restype = w.BOOL

        k32.ResumeThread.argtypes = [w.HANDLE]
        k32.ResumeThread.restype = w.DWORD

        k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        k32.TerminateProcess.restype = w.BOOL


class BasePtyBackend(abc.ABC):
    """Abstract base class for PTY session drivers."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols = cols
        self.rows = rows
        self._on_output_callback: Optional[Callable[[str], None]] = None

    def set_output_callback(self, callback: Callable[[str], None]) -> None:
        self._on_output_callback = callback

    @abc.abstractmethod
    def spawn(
        self,
        cmd: List[str],
        cwd: Path,
        env: Optional[dict[str, str]] = None,
        max_memory_mb: int = 256,
    ) -> ProcessIdentity:
        """Spawn the process in the PTY with containment."""

    @abc.abstractmethod
    def write(self, data: Union[str, bytes]) -> int:
        """Write user input to PTY stdin."""

    @abc.abstractmethod
    def resize(self, cols: int, rows: int) -> bool:
        """Dynamically resize PTY dimensions."""

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """Check if child process is still running."""

    @abc.abstractmethod
    def get_exit_code(self) -> Optional[int]:
        """Return exit code if terminated."""

    @abc.abstractmethod
    def terminate(self) -> bool:
        """Atomically kill process tree and release PTY handles."""


class WindowsConPtyBackend(BasePtyBackend):
    """Windows native ConPTY backend with Job Object containment."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        super().__init__(cols, rows)
        if os.name != "nt" or not hasattr(k32, "CreatePseudoConsole"):
            raise RuntimeError("Windows ConPTY is not supported on this platform")

        self._hpcon = None
        self._job_handle: Optional[int] = None
        self._h_in_write = None
        self._h_out_read = None
        self._pi = None
        self._p_attr = None
        self._attr_buf = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._exit_code: Optional[int] = None

    def spawn(
        self,
        cmd: List[str],
        cwd: Path,
        env: Optional[dict[str, str]] = None,
        max_memory_mb: int = 256,
    ) -> ProcessIdentity:
        # 1. Create communication pipes
        h_in_read = w.HANDLE()
        h_in_write = w.HANDLE()
        h_out_read = w.HANDLE()
        h_out_write = w.HANDLE()

        k32.CreatePipe(ctypes.byref(h_in_read), ctypes.byref(h_in_write), None, 0)
        k32.CreatePipe(ctypes.byref(h_out_read), ctypes.byref(h_out_write), None, 0)
        self._h_in_write = h_in_write
        self._h_out_read = h_out_read

        # 2. Create Job Object with resource limits
        self._job_handle = WindowsProcessController.create_job_object(
            kill_on_close=True,
            resource_limits=ResourceLimits(max_memory_bytes=max_memory_mb * 1024 * 1024),
        )
        if not self._job_handle:
            k32.CloseHandle(h_in_read)
            k32.CloseHandle(h_in_write)
            k32.CloseHandle(h_out_read)
            k32.CloseHandle(h_out_write)
            raise RuntimeError("Failed to create Windows Job Object containment for ConPTY")

        # 3. Create PseudoConsole
        h_pc = HPCON()
        size = COORD(self.cols, self.rows)
        res = k32.CreatePseudoConsole(size, h_in_read, h_out_write, 0, ctypes.byref(h_pc))
        if res != 0:
            k32.CloseHandle(h_in_read)
            k32.CloseHandle(h_in_write)
            k32.CloseHandle(h_out_read)
            k32.CloseHandle(h_out_write)
            raise RuntimeError(f"CreatePseudoConsole failed with HRESULT {res}")
        self._hpcon = h_pc

        # 4. Prepare attribute list
        attr_size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        self._attr_buf = (ctypes.c_char * attr_size.value)()
        self._p_attr = ctypes.cast(self._attr_buf, ctypes.c_void_p)
        k32.InitializeProcThreadAttributeList(self._p_attr, 1, 0, ctypes.byref(attr_size))

        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
        upd_ok = k32.UpdateProcThreadAttribute(
            self._p_attr,
            0,
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            self._hpcon,
            ctypes.sizeof(self._hpcon),
            None,
            None,
        )
        if not upd_ok:
            err = ctypes.get_last_error()
            k32.CloseHandle(h_in_read)
            k32.CloseHandle(h_in_write)
            k32.CloseHandle(h_out_read)
            k32.CloseHandle(h_out_write)
            self.terminate()
            raise RuntimeError(f"UpdateProcThreadAttribute failed with Win32 error code {err}")

        # 5. Setup STARTUPINFOEXW and spawn suspended
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = self._p_attr
        # Explicit stdio isolation: bind child stdio directly to ConPTY pipe ends,
        # preventing child console processes from inheriting parent Python/IPC stdio.
        STARTF_USESTDHANDLES = 0x00000100
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput = h_in_read
        si.StartupInfo.hStdOutput = h_out_write
        si.StartupInfo.hStdError = h_out_write
        self._pi = PROCESS_INFORMATION()

        # Flags: EXTENDED_STARTUPINFO_PRESENT (0x80000) | CREATE_SUSPENDED (0x4)
        CREATE_SUSPENDED = 0x00000004
        EXTENDED_STARTUPINFO_PRESENT = 0x00080000
        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED

        # Construct explicit, sanitized child environment
        # Strip COORDINATOR_IPC_TOKEN and internal host secrets so child never inherits them
        clean_env = dict(env) if env is not None else dict(os.environ)
        for secret_k in ("COORDINATOR_IPC_TOKEN", "COORDINATOR_SUPERVISOR_SECRET"):
            clean_env.pop(secret_k, None)

        env_str = "".join(f"{k}={v}\0" for k, v in clean_env.items()) + "\0"
        env_block = ctypes.cast(ctypes.create_unicode_buffer(env_str), ctypes.c_void_p)
        flags |= 0x00000400  # CREATE_UNICODE_ENVIRONMENT

        cmd_line = subprocess.list2cmdline(cmd)
        cmd_buf = ctypes.create_unicode_buffer(cmd_line)
        working_dir = str(cwd.resolve())

        ok = k32.CreateProcessW(
            None,
            cmd_buf,
            None,
            None,
            False,
            flags,
            env_block,
            working_dir,
            ctypes.cast(ctypes.byref(si), ctypes.POINTER(STARTUPINFOW)),
            ctypes.byref(self._pi),
        )

        # Close ConPTY-owned pipe ends in parent
        k32.CloseHandle(h_in_read)
        k32.CloseHandle(h_out_write)

        if not ok:
            err = ctypes.get_last_error()
            self.terminate()
            raise RuntimeError(f"CreateProcessW failed with Win32 error code {err}")

        # 6. Containment: Assign suspended child process to Job Object BEFORE execution
        assigned = WindowsProcessController.assign_to_job(
            self._job_handle, self._pi.dwProcessId, proc_handle=self._pi.hProcess
        )
        if not assigned:
            k32.TerminateProcess(self._pi.hProcess, 1)
            self.terminate()
            raise RuntimeError(
                f"Atomic containment failed: unable to assign PID {self._pi.dwProcessId} to Job Object. "
                "Child process terminated immediately before execution."
            )

        # 7. Safe Resumption: Process is confirmed contained inside Job Object
        resume_count = k32.ResumeThread(self._pi.hThread)
        if resume_count == 0xFFFFFFFF or resume_count < 0:
            k32.TerminateProcess(self._pi.hProcess, 1)
            self.terminate()
            raise RuntimeError(
                f"Failed to resume suspended ConPTY thread for PID {self._pi.dwProcessId}. "
                "Child process terminated for safety."
            )

        self._running = True

        # 8. Start reader thread
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        pid = self._pi.dwProcessId
        create_ns = WindowsProcessController.get_creation_time(pid) or 0
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return ProcessIdentity(pid=pid, create_time_ns=create_ns, system="windows", start_iso=now_iso)

    def _read_loop(self) -> None:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buf = (ctypes.c_char * 4096)()
        bread = w.DWORD()
        while self._running and self._h_out_read:
            ok = k32.ReadFile(self._h_out_read, buf, 4096, ctypes.byref(bread), None)
            if not ok or bread.value == 0:
                break
            raw_bytes = bytes(buf[: bread.value])
            text = decoder.decode(raw_bytes, final=False)
            if text and self._on_output_callback:
                try:
                    self._on_output_callback(text)
                except Exception:
                    pass

    def write(self, data: Union[str, bytes]) -> int:
        if not self._running or not self._h_in_write:
            return 0
        payload = data.encode("utf-8") if isinstance(data, str) else data
        written = w.DWORD()
        k32.WriteFile(self._h_in_write, payload, len(payload), ctypes.byref(written), None)
        return written.value

    def resize(self, cols: int, rows: int) -> bool:
        if not self._hpcon:
            return False
        self.cols = cols
        self.rows = rows
        res = k32.ResizePseudoConsole(self._hpcon, COORD(cols, rows))
        return res == 0

    def is_alive(self) -> bool:
        if not self._running or not self._pi:
            return False
        code = w.DWORD()
        if k32.GetExitCodeProcess(self._pi.hProcess, ctypes.byref(code)):
            if code.value == 259:  # STILL_ACTIVE
                return True
            self._exit_code = code.value
        return False

    def get_exit_code(self) -> Optional[int]:
        self.is_alive()
        return self._exit_code

    def terminate(self) -> bool:
        self._running = False
        success = True

        if self._job_handle:
            try:
                WindowsProcessController.terminate_job(self._job_handle, exit_code=0)
            except Exception:
                success = False

        if self._pi and self._pi.hProcess:
            try:
                k32.WaitForSingleObject(self._pi.hProcess, 1500)
            except Exception:
                pass

        if self._hpcon:
            try:
                k32.ClosePseudoConsole(self._hpcon)
            except Exception:
                pass
            self._hpcon = None

        if self._h_in_write:
            try:
                k32.CloseHandle(self._h_in_write)
            except Exception:
                pass
            self._h_in_write = None

        if self._h_out_read:
            try:
                k32.CloseHandle(self._h_out_read)
            except Exception:
                pass
            self._h_out_read = None

        if self._pi:
            try:
                k32.CloseHandle(self._pi.hProcess)
                k32.CloseHandle(self._pi.hThread)
            except Exception:
                pass
            self._pi = None

        if self._p_attr:
            try:
                k32.DeleteProcThreadAttributeList(self._p_attr)
            except Exception:
                pass
            self._p_attr = None

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)

        return success


class SubprocessPipeBackend(BasePtyBackend):
    """Cross-platform fallback backend using standard subprocess pipes."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        super().__init__(cols, rows)
        self.process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False
        self._exit_code: Optional[int] = None
        self._job_handle: Optional[int] = None

    def spawn(
        self,
        cmd: List[str],
        cwd: Path,
        env: Optional[dict[str, str]] = None,
        max_memory_mb: int = 256,
    ) -> ProcessIdentity:
        working_dir = str(cwd.resolve())
        clean_env = dict(env) if env is not None else dict(os.environ)
        for secret_k in ("COORDINATOR_IPC_TOKEN", "COORDINATOR_SUPERVISOR_SECRET"):
            clean_env.pop(secret_k, None)

        self.process = subprocess.Popen(
            cmd,
            cwd=working_dir,
            env=clean_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            bufsize=0,
        )

        if os.name == "nt":
            self._job_handle = WindowsProcessController.create_job_object(
                kill_on_close=True,
                resource_limits=ResourceLimits(max_memory_bytes=max_memory_mb * 1024 * 1024),
            )
            if self._job_handle:
                WindowsProcessController.assign_to_job(self._job_handle, self.process.pid)

        self._running = True
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        pid = self.process.pid
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        create_ns = 0
        if os.name == "nt":
            create_ns = WindowsProcessController.get_creation_time(pid) or 0
            system = "windows"
        else:
            create_ns = PosixProcessController.get_creation_time(pid) or int(time.time_ns())
            system = "posix"

        return ProcessIdentity(pid=pid, create_time_ns=create_ns, system=system, start_iso=now_iso)

    def _read_loop(self) -> None:
        import codecs

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while self._running and self.process and self.process.stdout:
            try:
                chunk = self.process.stdout.read(1024)
                if not chunk:
                    break
                text = decoder.decode(chunk, final=False)
                if text and self._on_output_callback:
                    self._on_output_callback(text)
            except Exception:
                break

    def write(self, data: Union[str, bytes]) -> int:
        if not self._running or not self.process or not self.process.stdin:
            return 0
        payload = data.encode("utf-8") if isinstance(data, str) else data
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
            return len(payload)
        except (BrokenPipeError, OSError):
            return 0

    def resize(self, cols: int, rows: int) -> bool:
        self.cols = cols
        self.rows = rows
        return True

    def is_alive(self) -> bool:
        if not self.process:
            return False
        poll = self.process.poll()
        if poll is None:
            return True
        self._exit_code = poll
        return False

    def get_exit_code(self) -> Optional[int]:
        self.is_alive()
        return self._exit_code

    def terminate(self) -> bool:
        self._running = False
        if not self.process:
            return True

        if self._job_handle and os.name == "nt":
            WindowsProcessController.terminate_job(self._job_handle, exit_code=0)

        try:
            self.process.terminate()
            self.process.wait(timeout=1.0)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)

        return True


def create_pty_backend(
    cols: int = 80,
    rows: int = 24,
    prefer_conpty: bool = True,
    backend_type: Optional[str] = None,
) -> BasePtyBackend:
    """Factory creating appropriate PTY backend for platform."""
    chosen = backend_type or os.environ.get("COORDINATOR_PTY_BACKEND", "auto")
    if chosen == "pipe":
        return SubprocessPipeBackend(cols=cols, rows=rows)

    if (chosen in ("auto", "conpty")) and prefer_conpty and os.name == "nt" and hasattr(k32, "CreatePseudoConsole"):
        try:
            return WindowsConPtyBackend(cols=cols, rows=rows)
        except Exception:
            pass
    return SubprocessPipeBackend(cols=cols, rows=rows)
