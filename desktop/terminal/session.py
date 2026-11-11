"""Terminal Session Entity for AI Coordinator Desktop (Marco 4D.2).

Manages an individual terminal session with strict distinction between
Human Interactive Terminals and Governed Agent Sessions.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ai_coordinator.human_workspace import HumanPrincipal
from ai_coordinator.process import ProcessIdentity

from .buffer import BoundedTerminalBuffer
from .pty_backend import BasePtyBackend, create_pty_backend


class SessionKind(str, enum.Enum):
    """Distinguishes Human Interactive Terminals from Governed Agent Sessions."""

    HUMAN_TERMINAL = "human_terminal"
    GOVERNED_AGENT = "governed_agent"


class TerminalSession:
    """Represents a governed terminal instance inside a tab pane."""

    def __init__(
        self,
        tab_id: str,
        pane_id: int,
        kind: SessionKind,
        command: List[str],
        cwd: Path,
        profile_name: str,
        principal: HumanPrincipal,
        backend: Optional[BasePtyBackend] = None,
        max_buffer_lines: int = 1000,
        max_buffer_bytes: int = 512 * 1024,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        self.session_id = str(uuid.uuid4())
        self.tab_id = tab_id
        self.pane_id = pane_id
        self.kind = kind
        self.command = command
        self.cwd = cwd.resolve()
        self.profile_name = profile_name
        self.principal = principal
        self.cols = cols
        self.rows = rows

        self.buffer = BoundedTerminalBuffer(max_lines=max_buffer_lines, max_bytes=max_buffer_bytes)
        self.backend = backend or create_pty_backend(cols=cols, rows=rows)
        self.backend.set_output_callback(self._on_output)

        self.identity: Optional[ProcessIdentity] = None
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _on_output(self, chunk: str) -> None:
        self.buffer.append(chunk)

    def start(self, env: Optional[Dict[str, str]] = None, max_memory_mb: int = 256) -> ProcessIdentity:
        """Spawn the session with bounded containment."""
        self.identity = self.backend.spawn(
            cmd=self.command,
            cwd=self.cwd,
            env=env,
            max_memory_mb=max_memory_mb,
        )
        return self.identity

    def write(self, data: Union[str, bytes]) -> int:
        return self.backend.write(data)

    def resize(self, cols: int, rows: int) -> bool:
        self.cols = cols
        self.rows = rows
        return self.backend.resize(cols, rows)

    @property
    def is_alive(self) -> bool:
        return self.backend.is_alive()

    @property
    def exit_code(self) -> Optional[int]:
        return self.backend.get_exit_code()

    @property
    def pid(self) -> Optional[int]:
        return self.identity.pid if self.identity else None

    def get_output(self) -> List[str]:
        return self.buffer.get_lines()

    def get_new_output(self, since_seq: int = 0) -> tuple[List[str], int]:
        return self.buffer.get_new_lines(since_seq)

    def get_sanitized_export(self) -> List[str]:
        return self.buffer.export_sanitized()

    def terminate(self) -> bool:
        return self.backend.terminate()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tab_id": self.tab_id,
            "pane_id": self.pane_id,
            "kind": self.kind.value,
            "command": self.command,
            "cwd": str(self.cwd),
            "profile_name": self.profile_name,
            "owner_id": self.principal.owner_id,
            "role": self.principal.role,
            "pid": self.pid,
            "is_alive": self.is_alive,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "buffer_lines": self.buffer.line_count,
            "buffer_bytes": self.buffer.byte_count,
        }
