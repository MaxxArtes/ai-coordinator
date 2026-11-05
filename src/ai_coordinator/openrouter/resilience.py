"""Operational resilience, rate limiting, and circuit breaker for OpenRouter (Phase 4C.1).

Enforces:
1. Standard free account rate limits (20 req/minute, 50 req/day).
2. Atomic cross-process reservation backed by SQLite transactions (BEGIN IMMEDIATE).
3. Fail-closed error handling: I/O or persistence failures raise QuotaPersistenceError.
4. Strict enforcement of HTTP 429 Retry-After headers across all attempts.
5. Three-state Circuit Breaker (CLOSED, OPEN, HALF_OPEN) preventing cascade failures.
6. Exponential backoff with decorrelated jitter for transient HTTP 429 and 503 errors.
"""
from __future__ import annotations

import collections
from enum import Enum
import logging
import math
import os
import random
import sqlite3
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimitExceededError(Exception):
    """Raised when rate limit is exceeded and immediate execution cannot proceed."""
    pass


class CircuitOpenError(Exception):
    """Raised when circuit breaker is in OPEN state, rejecting outbound calls."""
    pass


class QuotaPersistenceError(Exception):
    """Raised when atomic quota reservation persistence fails (fail-closed)."""
    pass


class SQLiteQuotaStore:
    """Transactional, cross-process quota reservation store backed by SQLite."""

    def __init__(self, db_path: str, timeout_sec: float = 10.0) -> None:
        self.db_path = os.path.abspath(db_path)
        self.timeout_sec = timeout_sec
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout_sec)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.isolation_level = None  # Manual transaction management
        return conn

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        try:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quota_reservations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id TEXT NOT NULL,
                        timestamp REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_quota_account_ts ON quota_reservations(account_id, timestamp)"
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS quota_blocks (
                        account_id TEXT PRIMARY KEY,
                        blocked_until REAL NOT NULL
                    )
                    """
                )
            finally:
                conn.close()
        except Exception as exc:
            raise QuotaPersistenceError(f"Falha ao inicializar banco de quotas SQLite: {exc}") from exc

    def record_retry_after(self, account_id: str, retry_after_sec: float, now: Optional[float] = None) -> None:
        t = now if now is not None else time.time()
        blocked_until = t + retry_after_sec
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO quota_blocks (account_id, blocked_until)
                VALUES (?, ?)
                ON CONFLICT(account_id) DO UPDATE SET blocked_until = MAX(quota_blocks.blocked_until, excluded.blocked_until)
                """,
                (account_id, blocked_until),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise QuotaPersistenceError(f"Falha ao registrar Retry-After no banco de quotas: {exc}") from exc
        finally:
            conn.close()

    def acquire_atomic(
        self,
        account_id: str,
        max_per_minute: int,
        max_per_day: int,
        now: Optional[float] = None,
    ) -> bool:
        """Atomically reserve a quota slot using BEGIN IMMEDIATE table-level locking.

        Returns True if granted, False if rate limited.
        Raises QuotaPersistenceError if transaction fails.
        """
        t = now if now is not None else time.time()
        conn = self._get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Check if account is in Retry-After cooling period
            cur = conn.execute("SELECT blocked_until FROM quota_blocks WHERE account_id = ?", (account_id,))
            row = cur.fetchone()
            if row and float(row[0]) > t:
                conn.execute("ROLLBACK")
                return False

            # Purge reservations older than 24h
            conn.execute(
                "DELETE FROM quota_reservations WHERE account_id = ? AND timestamp < ?",
                (account_id, t - 86400.0),
            )

            # Check counts
            cur = conn.execute(
                "SELECT COUNT(*) FROM quota_reservations WHERE account_id = ? AND timestamp >= ?",
                (account_id, t - 60.0),
            )
            min_count = cur.fetchone()[0]
            if min_count >= max_per_minute:
                conn.execute("ROLLBACK")
                return False

            cur = conn.execute(
                "SELECT COUNT(*) FROM quota_reservations WHERE account_id = ? AND timestamp >= ?",
                (account_id, t - 86400.0),
            )
            day_count = cur.fetchone()[0]
            if day_count >= max_per_day:
                conn.execute("ROLLBACK")
                return False

            # Insert reservation
            conn.execute(
                "INSERT INTO quota_reservations (account_id, timestamp) VALUES (?, ?)",
                (account_id, t),
            )
            conn.execute("COMMIT")
            return True
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise QuotaPersistenceError(f"Falha atômica ao reservar quota SQLite: {exc}") from exc
        finally:
            conn.close()

    def get_counts(self, account_id: str, now: Optional[float] = None) -> Tuple[int, int]:
        t = now if now is not None else time.time()
        conn = self._get_connection()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM quota_reservations WHERE account_id = ? AND timestamp >= ?",
                (account_id, t - 60.0),
            )
            min_count = cur.fetchone()[0]
            cur = conn.execute(
                "SELECT COUNT(*) FROM quota_reservations WHERE account_id = ? AND timestamp >= ?",
                (account_id, t - 86400.0),
            )
            day_count = cur.fetchone()[0]
            return min_count, day_count
        except Exception as exc:
            raise QuotaPersistenceError(f"Falha ao consultar contagens de quota: {exc}") from exc
        finally:
            conn.close()


