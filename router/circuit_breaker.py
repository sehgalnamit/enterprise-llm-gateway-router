"""A clean, dependency-free circuit breaker implementation.

States: CLOSED -> OPEN -> HALF_OPEN -> (CLOSED | OPEN).

* CLOSED: requests flow normally. Failures are counted in a sliding window;
  once the failure count reaches `failure_threshold` within `window_seconds`,
  the breaker trips to OPEN.
* OPEN: requests are rejected immediately (`allow_request()` returns False)
  until `recovery_timeout` has elapsed, at which point the breaker moves to
  HALF_OPEN to probe recovery.
* HALF_OPEN: a limited number of probe requests (`half_open_max_calls`) are
  allowed through. Any failure immediately reopens the breaker with an
  exponentially-increased recovery timeout (capped at `max_recovery_timeout`).
  Enough consecutive successes close the breaker and reset backoff.

HTTP 429 (rate-limit) failures are tracked identically to other failures but
are recorded via `record_failure(status_code=429)` so callers/logs can
distinguish "provider is overloaded" from "provider is broken".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock


class CircuitState(str, Enum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    window_seconds: float = 60.0
    recovery_timeout: float = 5.0
    max_recovery_timeout: float = 60.0
    backoff_multiplier: float = 2.0
    half_open_max_calls: int = 1
    half_open_success_threshold: int = 1


@dataclass
class _FailureEvent:
    timestamp: float
    status_code: int | None = None


@dataclass
class CircuitBreakerSnapshot:
    name: str
    state: CircuitState
    failure_count: int
    current_recovery_timeout: float
    opened_at: float | None
    half_open_calls_in_flight: int = 0


class CircuitBreaker:
    """Per-provider circuit breaker with exponential backoff + half-open
    recovery probing."""

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None) -> None:
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._lock = Lock()
        self._state = CircuitState.closed
        self._failures: list[_FailureEvent] = []
        self._opened_at: float | None = None
        self._current_recovery_timeout = self.config.recovery_timeout
        self._half_open_calls_in_flight = 0
        self._half_open_successes = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    def _maybe_transition_to_half_open(self) -> None:
        """Caller must hold `self._lock`."""
        if self._state is CircuitState.open and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._current_recovery_timeout:
                self._state = CircuitState.half_open
                self._half_open_calls_in_flight = 0
                self._half_open_successes = 0

    def allow_request(self) -> bool:
        """Should the router attempt a call through this provider right now?"""
        with self._lock:
            self._maybe_transition_to_half_open()
            if self._state is CircuitState.closed:
                return True
            if self._state is CircuitState.open:
                return False
            # half_open: allow a bounded number of probes through
            if self._half_open_calls_in_flight < self.config.half_open_max_calls:
                self._half_open_calls_in_flight += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state is CircuitState.half_open:
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.half_open_success_threshold:
                    self._close()
            else:
                # A success in the closed state decays the failure window.
                self._failures.clear()

    def record_failure(self, status_code: int | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            if self._state is CircuitState.half_open:
                # Any failure during a recovery probe reopens immediately
                # with increased backoff.
                self._open(now, escalate_backoff=True)
                return

            self._failures.append(_FailureEvent(timestamp=now, status_code=status_code))
            window_start = now - self.config.window_seconds
            self._failures = [f for f in self._failures if f.timestamp >= window_start]
            if len(self._failures) >= self.config.failure_threshold:
                self._open(now, escalate_backoff=False)

    def _open(self, now: float, *, escalate_backoff: bool) -> None:
        if escalate_backoff and self._state is CircuitState.half_open:
            self._current_recovery_timeout = min(
                self._current_recovery_timeout * self.config.backoff_multiplier,
                self.config.max_recovery_timeout,
            )
        self._state = CircuitState.open
        self._opened_at = now
        self._half_open_calls_in_flight = 0
        self._half_open_successes = 0

    def _close(self) -> None:
        self._state = CircuitState.closed
        self._failures.clear()
        self._opened_at = None
        self._current_recovery_timeout = self.config.recovery_timeout
        self._half_open_calls_in_flight = 0
        self._half_open_successes = 0

    def reset(self) -> None:
        """Test/demo helper: force the breaker back to a clean CLOSED state."""
        with self._lock:
            self._close()

    def snapshot(self) -> CircuitBreakerSnapshot:
        with self._lock:
            self._maybe_transition_to_half_open()
            return CircuitBreakerSnapshot(
                name=self.name,
                state=self._state,
                failure_count=len(self._failures),
                current_recovery_timeout=self._current_recovery_timeout,
                opened_at=self._opened_at,
                half_open_calls_in_flight=self._half_open_calls_in_flight,
            )
