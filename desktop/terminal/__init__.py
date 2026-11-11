"""Desktop Terminal and Supervision subsystem for AI Coordinator Desktop (Marco 4D.2)."""

from .buffer import BoundedTerminalBuffer, SessionLayoutExport
from .manager import MAX_PANES_PER_TAB, TabInfo, TerminalManager, TerminalManagerError
from .pty_backend import BasePtyBackend, SubprocessPipeBackend, WindowsConPtyBackend, create_pty_backend
from .session import SessionKind, TerminalSession

__all__ = [
    "BoundedTerminalBuffer",
    "SessionLayoutExport",
    "SessionKind",
    "TerminalSession",
    "BasePtyBackend",
    "WindowsConPtyBackend",
    "SubprocessPipeBackend",
    "create_pty_backend",
    "TabInfo",
    "TerminalManager",
    "TerminalManagerError",
    "MAX_PANES_PER_TAB",
]
