"""Operational resilience, rate limiting, and circuit breaker for OpenRouter (Phase 4C.1).

Enforces:
1. Standard free account rate limits (20 req/minute, 50 req/day).
2. Sliding window token bucket rate limiting with optional shared persistence.
3. Strict enforcement of HTTP 429 Retry-After headers across all attempts.
4. Three-state Circuit Breaker (CLOSED, OPEN, HALF_OPEN) preventing cascade failures.
5. Exponential backoff with decorrelated jitter for transient HTTP 429 and 503 errors.
"""
from __future__ import annotations

import collections
from enum import Enum
import json
import logging
import math
import os
import random
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


class SlidingWindowRateLimiter:
    """Sliding-window rate limiter tracking requests per minute and per day with persistence."""

    def __init__(
        self,
        max_per_minute: int = 20,
        max_per_day: int = 50,
        persistence_path: Optional[str] = None,
    ) -> None:
        self.max_per_minute = max_per_minute
        self.max_per_day = max_per_day
        self.persistence_path = persistence_path
        self._minute_window: Deque[float] = collections.deque()
        self._day_window: Deque[float] = collections.deque()
        self._blocked_until: float = 0.0

        if self.persistence_path:
            self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        """Load recorded timestamps from shared persistence file if available."""
        if not self.persistence_path or not os.path.exists(self.persistence_path):
            return
        try:
            with open(self.persistence_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            t_now = time.time()
            day_cutoff = t_now - 86400.0
            minute_cutoff = t_now - 60.0

            persisted_timestamps = [float(x) for x in data.get("timestamps", []) if float(x) >= day_cutoff]
            self._day_window = collections.deque(sorted(persisted_timestamps))
            self._minute_window = collections.deque(
                [ts for ts in self._day_window if ts >= minute_cutoff]
            )
            self._blocked_until = float(data.get("blocked_until", 0.0))
        except Exception as exc:
            logger.debug("Falha ao carregar estado do limitador em %s: %s", self.persistence_path, exc)

    def _save_persisted_state(self) -> None:
        """Sync timestamps to shared persistence file."""
        if not self.persistence_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.persistence_path)), exist_ok=True)
            temp_path = f"{self.persistence_path}.tmp.{os.getpid()}"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamps": list(self._day_window),
                    "blocked_until": self._blocked_until,
                    "updated_at": time.time(),
                }, f)
            os.replace(temp_path, self.persistence_path)
        except Exception as exc:
            logger.debug("Falha ao salvar estado do limitador em %s: %s", self.persistence_path, exc)

    def _purge_stale(self, now: float) -> None:
        """Purge timestamps outside the active windows."""
        minute_cutoff = now - 60.0
        while self._minute_window and self._minute_window[0] < minute_cutoff:
            self._minute_window.popleft()

        day_cutoff = now - 86400.0
        while self._day_window and self._day_window[0] < day_cutoff:
            self._day_window.popleft()

    def record_retry_after(self, retry_after_sec: float, now: Optional[float] = None) -> None:
        """Record a Retry-After directive received from remote endpoint."""
        t = now if now is not None else time.time()
        if retry_after_sec > 0:
            self._blocked_until = max(self._blocked_until, t + retry_after_sec)
            self._save_persisted_state()

    def can_acquire(self, now: Optional[float] = None) -> bool:
        """Check if a new request can be dispatched right now without exceeding limits."""
        t = now if now is not None else time.time()
        if t < self._blocked_until:
            return False
        self._purge_stale(t)
        return len(self._minute_window) < self.max_per_minute and len(self._day_window) < self.max_per_day

    def acquire(self, now: Optional[float] = None) -> bool:
        """Attempt to acquire a request token. Returns True if granted, False if rate limited."""
        t = now if now is not None else time.time()
        if t < self._blocked_until:
            return False
        self._purge_stale(t)
        if len(self._minute_window) >= self.max_per_minute or len(self._day_window) >= self.max_per_day:
            return False
        self._minute_window.append(t)
        self._day_window.append(t)
        self._save_persisted_state()
        return True

    def time_until_available(self, now: Optional[float] = None) -> float:
        """Return estimated seconds until next token is available."""
        t = now if now is not None else time.time()
        wait_for_retry = max(0.0, self._blocked_until - t)

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
        self._purge_stale(time.time())
        return len(self._minute_window)

    @property
    def requests_in_current_day(self) -> int:
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
        """Determine if an outbound request should be permitted."""
        t = now if now is not None else time.time()
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            # Check if recovery timeout has elapsed
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
        """Record successful transaction."""
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
        """Record failed transaction (e.g. HTTP 429, 503, connection drop)."""
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
        """Calculate backoff delay respecting Retry-After and decorrelated jitter."""
        if retry_after_sec is not None and retry_after_sec > 0:
            return min(self.max_delay_sec, max(self.base_delay_sec, retry_after_sec))

        factor = 2 ** max(0, attempt - 1)
        delay = self.base_delay_sec * factor
        # Add 0% to 50% random jitter
        jitter = random.uniform(0.0, delay * 0.5)
        return min(self.max_delay_sec, delay + jitter)

    def is_retryable_status(self, status_code: int) -> bool:
        """Check if an HTTP status code qualifies for retry."""
        return status_code in self.retryable_status_codes
