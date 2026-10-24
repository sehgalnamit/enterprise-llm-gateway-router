"""Process-wide fault-injection registry used for the failover demo.

This lets `scripts/demo_failover.py` and the `/admin/fault-inject` endpoint
simulate a provider outage or HTTP 429 rate-limit storm without touching any
real upstream, so the circuit-breaker + failover behavior can be demonstrated
and tested fully offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Literal

FaultMode = Literal["none", "rate_limited", "outage"]


@dataclass
class _FaultState:
    mode: FaultMode = "none"


class FaultInjector:
    """Thread-safe in-memory fault toggle, keyed by provider name."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._state: dict[str, _FaultState] = {}

    def set_fault(self, provider: str, mode: FaultMode) -> None:
        with self._lock:
            self._state[provider] = _FaultState(mode=mode)

    def clear(self, provider: str | None = None) -> None:
        with self._lock:
            if provider is None:
                self._state.clear()
            else:
                self._state.pop(provider, None)

    def current_fault(self, provider: str) -> FaultMode:
        with self._lock:
            state = self._state.get(provider)
            return state.mode if state else "none"

    def snapshot(self) -> dict[str, FaultMode]:
        with self._lock:
            return {name: state.mode for name, state in self._state.items()}


# Process-wide singleton. Tests/scripts import this directly; the FastAPI app
# exposes it via the admin router for demo purposes.
fault_injector = FaultInjector()
