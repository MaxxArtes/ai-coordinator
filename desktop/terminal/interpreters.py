"""Allow-listed interactive shells and host-to-WSL working-directory mapping."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable, Dict, Iterable, List, Optional, Tuple


DEFAULT_INTERPRETER_ID = "powershell"


class InterpreterError(ValueError):
    """A requested shell is unavailable or cannot represent this working directory."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Interpreter:
    id: str
    label: str
    executable: str
    arguments: Tuple[str, ...] = ()
    cwd_kind: str = "windows"

    def public_dict(self) -> dict:
        return {"id": self.id, "label": self.label}


def _wsl_is_registered() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        root = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as distro:
                        name, _ = winreg.QueryValueEx(distro, "DistributionName")
                        base, _ = winreg.QueryValueEx(distro, "BasePath")
                        if isinstance(name, str) and name and isinstance(base, str) and Path(base).exists():
                            return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def windows_to_wsl_path(path: str) -> str:
    """Map a drive-qualified Windows path to the conventional /mnt/<drive> WSL path."""
    normalized = path.replace("/", "\\")
    if len(normalized) >= 3 and normalized[1:3] == ":\\" and normalized[0].isalpha():
        tail = normalized[3:].replace("\\", "/")
        return f"/mnt/{normalized[0].lower()}/{tail}" if tail else f"/mnt/{normalized[0].lower()}"
    raise InterpreterError("O Bash WSL não pode representar este diretório como caminho /mnt/<unidade>.", "unsupported_cwd")


def discover_interpreters(
    *,
    platform: Optional[str] = None,
    environ: Optional[Dict[str, str]] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    exists: Optional[Callable[[Path], bool]] = None,
    wsl_registered: Optional[bool] = None,
) -> List[Interpreter]:
    """Return only shells that are present; no package is installed or shell executed during discovery."""
    platform = platform or sys_platform()
    env = os.environ if environ is None else environ
    if platform != "win32":
        shells: List[Interpreter] = []
        for shell_id, label, names, args in (
            ("cmd", "CMD", (), ()),
            ("powershell", "PowerShell", ("pwsh", "powershell"), ("-NoLogo",)),
            ("bash_git", "Bash", ("bash",), ("--login", "-i")),
        ):
            if shell_id == "cmd":
                continue
            executable = next((candidate for name in names if (candidate := which(name))), None)
            if executable:
                shells.append(Interpreter(shell_id, label, executable, args))
        return shells

    system_root = PureWindowsPath(env.get("SystemRoot") or r"C:\Windows")
    program_files = PureWindowsPath(env.get("ProgramFiles") or r"C:\Program Files")
    program_files_x86 = PureWindowsPath(env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
    path_exists = exists or (lambda path: path.exists())

    def first_path(candidates: Iterable[PureWindowsPath]) -> Optional[str]:
        for candidate in candidates:
            # Convert the pure Windows path using the host's concrete Path implementation. This keeps discovery
            # correct on Windows and lets platform-mocked unit tests run on POSIX without changing path semantics.
            if path_exists(Path(str(candidate))):
                return str(candidate)
        return None

    found: List[Interpreter] = []
    cmd = first_path((system_root / "System32" / "cmd.exe",)) or which("cmd.exe")
    if cmd:
        found.append(Interpreter("cmd", "CMD", cmd))

    pwsh = first_path((program_files / "PowerShell" / "7" / "pwsh.exe",)) or which("pwsh.exe") or which("pwsh")
    powershell = first_path((system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",))
    powershell = powershell or which("powershell.exe") or which("powershell")
    if pwsh:
        found.append(Interpreter("powershell", "PowerShell 7", pwsh, ("-NoLogo",)))
    elif powershell:
        found.append(Interpreter("powershell", "Windows PowerShell", powershell, ("-NoLogo",)))

    git_bash = first_path((
        program_files / "Git" / "bin" / "bash.exe",
        program_files_x86 / "Git" / "bin" / "bash.exe",
        PureWindowsPath(env.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
    )) or which("bash.exe")
    if git_bash:
        found.append(Interpreter("bash_git", "Bash (Git Bash)", git_bash, ("--login", "-i")))

    wsl = first_path((system_root / "System32" / "wsl.exe",)) or which("wsl.exe") or which("wsl")
    has_distro = _wsl_is_registered() if wsl_registered is None else wsl_registered
    if wsl and has_distro:
        found.append(Interpreter("bash_wsl", "Bash (WSL)", wsl, cwd_kind="wsl"))
    return found


def sys_platform() -> str:
    import sys

    return sys.platform


def resolve_interpreter(interpreter_id: Optional[str], cwd: str, available: Optional[List[Interpreter]] = None):
    choices = discover_interpreters() if available is None else available
    requested = interpreter_id or DEFAULT_INTERPRETER_ID
    match = next((item for item in choices if item.id == requested), None)
    if match is None:
        raise InterpreterError(f"O interpretador {requested!r} não está disponível neste computador.", "unavailable")
    command = [match.executable, *match.arguments]
    launch_cwd = cwd
    extra_env: Dict[str, str] = {}
    if match.id == "bash_git":
        # Git for Windows starts its login shell in the inherited host cwd when this flag is present.
        extra_env["CHERE_INVOKING"] = "1"
    elif match.cwd_kind == "wsl":
        command[1:1] = ["--cd", windows_to_wsl_path(cwd), "--exec", "bash", "-l"]
        launch_cwd = os.environ.get("SystemRoot", r"C:\Windows")
    return match, command, launch_cwd, extra_env
