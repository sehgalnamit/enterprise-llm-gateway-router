"""Circuit breaker state-machine tests."""

from __future__ import annotations

import time

from router.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitState


def _breaker(**overrides) -> CircuitBreaker:
    config = CircuitBreakerConfig(
        failure_threshold=3,
        window_seconds=60.0,
        recovery_timeout=0.05,
        max_recovery_timeout=0.4,
        backoff_multiplier=2.0,
        half_open_max_calls=1,
        half_open_success_threshold=1,
        **overrides,
    )
    return CircuitBreaker("test-provider", config)


def test_breaker_starts_closed_and_allows_requests():
    breaker = _breaker()
    assert breaker.state == CircuitState.closed
    assert breaker.allow_request() is True


def test_breaker_opens_after_threshold_failures():
    breaker = _breaker()
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitState.open
    assert breaker.allow_request() is False


def test_breaker_success_before_threshold_resets_failure_count():
    breaker = _breaker()
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    # Only 2 consecutive failures since the reset -- still closed.
    assert breaker.state == CircuitState.closed


def test_breaker_rate_limit_failures_count_toward_opening():
    breaker = _breaker()
    breaker.record_failure(status_code=429)
    breaker.record_failure(status_code=429)
    breaker.record_failure(status_code=429)
    assert breaker.state == CircuitState.open


def test_breaker_transitions_to_half_open_after_recovery_timeout():
    breaker = _breaker()
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitState.open
    time.sleep(0.08)
    assert breaker.state == CircuitState.half_open


def test_breaker_half_open_success_closes_breaker():
    breaker = _breaker()
    for _ in range(3):
        breaker.record_failure()
    time.sleep(0.08)
    assert breaker.allow_request() is True  # half-open probe allowed through
    breaker.record_success()
    assert breaker.state == CircuitState.closed


def test_breaker_half_open_failure_reopens_with_increased_backoff():
    breaker = _breaker()
    for _ in range(3):
        breaker.record_failure()
    first_timeout = breaker.snapshot().current_recovery_timeout
    time.sleep(0.08)
    assert breaker.allow_request() is True
    breaker.record_failure()  # fails the probe -> reopen, escalate backoff
    snap = breaker.snapshot()
    assert snap.state == CircuitState.open
    assert snap.current_recovery_timeout > first_timeout


def test_breaker_backoff_is_capped_at_max_recovery_timeout():
    breaker = _breaker()
    for _ in range(3):
        breaker.record_failure()
    for _ in range(6):
        time.sleep(breaker.snapshot().current_recovery_timeout + 0.01)
        breaker.allow_request()
        breaker.record_failure()
    assert breaker.snapshot().current_recovery_timeout <= 0.4


def test_breaker_half_open_limits_concurrent_probe_calls():
    breaker = _breaker()
    for _ in range(3):
        breaker.record_failure()
    time.sleep(0.08)
    assert breaker.allow_request() is True
    # A second concurrent probe should be rejected until the first resolves.
    assert breaker.allow_request() is False


def test_breaker_reset_forces_closed_state():
    breaker = _breaker()
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitState.open
    breaker.reset()
    assert breaker.state == CircuitState.closed
    assert breaker.allow_request() is True
