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
from .storage import (
    Store,
    RunState,
    ACTIVE_RUN_STATES,
    TERMINAL_RUN_STATES,
    VALID_STATUS_TRANSITIONS,
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
    "Store",
    "RunState",
    "ACTIVE_RUN_STATES",
    "TERMINAL_RUN_STATES",
    "VALID_STATUS_TRANSITIONS",
]
__version__ = "0.2.1"
