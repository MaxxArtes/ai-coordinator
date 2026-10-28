"""Streaming output consumption and real-time sanitization.

Prevents secret leaks across chunk and line boundaries, bounds memory usage,
and guarantees that no raw, unsanitized output is ever written to disk.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, List, Optional, Set, Tuple, Union

# Common secret regex patterns (same standard as coordinator scrub)
SECRET_PATTERNS = [
    # API Keys: OpenAI, Anthropic, Google AI, GitHub
    re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{24,}|AIza[a-z0-9_-]{30,}|gh[pousr]_[a-z0-9]{30,})\b"),
    # Headers in JSON or HTTP: authorization, set-cookie, x-api-key, password
    re.compile(r'(?i)("(?:authorization|set-cookie|cookie|x-api-key|api_key|password)"\s*:\s*)"(?:\\.|[^"\\])*"'),
    # GitLab tokens and Web2API / Gemini web keys
    re.compile(r"\b(?:glpat-[A-Za-z0-9_.-]{20,}|(?:w2a|gw)-[0-9a-f]{64})\b"),
    # Key-value pairs: senha=..., PASSWORD: ..., secret: ...
    re.compile(r"(?i)\b((?:senha|password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*['\"]?)[^\s'\"]{4,}"),
    # Connection strings: postgresql://user:pass@host
    re.compile(r"(://[^:/@\s]+:)[^@\s]+@"),
    # Bearer tokens
    re.compile(r"(?i)\bBearer\s+[a-zA-Z0-9._-]{12,}"),
]


# Candidate prefixes that can introduce a sensitive secret
SENSITIVE_PREFIX_CANDIDATE = re.compile(
    r"(?i)\b(?:Bearer\s*|sk-|AIza|gh[pousr]_|glpat-|(?:senha|password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*['\"]?|\"(?:authorization|set-cookie|cookie|x-api-key|api_key|password)\"\s*:\s*['\"]?|://[^:/@\s]+:)"
)

# Incomplete partial prefixes that might be forming at the buffer boundary
PARTIAL_PREFIX_TOKENS = (
    "bearer", "sk-", "aiza", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "glpat-",
    "password", "passwd", "senha", "secret", "token", "authorization", "x-api-key", "api_key"
)


class StreamingScrubber:
    """Incrementally scrubs text streams while retaining boundary overlap.

    Guarantees stateful incremental scrubbing: sensitive prefix candidates are
    held back until their closing boundary is reached, preventing credential leaks
    across chunks even when delimiters appear within the prefix itself (e.g. 'Bearer ').
    """

    def __init__(
        self,
        overlap_size: int = 128,
        max_word_size: int = 1024,
        exact_secrets: Optional[Set[str]] = None,
    ):
        self.overlap_size = overlap_size
        self.max_word_size = max_word_size
        self.exact_secrets = {s for s in (exact_secrets or set()) if len(s) >= 4}
        self.buffer = ""

    def scrub(self, text: str) -> str:
        """Sanitize a standalone block of text."""
        if not text:
            return ""
        # Redact exact secrets first
        for secret in self.exact_secrets:
            text = text.replace(secret, "[REDACTED]")

        # Apply regex patterns
        for pattern in SECRET_PATTERNS:
            if pattern.pattern.startswith("(?i)("):
                # Pattern with capture group for key
                text = pattern.sub(r'\1"[REDACTED]"' if '":' in pattern.pattern else r'\1[REDACTED]', text)
            elif pattern.pattern.startswith("(://"):
                text = pattern.sub(r"\1[REDACTED]@", text)
            elif "Bearer" in pattern.pattern:
                text = pattern.sub("Bearer [REDACTED]", text)
            else:
                text = pattern.sub("[REDACTED]", text)
        return text

    def feed(self, chunk: str) -> str:
        """Feed a new chunk of output and return the sanitized prefix."""
        self.buffer += chunk
        # Always run scrub on the entire buffer so any complete secrets are redacted immediately
        self.buffer = self.scrub(self.buffer)

        # Check if there are any candidate uncompleted sensitive prefixes in the buffer
        uncompleted_start = None
        for m in SENSITIVE_PREFIX_CANDIDATE.finditer(self.buffer):
            start = m.start()
            end = m.end()
            tail = self.buffer[end:]
            # Look for closing delimiter after prefix
            delims = [tail.find(c) for c in ("\n", "\r", " ", "\t", '"', "'", ";", ",")]
            found = [d for d in delims if d != -1]
            if not found:
                # No delimiter after prefix yet -> uncompleted!
                if uncompleted_start is None or start < uncompleted_start:
                    uncompleted_start = start

        # Check buffer tail for partial prefix (e.g. ends with "Bear" or "pass")
        buf_lower = self.buffer.lower()
        tail_32 = buf_lower[-32:] if len(buf_lower) >= 32 else buf_lower
        for tok in PARTIAL_PREFIX_TOKENS:
            for l in range(2, len(tok) + 1):
                sub = tok[:l]
                if tail_32.endswith(sub):
                    pos = len(self.buffer) - len(sub)
                    if pos == 0 or not self.buffer[pos - 1].isalnum():
                        if uncompleted_start is None or pos < uncompleted_start:
                            uncompleted_start = pos

        max_safe_idx = len(self.buffer)
        if uncompleted_start is not None:
            max_safe_idx = min(max_safe_idx, uncompleted_start)
        else:
            max_safe_idx = max_safe_idx - self.overlap_size

        if max_safe_idx <= 0:
            # Check if buffer has grown excessively beyond max_word_size without delimiters
            if len(self.buffer) > self.max_word_size * 2:
                to_emit = self.buffer[:self.max_word_size]
                self.buffer = self.buffer[self.max_word_size:]
                if SENSITIVE_PREFIX_CANDIDATE.search(to_emit):
                    return "[REDACTED_OVERSIZED_CREDENTIAL]"
                return self.scrub(to_emit)
            return ""

        search_zone = self.buffer[:max_safe_idx]
        delims = [search_zone.rfind(c) for c in ("\n", " ", "\t", '"', "'", ";", ",")]
        last_delim = max(delims)

        if last_delim != -1:
            to_emit = self.buffer[:last_delim + 1]
            self.buffer = self.buffer[last_delim + 1:]
            return to_emit
        elif len(search_zone) > self.max_word_size:
            to_emit = self.buffer[:self.max_word_size]
            self.buffer = self.buffer[self.max_word_size:]
            if SENSITIVE_PREFIX_CANDIDATE.search(to_emit):
                return "[REDACTED_OVERSIZED_CREDENTIAL]"
            return self.scrub(to_emit)

        return ""

    def flush(self) -> str:
        """Scrub and flush all remaining buffer content at stream EOF."""
        res = self.scrub(self.buffer)
        self.buffer = ""
        return res


@dataclass
class StreamConsumeResult:
    """Result of streaming consumption."""
    stdout: str
    stderr: str
    returncode: int
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False


class SafeStreamConsumer:
    """Consumes stdout and stderr concurrently, scrubs in real time, and bounds memory."""

    def __init__(
        self,
        stdout_path: Optional[Union[str, Path]] = None,
        stderr_path: Optional[Union[str, Path]] = None,
        max_log_bytes: int = 1_048_576,  # 1 MiB
        max_memory_preview: int = 65_536,  # 64 KiB
        exact_secrets: Optional[Set[str]] = None,
        chunk_size: int = 4096,
    ):
        self.stdout_path = Path(stdout_path) if stdout_path else None
        self.stderr_path = Path(stderr_path) if stderr_path else None
        self.max_log_bytes = max_log_bytes
        self.max_memory_preview = max_memory_preview
        self.exact_secrets = exact_secrets or set()
        self.chunk_size = chunk_size

    def consume(
        self,
        proc: Any,
        timeout_seconds: float,
        stdin_data: Optional[str] = None,
        on_poll: Optional[Callable[[], None]] = None,
    ) -> StreamConsumeResult:
        """Concurrently read and sanitize proc stdout/stderr with timeout and memory bounds."""
        stdout_scrubber = StreamingScrubber(exact_secrets=self.exact_secrets)
        stderr_scrubber = StreamingScrubber(exact_secrets=self.exact_secrets)

        stdout_acc: list[str] = []
        stderr_acc: list[str] = []
        counts = {"stdout": 0, "stderr": 0, "truncated": False}

        stdout_file = self.stdout_path.open("w", encoding="utf-8", buffering=1) if self.stdout_path else None
        stderr_file = self.stderr_path.open("w", encoding="utf-8", buffering=1) if self.stderr_path else None

        def _reader(pipe, scrubber, file_handle, acc, key):
            try:
                while True:
                    chunk = pipe.read(self.chunk_size)
                    if not chunk:
                        break
                    scrubbed = scrubber.feed(chunk)
                    if scrubbed:
                        counts[key] += len(scrubbed)
                        if counts[key] <= self.max_log_bytes:
                            if file_handle:
                                file_handle.write(scrubbed)
                            if sum(len(s) for s in acc) < self.max_memory_preview:
                                acc.append(scrubbed)
                        elif not counts["truncated"]:
                            counts["truncated"] = True
                            marker = "\n[...TRUNCATED: LOG EXCEEDED MAXIMUM BOUND FOR SAFETY...]\n"
                            if file_handle:
                                file_handle.write(marker)

                # Stream EOF: flush remaining buffer
                final = scrubber.flush()
                if final:
                    counts[key] += len(final)
                    if counts[key] <= self.max_log_bytes:
                        if file_handle:
                            file_handle.write(final)
                        if sum(len(s) for s in acc) < self.max_memory_preview:
                            acc.append(final)
            finally:
                if file_handle:
                    file_handle.flush()
                    file_handle.close()

        t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_scrubber, stdout_file, stdout_acc, "stdout"))
        t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_scrubber, stderr_file, stderr_acc, "stderr"))
        t_out.daemon = True
        t_err.daemon = True
        t_out.start()
        t_err.start()

        # Wait with global deadline encompassing stdin write, output consumption and process exit
        start = time.monotonic()
        deadline = start + timeout_seconds
        timed_out = False

        # Feed stdin concurrently bounded by the global deadline
        if stdin_data is not None and proc.stdin:
            def _write_stdin():
                try:
                    proc.stdin.write(stdin_data)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass

            t_stdin = threading.Thread(target=_write_stdin)
            t_stdin.daemon = True
            t_stdin.start()

        while time.monotonic() < deadline:
            if on_poll is not None:
                try:
                    on_poll()
                except Exception:
                    pass
            if not t_out.is_alive() and not t_err.is_alive():
                break
            time.sleep(0.05)
        else:
            timed_out = True

        if timed_out:
            from .process import safe_terminate_tree
            safe_terminate_tree(proc, timeout_seconds=2.0)
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)
            raise TimeoutError("Tempo limite atingido; nao repetir automaticamente")

        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)

        if on_poll is not None:
            try:
                on_poll()
            except Exception:
                pass

        # Wait for exit code
        if hasattr(proc, "wait"):
            proc.wait(timeout=1.0)
        returncode = proc.returncode if proc.returncode is not None else 0

        return StreamConsumeResult(
            stdout="".join(stdout_acc),
            stderr="".join(stderr_acc),
            returncode=returncode,
            stdout_bytes=counts["stdout"],
            stderr_bytes=counts["stderr"],
            truncated=counts["truncated"],
        )
