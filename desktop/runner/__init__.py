"""Desktop runner and foundation verification module (Marco 4D.1.4)."""

from .verify_foundation import (
    get_process_tree_memory,
    run_full_foundation_verification,
    verify_clean_install,
    verify_ipc_handshake_and_isolation,
)

__all__ = [
    "get_process_tree_memory",
    "verify_clean_install",
    "verify_ipc_handshake_and_isolation",
    "run_full_foundation_verification",
]
