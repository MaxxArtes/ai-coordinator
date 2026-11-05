"""Operational resilience, rate limiting, and circuit breaker for OpenRouter (Phase 4C.1).

Enforces:
1. Standard free account rate limits (20 req/minute, 50 req/day).
2. Sliding window token bucket rate limiting.
3. Three-state Circuit Breaker (CLOSED, OPEN, HALF_OPEN) preventing cascade failures.
4. Exponential backoff with decorrelated jitter for transient HTTP 429 and 503 errors.
"""
from __future__ import annotations

import collections
from enum import Enum
import logging
import math
import random
import time
from typing import Callable, Deque, Dict, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RateLimitExceededError(Exception):
    """Raised when rate limit is exceeded and immediate execution cannot proceed."""
    pass


class CircuitOpenError(Exception):
    """Raised when circuit breaker is in OPEN state, rejecting outbound calls."""
    pass


class SlidingWindowRateLimiter:
    """Sliding-window rate limiter tracking requests per minute and per day."""

    def __init__(
        self,
        max_per_minute: int = 20,
        max_per_day: int = 50,
    ) -> None:
        self.max_per_minute = max_per_minute
        self.max_per_day = max_per_day
        self._minute_window: Deque[float] = collections.deque()
        self._day_window: Deque[float] = collections.deque()

    def _purge_stale(self, now: float) -> None:
        """Purge timestamps outside the active windows."""
        minute_cutoff = now - 60.0
        while self._minute_window and self._minute_window[0] < minute_cutoff:
            self._minute_window.popleft()

        day_cutoff = now - 86400.0
        while self._day_window and self._day_window[0] < day_cutoff:
            self._day_window.popleft()

    def can_acquire(self, now: Optional[float] = None) -> bool:
        """Check if a new request can be dispatched right now without exceeding limits."""
        t = now if now is not None else time.time()
        self._purge_stale(t)
        return len(self._minute_window) < self.max_per_minute and len(self._day_window) < self.max_per_day

    def acquire(self, now: Optional[float] = None) -> bool:
        """Attempt to acquire a request token. Returns True if granted, False if rate limited."""
        t = now if now is not None else time.time()
        self._purge_stale(t)
        if len(self._minute_window) >= self.max_per_minute or len(self._day_window) >= self.max_per_day:
            return False
        self._minute_window.append(t)
        self._day_window.append(t)
        return True

    def time_until_available(self, now: Optional[float] = None) -> float:
        """Return estimated seconds until next token is available."""
        t = now if now is not None else time.time()
        self._purge_stale(t)
        if len(self._day_window) >= self.max_per_day:
            oldest_day = self._day_window[0]
            return max(0.0, (oldest_day + 86400.0) - t)
        if len(self._minute_window) >= self.max_per_minute:
            oldest_min = self._minute_window[0]
            return max(0.0, (oldest_min + 60.0) - t)
        return 0.0

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
    """Decorrelated exponential backoff with bounded jitter."""

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

    def calculate_delay(self, attempt: int) -> float:
        """Calculate decorrelated jittered backoff delay."""
        factor = 2 ** max(0, attempt - 1)
        delay = self.base_delay_sec * factor
        # Add 0% to 50% random jitter
        jitter = random.uniform(0.0, delay * 0.5)
        return min(self.max_delay_sec, delay + jitter)

    def is_retryable_status(self, status_code: int) -> bool:
        """Check if an HTTP status code qualifies for retry."""
        return status_code in self.retryable_status_codes
