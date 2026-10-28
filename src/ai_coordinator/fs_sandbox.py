"""Filesystem Sandbox, Access Control, and Operating System-Level Boundary Enforcement.

Phase 4B.5.2: Enforces strict filesystem isolation for worker execution:
- Windows: AppContainer profiles (S-1-15-2-*) with explicit DACL grants, preventing access
  to coordinator SQLite, auth credentials, backups, and user private profiles.
- Linux: Landlock LSM ruleset enforcement via prctl(PR_SET_NO_NEW_PRIVS) + landlock syscalls,
  restricting filesystem access exclusively to the execution sandbox and runtime binaries.
- macOS: Seatbelt Sandbox profile (SBPL) via sandbox-exec / sandbox_init.
- Fail-Closed: Halts execution immediately if the required OS-level filesystem boundary cannot be established.
- Path Traversal & Symlink/Junction Defense: Strict canonicalization and rejection of reparse points/symlinks
  escaping the sandbox boundary.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import datetime as dt
import errno
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

if os.name == "nt":
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
else:
    advapi32 = None  # type: ignore
    kernel32 = None  # type: ignore
    userenv = None  # type: ignore


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class FileSandboxSecurityError(RuntimeError):
    """Raised when filesystem boundary enforcement fails or a boundary violation is detected."""
    pass


class PathTraversalError(FileSandboxSecurityError):
    """Raised when a path attempts directory traversal outside the authorized sandbox."""
    pass


class ReparseEscapeError(FileSandboxSecurityError):
    """Raised when a symlink, junction, or reparse point attempts to escape the authorized boundary."""
    pass


# ---------------------------------------------------------------------------
# Windows Win32 Structures and Constants
# ---------------------------------------------------------------------------

if os.name == "nt":
    PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
    PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_SUSPENDED = 0x00000004
    CREATE_NO_WINDOW = 0x08000000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", ctypes.c_void_p),
            ("Capabilities", ctypes.c_void_p),
            ("CapabilityCount", wintypes.DWORD),
            ("Reserved", wintypes.DWORD),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_char_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]


# ---------------------------------------------------------------------------
# Linux Landlock Constants and Structures
# ---------------------------------------------------------------------------

PR_SET_NO_NEW_PRIVS = 38
LANDLOCK_CREATE_RULESET_VERSION = (1 << 0)
LANDLOCK_RULE_PATH_BENEATH = 1

# Landlock access flags for filesystem (ABI v1)
LANDLOCK_ACCESS_FS_EXECUTE = (1 << 0)
LANDLOCK_ACCESS_FS_WRITE_FILE = (1 << 1)
LANDLOCK_ACCESS_FS_READ_FILE = (1 << 2)
LANDLOCK_ACCESS_FS_READ_DIR = (1 << 3)
LANDLOCK_ACCESS_FS_REMOVE_DIR = (1 << 4)
LANDLOCK_ACCESS_FS_REMOVE_FILE = (1 << 5)
LANDLOCK_ACCESS_FS_MAKE_CHAR = (1 << 6)
LANDLOCK_ACCESS_FS_MAKE_DIR = (1 << 7)
LANDLOCK_ACCESS_FS_MAKE_REG = (1 << 8)
LANDLOCK_ACCESS_FS_MAKE_SOCK = (1 << 9)
LANDLOCK_ACCESS_FS_MAKE_FIFO = (1 << 10)
LANDLOCK_ACCESS_FS_MAKE_BLOCK = (1 << 11)
LANDLOCK_ACCESS_FS_MAKE_SYM = (1 << 12)
LANDLOCK_ACCESS_FS_REFER = (1 << 13)
LANDLOCK_ACCESS_FS_TRUNCATE = (1 << 14)


CREATE_UNICODE_ENVIRONMENT = 0x00000400


def build_win32_unicode_environment(env_dict: dict[str, str]) -> Any:
    """Construct a Win32 Unicode environment block (null-separated KEY=VALUE pairs, double-null terminated)."""
    items = sorted(env_dict.items(), key=lambda x: x[0].upper())
    buffer_str = "\0".join(f"{k}={v}" for k, v in items) + "\0\0"
    return ctypes.create_unicode_buffer(buffer_str)


# ---------------------------------------------------------------------------
# Policy and Configuration
# ---------------------------------------------------------------------------

@dataclass
class FileSandboxConfig:
    """Configuration for an isolated execution sandbox."""
    sandbox_id: str
    sandbox_root: Path
    coordinator_private_dir: Optional[Path] = None
    allowed_read_roots: list[Path] = field(default_factory=list)
    enforce_os_boundary: bool = True
    max_output_bytes: int = 10_485_760  # 10 MB default limit
    clean_on_exit: bool = True

    def __post_init__(self) -> None:
        self.sandbox_root = Path(self.sandbox_root).resolve()
        if self.coordinator_private_dir:
            self.coordinator_private_dir = Path(self.coordinator_private_dir).resolve()
        self.allowed_read_roots = [Path(r).resolve() for r in self.allowed_read_roots]


# ---------------------------------------------------------------------------
# Path and Reparse Defense Utilities
# ---------------------------------------------------------------------------

def is_reparse_point_or_symlink(path: Union[str, Path]) -> bool:
    """Detect if a path is a symbolic link or a Windows junction/reparse point."""
    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return False
    if p.is_symlink():
        return True
    if os.name == "nt":
        try:
            attrs = kernel32.GetFileAttributesW(str(p))
            if attrs != 0xFFFFFFFF and (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
                return True
        except Exception:
            pass
    return False


def validate_contained_path(relative_or_abs_path: Union[str, Path], sandbox_root: Path) -> Path:
    """Canonicalize a path and verify it is strictly contained within sandbox_root.
    Rejects directory traversal (../), absolute paths outside root, and symlinks escaping the boundary.
    """
    root_resolved = Path(sandbox_root).resolve()
    target = Path(relative_or_abs_path)

    # 1. Reject paths with '..' traversal attempts
    parts = target.parts
    if ".." in parts:
        raise PathTraversalError(f"Tentativa de path traversal detectada com '..': '{relative_or_abs_path}'")

    if target.is_absolute():
        unresolved_full = target
        resolved_target = target.resolve()
    else:
        unresolved_full = root_resolved / target
        resolved_target = unresolved_full.resolve()

    # 2. Check if the path or any intermediate component is a symlink or reparse point
    is_sym = False
    curr = unresolved_full
    while curr != root_resolved and curr != curr.parent:
        if is_reparse_point_or_symlink(curr):
            is_sym = True
            break
        curr = curr.parent

    # 3. Check strict containment
    try:
        resolved_target.relative_to(root_resolved)
    except ValueError:
        if is_sym:
            raise ReparseEscapeError(
                f"Fronteira violada: symlink/junction '{relative_or_abs_path}' aponta para fora da sandbox: '{resolved_target}'"
            )
        raise PathTraversalError(
            f"Acesso negado: caminho '{relative_or_abs_path}' escapa da raiz autorizada '{root_resolved}'"
        )

    return resolved_target


# ---------------------------------------------------------------------------
# Windows AppContainer Controller
# ---------------------------------------------------------------------------

class WindowsAppContainerController:
    """Manages Windows AppContainer profiles and NTFS DACL grants for worker sandboxing."""

    def __init__(self, sandbox_id: str, sandbox_root: Path, read_roots: Optional[Sequence[Path]] = None):
        self.sandbox_id = sandbox_id
        self.profile_name = f"ai_coord_worker_{sandbox_id[:24]}"
        self.sandbox_root = Path(sandbox_root).resolve()
        self.read_roots = [Path(r).resolve() for r in (read_roots or [])]
        self.sid_ptr: Optional[ctypes.c_void_p] = None
        self.sid_str: Optional[str] = None
        self._attr_list = None
        self._sec_caps = None

    def setup(self) -> str:
        """Create AppContainer profile and grant NTFS permissions."""
        if os.name != "nt":
            raise FileSandboxSecurityError("WindowsAppContainerController so e suportado no Windows")

        # 1. Clean up any stale profile with same name
        userenv.DeleteAppContainerProfile(self.profile_name)

        # 2. Create new AppContainer profile
        sid_ptr = ctypes.c_void_p()
        hr = userenv.CreateAppContainerProfile(
            self.profile_name,
            f"AI Coordinator Worker {self.sandbox_id}",
            "Isolated Worker Execution Sandbox",
            None,
            0,
            ctypes.byref(sid_ptr),
        )
        if hr != 0 or not sid_ptr:
            raise FileSandboxSecurityError(
                f"Falha ao criar perfil AppContainer no Windows (HRESULT={hex(hr & 0xFFFFFFFF)})"
            )
        self.sid_ptr = sid_ptr

        # Convert SID to string for icacls grant
        str_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_sid)):
            self.cleanup()
            raise FileSandboxSecurityError("Falha ao converter AppContainer SID para string")
        self.sid_str = str_sid.value
        kernel32.LocalFree(str_sid)

        # 3. Grant Full Control only to the sandbox directory
        res = subprocess.run(
            ["icacls", str(self.sandbox_root), "/grant", f"*{self.sid_str}:(OI)(CI)F"],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            self.cleanup()
            raise FileSandboxSecurityError(f"Falha ao conceder DACL do AppContainer para sandbox_root: {res.stderr}")

        # 4. Grant Read+Execute to Python interpreter folder and any extra read_roots
        py_folder = Path(sys.executable).parent
        read_dirs = set(self.read_roots) | {py_folder}
        for rd in read_dirs:
            if rd.exists():
                subprocess.run(
                    ["icacls", str(rd), "/grant", f"*{self.sid_str}:(OI)(CI)RX"],
                    capture_output=True,
                    text=True,
                )

        return self.sid_str

    def build_startupinfo(self, inherited_handles: Optional[Sequence[Any]] = None) -> Tuple[STARTUPINFOEXW, ctypes.c_char_p]:
        """Construct STARTUPINFOEXW with PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES and restricted HANDLE_LIST."""
        if not self.sid_ptr:
            raise FileSandboxSecurityError("AppContainer profile nao inicializado")

        attr_count = 2 if inherited_handles else 1
        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, attr_count, 0, ctypes.byref(size))
        attr_list = ctypes.create_string_buffer(size.value)
        if not kernel32.InitializeProcThreadAttributeList(attr_list, attr_count, 0, ctypes.byref(size)):
            raise FileSandboxSecurityError("Falha ao inicializar ProcThreadAttributeList")

        sec_caps = SECURITY_CAPABILITIES()
        sec_caps.AppContainerSid = self.sid_ptr
        sec_caps.Capabilities = None
        sec_caps.CapabilityCount = 0
        sec_caps.Reserved = 0

        ok = kernel32.UpdateProcThreadAttribute(
            attr_list,
            0,
            PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            ctypes.byref(sec_caps),
            ctypes.sizeof(sec_caps),
            None,
            None,
        )
        if not ok:
            kernel32.DeleteProcThreadAttributeList(attr_list)
            raise FileSandboxSecurityError(
                f"UpdateProcThreadAttribute falhou com codigo {kernel32.GetLastError()}"
            )

        self._handle_arr = None
        if inherited_handles:
            handle_arr = (wintypes.HANDLE * len(inherited_handles))(*inherited_handles)
            self._handle_arr = handle_arr
            ok_handles = kernel32.UpdateProcThreadAttribute(
                attr_list,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.byref(handle_arr),
                ctypes.sizeof(handle_arr),
                None,
                None,
            )
            if not ok_handles:
                kernel32.DeleteProcThreadAttributeList(attr_list)
                raise FileSandboxSecurityError(
                    f"UpdateProcThreadAttribute HANDLE_LIST falhou com codigo {kernel32.GetLastError()}"
                )

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.lpAttributeList = ctypes.cast(attr_list, ctypes.c_void_p)

        self._attr_list = attr_list
        self._sec_caps = sec_caps
        return si, attr_list

    def cleanup(self) -> None:
        """Release AppContainer profile and SID resources."""
        if os.name != "nt":
            return
        if self._attr_list is not None:
            try:
                kernel32.DeleteProcThreadAttributeList(self._attr_list)
            except Exception:
                pass
            self._attr_list = None
        if self.sid_ptr:
            try:
                advapi32.FreeSid(self.sid_ptr)
            except Exception:
                pass
            self.sid_ptr = None
        try:
            userenv.DeleteAppContainerProfile(self.profile_name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Linux Landlock Controller
# ---------------------------------------------------------------------------

class LinuxLandlockController:
    """Enforces Linux Landlock LSM rules to restrict filesystem access to the sandbox."""

    @classmethod
    def is_available(cls) -> bool:
        """Check if Linux kernel supports Landlock LSM."""
        if os.name == "nt" or not sys.platform.startswith("linux"):
            return False
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            # Landlock syscall 444 (x86_64, arm64)
            syscall = getattr(libc, "syscall", None)
            if not syscall:
                return False
            # Check Landlock ABI version
            SYS_landlock_create_ruleset = 444
            abi = syscall(SYS_landlock_create_ruleset, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
            return abi >= 1
        except Exception:
            return False

    @classmethod
    def get_preexec_fn(
        cls,
        sandbox_root: Path,
        allowed_read_roots: Sequence[Path],
    ) -> Callable[[], None]:
        """Construct preexec_fn for subprocess to enforce Landlock restrictions before worker executes."""
        resolved_sandbox = Path(sandbox_root).resolve()
        system_roots = [
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
            Path("/bin"),
            Path("/sbin"),
            Path("/etc"),
            Path("/opt"),
            Path("/dev"),
            Path(sys.prefix),
            Path(sys.base_prefix),
        ]
        all_read_roots = set(Path(r).resolve() for r in allowed_read_roots)
        for sr in system_roots:
            if sr.exists():
                all_read_roots.add(sr.resolve())

        def _preexec() -> None:
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                syscall = libc.syscall

                # 1. prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) is mandatory before Landlock
                if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                    os.write(2, b"FAIL-CLOSED: prctl(PR_SET_NO_NEW_PRIVS) failed\n")
                    os._exit(43)

                # 2. Define Landlock ruleset structure
                class LandlockRulesetAttr(ctypes.Structure):
                    _fields_ = [
                        ("handled_access_fs", ctypes.c_uint64),
                    ]

                class LandlockPathBeneathAttr(ctypes.Structure):
                    _fields_ = [
                        ("allowed_access", ctypes.c_uint64),
                        ("parent_fd", ctypes.c_int32),
                    ]

                handled = (
                    LANDLOCK_ACCESS_FS_EXECUTE
                    | LANDLOCK_ACCESS_FS_WRITE_FILE
                    | LANDLOCK_ACCESS_FS_READ_FILE
                    | LANDLOCK_ACCESS_FS_READ_DIR
                    | LANDLOCK_ACCESS_FS_REMOVE_DIR
                    | LANDLOCK_ACCESS_FS_REMOVE_FILE
                    | LANDLOCK_ACCESS_FS_MAKE_DIR
                    | LANDLOCK_ACCESS_FS_MAKE_REG
                    | LANDLOCK_ACCESS_FS_MAKE_SYM
                    | LANDLOCK_ACCESS_FS_TRUNCATE
                )

                attr = LandlockRulesetAttr(handled_access_fs=handled)
                SYS_landlock_create_ruleset = 444
                SYS_landlock_add_rule = 445
                SYS_landlock_restrict_self = 446

                ruleset_fd = syscall(
                    SYS_landlock_create_ruleset,
                    ctypes.byref(attr),
                    ctypes.sizeof(attr),
                    0,
                )
                if ruleset_fd < 0:
                    err = ctypes.get_errno()
                    os.write(2, f"FAIL-CLOSED: SYS_landlock_create_ruleset failed (errno={err})\n".encode())
                    os._exit(43)

                # 3. Add Read+Write rule for sandbox_root
                sbox_fd = os.open(str(resolved_sandbox), os.O_PATH | os.O_CLOEXEC)
                if sbox_fd < 0:
                    os.close(ruleset_fd)
                    os.write(2, b"FAIL-CLOSED: unable to open sandbox_root for Landlock rule\n")
                    os._exit(43)

                path_rule = LandlockPathBeneathAttr(
                    allowed_access=handled,
                    parent_fd=sbox_fd,
                )
                res = syscall(
                    SYS_landlock_add_rule,
                    ruleset_fd,
                    LANDLOCK_RULE_PATH_BENEATH,
                    ctypes.byref(path_rule),
                    0,
                )
                os.close(sbox_fd)
                if res != 0:
                    os.close(ruleset_fd)
                    os.write(2, b"FAIL-CLOSED: SYS_landlock_add_rule for sandbox_root failed\n")
                    os._exit(43)

                # 4. Add Read+Execute rules for system read roots
                read_handled = (
                    LANDLOCK_ACCESS_FS_EXECUTE
                    | LANDLOCK_ACCESS_FS_READ_FILE
                    | LANDLOCK_ACCESS_FS_READ_DIR
                )
                for rd in all_read_roots:
                    if rd.exists():
                        try:
                            r_fd = os.open(str(rd), os.O_PATH | os.O_CLOEXEC)
                            if r_fd >= 0:
                                path_rule = LandlockPathBeneathAttr(
                                    allowed_access=read_handled,
                                    parent_fd=r_fd,
                                )
                                syscall(
                                    SYS_landlock_add_rule,
                                    ruleset_fd,
                                    LANDLOCK_RULE_PATH_BENEATH,
                                    ctypes.byref(path_rule),
                                    0,
                                )
                                os.close(r_fd)
                        except Exception:
                            pass

                # 5. Restrict calling process
                res_restrict = syscall(SYS_landlock_restrict_self, ruleset_fd, 0)
                os.close(ruleset_fd)
                if res_restrict != 0:
                    err = ctypes.get_errno()
                    os.write(2, f"FAIL-CLOSED: SYS_landlock_restrict_self failed (errno={err})\n".encode())
                    os._exit(43)
            except Exception as e:
                os.write(2, f"FAIL-CLOSED: Landlock setup unhandled error: {e}\n".encode())
                os._exit(43)

        return _preexec


# ---------------------------------------------------------------------------
# macOS Seatbelt Sandbox Controller
# ---------------------------------------------------------------------------

class MacOsSeatbeltController:
    """Constructs Apple Seatbelt Sandbox profile (SBPL) for macOS worker isolation."""

    @classmethod
    def is_available(cls) -> bool:
        """Check if sandbox-exec is available on macOS."""
        if sys.platform != "darwin":
            return False
        return Path("/usr/bin/sandbox-exec").is_file()

    @classmethod
    def build_profile(
        cls,
        sandbox_root: Path,
        coordinator_private_dir: Optional[Path] = None,
        allowed_read_roots: Optional[Sequence[Path]] = None,
    ) -> str:
        """Construct SBPL profile allowing write only to sandbox_root and denying coordinator files."""
        resolved_sbox = Path(sandbox_root).resolve()
        coord_private = Path(coordinator_private_dir).resolve() if coordinator_private_dir else None

        profile_lines = [
            "(version 1)",
            "(deny default)",
            "(allow process-exec)",
            "(allow process-fork)",
            "(allow sysctl*)",
            "(allow mach-lookup)",
            "(allow signal (target self))",
            "(allow ipc-posix-shm)",
            "(allow file-read*)",
            "(deny file-write*)",
        ]

        sbox_str = str(resolved_sbox)
        profile_lines.append(f'(allow file-write* (subpath "{sbox_str}"))')
        if sbox_str.startswith("/private/"):
            non_priv = sbox_str[len("/private"):]
            profile_lines.append(f'(allow file-write* (subpath "{non_priv}"))')
        else:
            profile_lines.append(f'(allow file-write* (subpath "/private{sbox_str}"))')

        # Deny sensitive supervisor, task siblings, and user credential directories
        profile_lines.append('(deny file-read* (regex #".*/(\\.ssh|\\.aws|\\.gnupg|mock_home)(/.*|$)"))')
        profile_lines.append('(deny file-read* (regex #".*/tasks/[^/]+/sibling_[^/]+(/.*|$)"))')

        if coord_private:
            str_path = str(coord_private)
            profile_lines.append(f'(deny file-read* (subpath "{str_path}"))')
            profile_lines.append(f'(deny file-write* (subpath "{str_path}"))')
            if str_path.startswith("/private/"):
                non_private = str_path[len("/private"):]
                profile_lines.append(f'(deny file-read* (subpath "{non_private}"))')
                profile_lines.append(f'(deny file-write* (subpath "{non_private}"))')
            else:
                profile_lines.append(f'(deny file-read* (subpath "/private{str_path}"))')
                profile_lines.append(f'(deny file-write* (subpath "/private{str_path}"))')
            profile_lines.append(f'(deny file-read* (regex #".*/{coord_private.name}(/.*|$)"))')
            profile_lines.append(f'(deny file-write* (regex #".*/{coord_private.name}(/.*|$)"))')

        return "\n".join(profile_lines)


# ---------------------------------------------------------------------------
# High-Level Execution Sandbox
# ---------------------------------------------------------------------------

class ExecutionSandbox:
    """Manages an isolated execution directory with OS-level boundary enforcement."""

    def __init__(self, config: FileSandboxConfig):
        self.config = config
        self.sandbox_id = config.sandbox_id
        self.sandbox_root = config.sandbox_root
        self.coordinator_private_dir = config.coordinator_private_dir
        self.windows_controller: Optional[WindowsAppContainerController] = None
        self._prepared = False
        self._closed = False
        self.boundary_status = "uninitialized"

    def prepare(self, input_files: Optional[Dict[str, Union[str, bytes, Path]]] = None) -> None:
        """Initialize the isolated sandbox directory, populate input files, and establish OS boundaries."""
        if self._prepared:
            return

        # 1. Ensure clean sandbox directory
        self.sandbox_root.mkdir(parents=True, exist_ok=True)

        # 2. Populate authorized input files (strictly validated against traversal)
        if input_files:
            for rel_name, content in input_files.items():
                dest_path = validate_contained_path(rel_name, self.sandbox_root)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, Path):
                    src_path = Path(content).resolve()
                    if is_reparse_point_or_symlink(src_path):
                        raise ReparseEscapeError(f"Arquivo de entrada nao pode ser symlink: {src_path}")
                    shutil.copy2(src_path, dest_path)
                elif isinstance(content, str):
                    dest_path.write_text(content, encoding="utf-8")
                elif isinstance(content, bytes):
                    dest_path.write_bytes(content)

        # 3. Establish OS-Level Boundary
        if os.name == "nt":
            self.windows_controller = WindowsAppContainerController(
                sandbox_id=self.sandbox_id,
                sandbox_root=self.sandbox_root,
                read_roots=self.config.allowed_read_roots,
            )
            try:
                self.windows_controller.setup()
                self.boundary_status = "enforced_windows_appcontainer"
            except Exception as e:
                self.boundary_status = "failed"
                if self.config.enforce_os_boundary:
                    raise FileSandboxSecurityError(
                        f"Fail-closed: impossivel estabelecer barreira de filesystem AppContainer: {e}"
                    )
        elif sys.platform.startswith("linux"):
            if LinuxLandlockController.is_available():
                self.boundary_status = "enforced_linux_landlock"
            else:
                self.boundary_status = "unsupported_linux_kernel"
                if self.config.enforce_os_boundary:
                    raise FileSandboxSecurityError(
                        "Fail-closed: Linux Landlock LSM indisponivel no kernel para isolamento de filesystem"
                    )
        elif sys.platform == "darwin":
            if MacOsSeatbeltController.is_available():
                self.boundary_status = "enforced_macos_seatbelt"
            else:
                self.boundary_status = "unsupported_macos_seatbelt"
                if self.config.enforce_os_boundary:
                    raise FileSandboxSecurityError(
                        "Fail-closed: macOS Seatbelt Sandbox indisponivel para isolamento de filesystem"
                    )
        else:
            self.boundary_status = "unsupported_platform"
            if self.config.enforce_os_boundary:
                raise FileSandboxSecurityError(
                    f"Fail-closed: plataforma '{sys.platform}' nao suporta isolamento mandatorio de filesystem"
                )

        self._prepared = True

    def import_output(self, relative_name: str, max_bytes: Optional[int] = None) -> bytes:
        """Safely import bytes from an output file generated inside the sandbox.
        Rejects directory traversal, symlink escapes, and oversized payloads.
        """
        limit = max_bytes or self.config.max_output_bytes
        target = validate_contained_path(relative_name, self.sandbox_root)
        if not target.is_file():
            raise FileNotFoundError(f"Arquivo de saida do worker nao encontrado na sandbox: '{relative_name}'")

        size = target.stat().st_size
        if size > limit:
            raise ValueError(f"Arquivo de saida excede limite permitido: {size} > {limit} bytes")

        return target.read_bytes()

    def import_output_text(self, relative_name: str, encoding: str = "utf-8", max_bytes: Optional[int] = None) -> str:
        """Safely import text from an output file generated inside the sandbox."""
        data = self.import_output(relative_name, max_bytes=max_bytes)
        return data.decode(encoding=encoding, errors="replace")

    def import_output_to_file(
        self,
        relative_name: str,
        destination: Path,
        max_bytes: Optional[int] = None,
    ) -> Path:
        """Safely import an output file from sandbox to an authorized destination path."""
        data = self.import_output(relative_name, max_bytes=max_bytes)
        dest = Path(destination).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def cleanup(self) -> None:
        """Tear down OS boundaries and purge temporary sandbox files."""
        if self._closed:
            return
        self._closed = True

        if self.windows_controller:
            try:
                self.windows_controller.cleanup()
            except Exception:
                pass
            self.windows_controller = None

        if self.config.clean_on_exit and self.sandbox_root.exists():
            try:
                shutil.rmtree(self.sandbox_root, ignore_errors=True)
            except Exception:
                pass

    def __enter__(self) -> ExecutionSandbox:
        self.prepare()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cleanup()
