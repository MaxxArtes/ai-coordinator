"""Bounded in-memory ring buffer for AI Coordinator Desktop terminals (Marco 4D.2).

Enforces:
- 1,000 lines + 512 KB byte cap per terminal (bounded RAM).
- FIFO eviction policy (oldest lines dropped on limit).
- Sensitive token redaction upon session export.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

SENSITIVE_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]{20,}", re.IGNORECASE),
    re.compile(r"password\s*[:=]\s*['\"]?\S+['\"]?", re.IGNORECASE),
    re.compile(r"(api[_-]?key|secret)\s*[:=]\s*['\"]?[a-zA-Z0-9_\-\.]{12,}['\"]?", re.IGNORECASE),
]


@dataclass
class TerminalLine:
    text: str
    byte_length: int


class BoundedTerminalBuffer:
    """Circular ring buffer enforcing strict line and byte boundaries for a terminal."""

    def __init__(self, max_lines: int = 1000, max_bytes: int = 512 * 1024) -> None:
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.lines: Deque[TerminalLine] = collections.deque()
        self.total_bytes: int = 0

    def append(self, text: str) -> None:
        """Append raw line/chunk, evicting oldest lines if exceeding limits."""
        for raw_line in text.splitlines(keepends=False):
            line_bytes = len(raw_line.encode("utf-8", errors="replace"))
            entry = TerminalLine(text=raw_line, byte_length=line_bytes)
            self.lines.append(entry)
            self.total_bytes += line_bytes

            # Enforce line boundary
            while len(self.lines) > self.max_lines:
                evicted = self.lines.popleft()
                self.total_bytes -= evicted.byte_length

            # Enforce byte boundary
            while self.total_bytes > self.max_bytes and self.lines:
                evicted = self.lines.popleft()
                self.total_bytes -= evicted.byte_length

    def get_lines(self) -> List[str]:
        return [line.text for line in self.lines]

    def get_raw_text(self) -> str:
        return "\n".join(self.get_lines())

    def clear(self) -> None:
        self.lines.clear()
        self.total_bytes = 0

    @property
    def line_count(self) -> int:
        return len(self.lines)

    @property
    def byte_count(self) -> int:
        return self.total_bytes

    def export_sanitized(self) -> List[str]:
        """Export lines with sensitive credentials redacted."""
        sanitized: List[str] = []
        for line in self.lines:
            t = line.text
            for pat in SENSITIVE_PATTERNS:
                t = pat.sub("[REDACTED_CREDENTIAL]", t)
            sanitized.append(t)
        return sanitized


@dataclass
class SessionLayoutExport:
    """Structured export payload when user saves session state."""

    tab_id: str
    profile_name: str
    project_path: str
    terminals: Dict[str, List[str]] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "profile_name": self.profile_name,
            "project_path": self.project_path,
            "terminals": self.terminals,
            "metadata": self.metadata,
        }