class SlidingWindowRateLimiter:
    """Sliding-window rate limiter with atomic SQLite or in-memory backing."""

    def __init__(
        self,
        max_per_minute: int = 20,
        max_per_day: int = 50,
        sqlite_path: Optional[str] = None,
        account_id: str = "openrouter_free_default",
    ) -> None:
        self.max_per_minute = max_per_minute
        self.max_per_day = max_per_day
        self.sqlite_path = sqlite_path
        self.account_id = account_id

        self._store: Optional[SQLiteQuotaStore] = None
        if self.sqlite_path:
            self._store = SQLiteQuotaStore(self.sqlite_path)

        # In-memory windows for single-process operation
        self._minute_window: Deque[float] = collections.deque()
        self._day_window: Deque[float] = collections.deque()
        self._blocked_until: float = 0.0

    def _purge_stale(self, now: float) -> None:
        minute_cutoff = now - 60.0
        while self._minute_window and self._minute_window[0] < minute_cutoff:
            self._minute_window.popleft()

        day_cutoff = now - 86400.0
        while self._day_window and self._day_window[0] < day_cutoff:
            self._day_window.popleft()

    def record_retry_after(self, retry_after_sec: float, now: Optional[float] = None) -> None:
        t = now if now is not None else time.time()
        if self._store:
            self._store.record_retry_after(self.account_id, retry_after_sec, now=t)
        if retry_after_sec > 0:
            self._blocked_until = max(self._blocked_until, t + retry_after_sec)

    def can_acquire(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        if self._store:
            min_c, day_c = self._store.get_counts(self.account_id, now=t)
            return min_c < self.max_per_minute and day_c < self.max_per_day

        if t < self._blocked_until:
            return False
        self._purge_stale(t)
        return len(self._minute_window) < self.max_per_minute and len(self._day_window) < self.max_per_day

    def acquire(self, now: Optional[float] = None) -> bool:
        """Atomically reserve a quota slot. Raises QuotaPersistenceError on persistence failure."""
        t = now if now is not None else time.time()
        if self._store:
            return self._store.acquire_atomic(
                account_id=self.account_id,
                max_per_minute=self.max_per_minute,
                max_per_day=self.max_per_day,
                now=t,
            )

        if t < self._blocked_until:
            return False
        self._purge_stale(t)
        if len(self._minute_window) >= self.max_per_minute or len(self._day_window) >= self.max_per_day:
            return False
        self._minute_window.append(t)
        self._day_window.append(t)
        return True

    def time_until_available(self, now: Optional[float] = None) -> float:
        t = now if now is not None else time.time()
        wait_for_retry = max(0.0, self._blocked_until - t)

        if self._store:
            min_c, day_c = self._store.get_counts(self.account_id, now=t)
            wait_for_rate = 1.0 if (min_c >= self.max_per_minute or day_c >= self.max_per_day) else 0.0
            return max(wait_for_retry, wait_for_rate)

        self._purge_stale(t)
        wait_for_rate = 0.0
        if len(self._day_window) >= self.max_per_day:
            oldest_day = self._day_window[0]
            wait_for_rate = max(wait_for_rate, (oldest_day + 86400.0) - t)
        if len(self._minute_window) >= self.max_per_minute:
            oldest_min = self._minute_window[0]
            wait_for_rate = max(wait_for_rate, (oldest_min + 60.0) - t)

        return max(wait_for_retry, wait_for_rate)

    @property
    def requests_in_current_minute(self) -> int:
        if self._store:
            min_c, _ = self._store.get_counts(self.account_id)
            return min_c
        self._purge_stale(time.time())
        return len(self._minute_window)

    @property
    def requests_in_current_day(self) -> int:
        if self._store:
            _, day_c = self._store.get_counts(self.account_id)
            return day_c
        self._purge_stale(time.time())
        return len(self._day_window)


class CircuitState(str, Enum):
    """Circuit breaker operational state."""
    CLOSED = "CLOSED"      # Normal operation
    OPEN = "OPEN"          # Tripped, rejecting all requests
    HALF_OPEN = "HALF_OPEN"# Testing recovery with limited probes


class CircuitBreaker:
    """Protects coordinator infrastructure from failing remote endpoints."""

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_sec: float = 30.0,
        half_open_success_threshold: int = 2,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_sec = recovery_timeout_sec
        self.half_open_success_threshold = half_open_success_threshold

        self.state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_state_change = time.time()

    def allow_request(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if t - self._last_state_change >= self.recovery_timeout_sec:
                self.state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0
                self._last_state_change = t
                return True
            return False
        if self.state == CircuitState.HALF_OPEN:
            return True
        return False

    def record_success(self, now: Optional[float] = None) -> None:
        t = now if now is not None else time.time()
        self._consecutive_failures = 0
        if self.state == CircuitState.HALF_OPEN:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.half_open_success_threshold:
                self.state = CircuitState.CLOSED
                self._last_state_change = t
        elif self.state == CircuitState.CLOSED:
            self._consecutive_successes += 1

    def record_failure(self, now: Optional[float] = None) -> None:
        t = now if now is not None else time.time()
        self._consecutive_failures += 1
        if self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            if self._consecutive_failures >= self.failure_threshold or self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self._last_state_change = t


class RetryPolicy:
    """Decorrelated exponential backoff with bounded jitter and Retry-After awareness."""

    def __init__(
        self,
        max_attempts: int = 3,
        base_delay_sec: float = 0.5,
        max_delay_sec: float = 10.0,
        retryable_status_codes: Optional[Tuple[int, ...]] = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.base_delay_sec = base_delay_sec
        self.max_delay_sec = max_delay_sec
        self.retryable_status_codes = retryable_status_codes or (429, 502, 503, 504)

    def calculate_delay(self, attempt: int, retry_after_sec: Optional[float] = None) -> float:
        if retry_after_sec is not None and retry_after_sec > 0:
            return min(self.max_delay_sec, max(self.base_delay_sec, retry_after_sec))

        factor = 2 ** max(0, attempt - 1)
        delay = self.base_delay_sec * factor
        jitter = random.uniform(0.0, delay * 0.5)
        return min(self.max_delay_sec, delay + jitter)

    def is_retryable_status(self, status_code: int) -> bool:
        return status_code in self.retryable_status_codes
