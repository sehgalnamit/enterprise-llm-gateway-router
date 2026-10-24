"""Demo (c): fault injection + circuit breaker + seamless mid-session
failover.

Simulates the primary provider ("mock-claude") going into an outage
mid-conversation. Shows:
  1. Turn 1 served successfully by the primary.
  2. The primary failing (outage injected) for enough consecutive calls to
     trip its circuit breaker OPEN.
  3. The router automatically failing over to the secondary provider
     ("mock-gpt") for all subsequent turns in the SAME session.
  4. The full provider-agnostic message history remaining intact across the
     failover -- zero dropped context.

Run: python scripts/demo_failover.py
Fully offline -- uses MockProvider personas + fakeredis.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cache.embedder import HashEmbedder  # noqa: E402
from cache.semantic_cache import SemanticCache  # noqa: E402
from providers.fault_injection import fault_injector  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from router.circuit_breaker import CircuitBreakerConfig  # noqa: E402
from router.gateway_router import GatewayRouter, default_failover_chains  # noqa: E402

SESSION_ID = "demo-failover-session"


def build_router() -> GatewayRouter:
    import fakeredis

    providers = {
        "mock-claude": MockProvider(persona="mock-claude", simulate_latency=False),
        "mock-gpt": MockProvider(persona="mock-gpt", simulate_latency=False),
        "local-qwen": MockProvider(persona="local-qwen", simulate_latency=False),
    }
    cache = SemanticCache(fakeredis.FakeRedis(decode_responses=True), HashEmbedder())
    breaker_config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout=30.0)
    return GatewayRouter(
        providers,
        failover_chains=default_failover_chains(tuple(providers.keys())),
        breaker_config=breaker_config,
        cache=cache,
    )


def print_turn(label: str, result) -> None:
    print(f"[{label}] served_by={result.response.provider!r} content={result.response.content!r}")
    print(f"         attempts={[(a.provider, a.outcome) for a in result.attempts]}")


def main() -> None:
    fault_injector.clear()
    router = build_router()

    print("=== Step 1: normal conversation on primary provider 'mock-claude' ===")
    r1 = router.route_with_session(
        SESSION_ID, "Hello, who are you?", model="mock-claude", use_cache=False
    )
    print_turn("turn 1", r1)
    assert r1.response.provider == "mock-claude"

    print("\n=== Step 2: injecting an outage on 'mock-claude' (simulated downtime) ===")
    fault_injector.set_fault("mock-claude", "outage")

    print("Sending more turns -- breaker trips after failure_threshold=2, then router")
    print("fails over to 'mock-gpt' for the SAME session with full history intact.\n")
    for i, text in enumerate(["Tell me a fact.", "And another one?", "One more please."], start=2):
        result = router.route_with_session(SESSION_ID, text, model="mock-claude", use_cache=False)
        print_turn(f"turn {i}", result)

    breaker_state = router.breakers["mock-claude"].state.value
    print(f"\nCircuit breaker state for 'mock-claude': {breaker_state.upper()}")
    assert breaker_state == "open"

    print("\n=== Step 3: full session history, provider-agnostic, zero dropped turns ===")
    history = router.sessions.history(SESSION_ID)
    record = router.sessions.get_or_create(SESSION_ID)
    print(json.dumps([m.model_dump() for m in history], indent=2))
    print(f"\nTurns served by (in order): {record.served_by}")
    assert len(history) == 8, "expected 4 user + 4 assistant turns, nothing dropped"
    assert record.served_by[0] == "mock-claude"
    assert all(p == "mock-gpt" for p in record.served_by[1:])

    print(
        "\nDemo complete: the session continued seamlessly through the primary "
        "outage with zero context loss, proving provider-agnostic session "
        "state + automatic circuit-breaker failover."
    )


if __name__ == "__main__":
    main()
