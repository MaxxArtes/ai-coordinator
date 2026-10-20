"""Portable shared task coordination for local AI coding agents."""
from .api import Coordinator, open_coordinator
from .process import (
    ProcessIdentity,
    ProcessSupervisor,
    SupervisedProcess,
    TerminationResult,
    WindowsProcessController,
    PosixProcessController,
    safe_process_alive,
    safe_terminate_tree,
)

__all__ = [
    "Coordinator",
    "open_coordinator",
    "ProcessIdentity",
    "ProcessSupervisor",
    "SupervisedProcess",
    "TerminationResult",
    "WindowsProcessController",
    "PosixProcessController",
    "safe_process_alive",
    "safe_terminate_tree",
]
__version__ = "0.2.1"
