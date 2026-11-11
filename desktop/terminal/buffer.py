"""Bounded in-memory ring buffer for AI Coordinator Desktop terminals (Marco 4D.2).

Enforces:
- Separation between raw interactive PTY byte/chunk stream (for xterm.js) and
  line-based audit history (for session persistence and security inspection).
- Preserves exact CR/LF, ANSI escape sequences, UTF-8 partial chunks, and prompts
  without line breaks in the interactive raw stream.
- 1,000 lines + 512 KB byte cap per terminal (bounded RAM).
- FIFO eviction policy (oldest lines/chunks dropped on limit).
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
    """Represents a structured line of terminal output for audit and persistence."""

    seq: int
    text: str
    byte_length: int


@dataclass
class RawOutputChunk:
    """Represents a raw chunk of PTY stream preserving CR, LF, and ANSI sequences."""

    seq: int
    data: str
    byte_length: int


class BoundedTerminalBuffer:
    """Circular ring buffer separating raw interactive PTY streams from line-based audit history."""

    def __init__(
        self,
        max_lines: int = 1000,
        max_bytes: int = 512 * 1024,
        max_raw_chunks: int = 5000,
    ) -> None:
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.max_raw_chunks = max_raw_chunks

        # Line-based audit collection
        self.lines: Deque[TerminalLine] = collections.deque()
        self.total_bytes: int = 0

        # Raw chunk collection for interactive terminal emulator (xterm.js)
        self.raw_chunks: Deque[RawOutputChunk] = collections.deque()
        self.total_raw_bytes: int = 0

        self._next_seq: int = 0

    def append(self, text: str) -> None:
        """Append raw chunk/text, updating both raw interactive stream and line-based audit log."""
        if not text:
            return

        # 1. Store in raw chunk ring buffer (preserves CR, LF, ANSI, prompts without newlines)
        chunk_bytes = len(text.encode("utf-8", errors="replace"))
        self._next_seq += 1
        raw_chunk = RawOutputChunk(seq=self._next_seq, data=text, byte_length=chunk_bytes)
        self.raw_chunks.append(raw_chunk)
        self.total_raw_bytes += chunk_bytes

        # Evict old raw chunks if exceeding limit
        while len(self.raw_chunks) > self.max_raw_chunks:
            evicted_chunk = self.raw_chunks.popleft()
            self.total_raw_bytes -= evicted_chunk.byte_length

        while self.total_raw_bytes > self.max_bytes and self.raw_chunks:
            evicted_chunk = self.raw_chunks.popleft()
            self.total_raw_bytes -= evicted_chunk.byte_length

        # 2. Extract lines for structured audit history
        # Note: splitlines allows preserving structured line boundaries for search/export
        raw_lines = text.splitlines(keepends=False)
        for raw_line in raw_lines:
            line_bytes = len(raw_line.encode("utf-8", errors="replace"))
            entry = TerminalLine(seq=self._next_seq, text=raw_line, byte_length=line_bytes)
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
        """Return all audit lines in buffer."""
        return [line.text for line in self.lines]

    def get_new_lines(self, since_seq: int = 0) -> tuple[List[str], int]:
        """Return all audit lines with seq > since_seq and the latest sequence number."""
        if since_seq <= 0:
            return [line.text for line in self.lines], self._next_seq
        return [line.text for line in self.lines if line.seq > since_seq], self._next_seq

    def get_new_raw(self, since_seq: int = 0) -> tuple[str, int]:
        """Return concatenated raw stream data since since_seq for xterm.js rendering."""
        if since_seq <= 0:
            raw_text = "".join(chunk.data for chunk in self.raw_chunks)
        else:
            raw_text = "".join(chunk.data for chunk in self.raw_chunks if chunk.seq > since_seq)
        return raw_text, self._next_seq

    def get_new_output(self, since_seq: int = 0) -> tuple[str, List[str], int]:
        """Return both raw streaming text and structured audit lines since since_seq."""
        raw_text, _ = self.get_new_raw(since_seq)
        lines, next_seq = self.get_new_lines(since_seq)
        return raw_text, lines, next_seq

    def get_raw_text(self) -> str:
        """Return full raw text across all stored chunks, or newline-joined lines if chunks empty."""
        if self.raw_chunks:
            return "".join(chunk.data for chunk in self.raw_chunks)
        return "\n".join(self.get_lines())

    def clear(self) -> None:
        self.lines.clear()
        self.raw_chunks.clear()
        self.total_bytes = 0
        self.total_raw_bytes = 0
        self._next_seq = 0

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
