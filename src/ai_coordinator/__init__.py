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
from .streaming import (
    StreamingScrubber,
    SafeStreamConsumer,
    StreamConsumeResult,
)
from .contracts import (
    HeadlessContract,
    get_client_contract,
    validate_headless_arguments,
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
    "StreamingScrubber",
    "SafeStreamConsumer",
    "StreamConsumeResult",
    "HeadlessContract",
    "get_client_contract",
    "validate_headless_arguments",
]
__version__ = "0.2.1"
