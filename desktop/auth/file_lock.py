"""Reentrant advisory locks for Desktop files, shared by threads and processes.

The sidecar file is permanent: deleting a lockfile would let two processes lock different inodes.
Windows locks its first byte with msvcrt; POSIX uses flock. No payload is written to this file.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")

SHARING_RETRY_ATTEMPTS = 8
SHARING_RETRY_DELAY = 0.02


class FileLockTimeoutError(TimeoutError):
    """Another cooperating process did not release the file lock in time."""


def retry_sharing_violation(operation: Callable[[], T], attempts: int = SHARING_RETRY_ATTEMPTS,
                            delay: float = SHARING_RETRY_DELAY) -> T:
    """Windows: a reader without FILE_SHARE_DELETE makes another process's os.replace fail, and vice versa, with a
    short-lived PermissionError. Retry a few times (about half a second at most); any other error, the last failure and
    every failure outside Windows propagate unchanged."""
    for attempt in range(attempts):
        try:
            return operation()
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))
    raise AssertionError("unreachable")


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held = threading.local()


@contextmanager
def interprocess_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Lock a stable sidecar path; nested calls from the same thread reuse the OS lock."""
    target = Path(path).resolve()
    key = os.path.normcase(str(target))
    with _registry_guard:
        local_lock = _thread_locks.setdefault(key, threading.RLock())
    if not local_lock.acquire(timeout=timeout):
        raise FileLockTimeoutError("Tempo esgotado aguardando acesso exclusivo ao arquivo.")
    try:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if key in held:
            yield
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        # Windows can lock a range beyond EOF. Initialize only after locking: writing before acquiring can race
        # with the first process that creates/locks the sidecar and would fail with a sharing violation.
        with target.open("a+b") as stream:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    acquire()
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise FileLockTimeoutError("Tempo esgotado aguardando acesso exclusivo ao arquivo.") from None
                    time.sleep(0.02)
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                stream.seek(0)
                release()
    finally:
        local_lock.release()
