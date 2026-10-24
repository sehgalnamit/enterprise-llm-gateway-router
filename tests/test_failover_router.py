"""Failover routing tests: simulated outage/rate-limit trips the primary
provider's circuit breaker and the router seamlessly fails over to the
secondary, carrying session history across the switch with zero context
loss (demo requirement c)."""

from __future__ import annotations

from cache.embedder import HashEmbedder
from cache.semantic_cache import SemanticCache
from providers.fault_injection import fault_injector
from router.circuit_breaker import CircuitBreakerConfig
from router.gateway_router import (
    AllProvidersUnavailableError,
    GatewayRouter,
    default_failover_chains,
)
from schemas.contracts import ChatMessage, ChatRequest, MessageRole


def _router(mock_providers, fake_redis_client, **breaker_overrides):
    config = CircuitBreakerConfig(failure_threshold=2, window_seconds=60.0, **breaker_overrides)
    cache = SemanticCache(fake_redis_client, HashEmbedder())
    return GatewayRouter(
        mock_providers,
        failover_chains=default_failover_chains(("mock-claude", "mock-gpt", "local-qwen")),
        breaker_config=config,
        cache=cache,
    )


def test_single_request_succeeds_against_primary(mock_providers, fake_redis_client):
    router = _router(mock_providers, fake_redis_client)
    request = ChatRequest(
        messages=[ChatMessage(role=MessageRole.user, content="hello")], model="mock-claude"
    )
    result = router.route(request, use_cache=False)
    assert result.response.provider == "mock-claude"
    assert result.attempts[-1].outcome == "success"


def test_router_fails_over_after_primary_breaker_opens(mock_providers, fake_redis_client):
    router = _router(mock_providers, fake_redis_client)
    fault_injector.set_fault("mock-claude", "outage")

    # First two calls to mock-claude fail and trip its breaker (threshold=2),
    # but each call still succeeds end-to-end via failover to mock-gpt.
    for _ in range(2):
        request = ChatRequest(
            messages=[ChatMessage(role=MessageRole.user, content="hi")], model="mock-claude"
        )
        result = router.route(request, use_cache=False)
        assert result.response.provider == "mock-gpt"

    assert router.breakers["mock-claude"].state.value == "open"

    # Subsequent requests skip straight past the open breaker.
    request = ChatRequest(
        messages=[ChatMessage(role=MessageRole.user, content="hi again")], model="mock-claude"
    )
    result = router.route(request, use_cache=False)
    assert result.response.provider == "mock-gpt"
    assert any(a.outcome == "breaker_open" for a in result.attempts)


def test_rate_limit_triggers_failover_same_as_outage(mock_providers, fake_redis_client):
    router = _router(mock_providers, fake_redis_client)
    fault_injector.set_fault("mock-gpt", "rate_limited")
    request = ChatRequest(
        messages=[ChatMessage(role=MessageRole.user, content="hi")], model="mock-gpt"
    )
    result = router.route(request, use_cache=False)
    assert result.response.provider == "mock-claude"
    assert result.attempts[0].outcome == "rate_limited"


def test_all_providers_unavailable_raises_structured_error(mock_providers, fake_redis_client):
    router = _router(mock_providers, fake_redis_client)
    for persona in mock_providers:
        fault_injector.set_fault(persona, "outage")
    request = ChatRequest(
        messages=[ChatMessage(role=MessageRole.user, content="hi")], model="mock-claude"
    )
    try:
        router.route(request, use_cache=False)
        raise AssertionError("expected AllProvidersUnavailableError")
    except AllProvidersUnavailableError as exc:
        assert exc.error.error_type == "all_providers_unavailable"
        assert exc.error.request_id == request.request_id


def test_session_continuity_survives_mid_conversation_failover(mock_providers, fake_redis_client):
    """The scenario from the README failover demo: a multi-turn session
    starts on the primary provider, the primary goes down mid-conversation,
    and subsequent turns are served by the secondary with the FULL message
    history intact and provider-agnostic."""
    router = _router(mock_providers, fake_redis_client)
    session_id = "session-abc"

    r1 = router.route_with_session(session_id, "turn one", model="mock-claude", use_cache=False)
    assert r1.response.provider == "mock-claude"

    fault_injector.set_fault("mock-claude", "outage")

    r2 = router.route_with_session(session_id, "turn two", model="mock-claude", use_cache=False)
    r3 = router.route_with_session(session_id, "turn three", model="mock-claude", use_cache=False)
    assert r2.response.provider == "mock-gpt"
    assert r3.response.provider == "mock-gpt"

    history = router.sessions.history(session_id)
    user_turns = [m.content for m in history if m.role == MessageRole.user]
    assert user_turns == ["turn one", "turn two", "turn three"]
    # 3 user + 3 assistant turns, zero dropped.
    assert len(history) == 6

    record = router.sessions.get_or_create(session_id)
    assert record.served_by == ["mock-claude", "mock-gpt", "mock-gpt"]
