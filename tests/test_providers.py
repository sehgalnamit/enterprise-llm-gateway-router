"""Provider-adapter tests: each mock persona's distinct raw payload shape is
correctly normalized into the canonical contract, fault injection raises the
right typed exceptions, and malformed payloads are cleanly rejected or
repaired -- this is the core of demo (b), "strict contract enforcement".
"""

from __future__ import annotations

import pytest

from providers.exceptions import ProviderOutageError, ProviderRateLimitedError
from providers.fault_injection import fault_injector
from providers.mock import MockProvider
from providers.normalize_utils import from_anthropic_shape, from_openai_shape
from schemas.contracts import (
    ChatMessage,
    ChatRequest,
    FinishReason,
    MessageRole,
    SchemaContractError,
)


def _request(prompt: str = "What is the capital of France?") -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role=MessageRole.user, content=prompt)])


@pytest.mark.parametrize("persona", ["mock-claude", "mock-gpt", "local-qwen"])
def test_each_persona_produces_valid_canonical_response(persona):
    provider = MockProvider(persona=persona, simulate_latency=False)
    request = _request()
    response = provider.chat(request)
    assert response.provider == persona
    assert response.content
    assert response.finish_reason == FinishReason.stop
    expected_total = response.usage.prompt_tokens + response.usage.completion_tokens
    assert response.usage.total_tokens == expected_total
    assert response.cache_hit is False
    assert response.latency_ms >= 0


def test_personas_use_distinct_raw_shapes_but_same_canonical_contract():
    request = _request()
    responses = {
        persona: MockProvider(persona=persona, simulate_latency=False).chat(request)
        for persona in ("mock-claude", "mock-gpt", "local-qwen")
    }
    # All three normalize to the same Pydantic contract shape...
    for response in responses.values():
        assert set(response.model_dump().keys()) >= {
            "request_id",
            "model_used",
            "provider",
            "content",
            "finish_reason",
            "usage",
            "latency_ms",
            "cache_hit",
        }
    # ...despite producing content tagged with different provider personas.
    assert len({r.model_used for r in responses.values()}) == 3


def test_rate_limit_fault_injection_raises_typed_error():
    fault_injector.set_fault("mock-gpt", "rate_limited")
    provider = MockProvider(persona="mock-gpt", simulate_latency=False)
    with pytest.raises(ProviderRateLimitedError):
        provider.chat(_request())


def test_outage_fault_injection_raises_typed_error():
    fault_injector.set_fault("mock-claude", "outage")
    provider = MockProvider(persona="mock-claude", simulate_latency=False)
    with pytest.raises(ProviderOutageError):
        provider.chat(_request())


def test_malformed_openai_shape_is_rejected_with_structured_error():
    provider = MockProvider(persona="mock-gpt", simulate_latency=False)
    request = _request()
    malformed = {"object": "chat.completion", "choices": []}  # no content at all
    with pytest.raises(SchemaContractError) as exc_info:
        provider.normalize_external(malformed, request)
    err = exc_info.value
    assert err.provider == "mock-gpt"
    gw = err.to_gateway_error()
    assert gw.error_type == "schema_violation"
    assert "choices" in gw.message


def test_malformed_anthropic_shape_missing_text_block_is_rejected():
    request = _request()
    malformed = {"content": [{"type": "image", "source": "..."}], "stop_reason": "end_turn"}
    with pytest.raises(SchemaContractError):
        from_anthropic_shape(malformed, request, 1.0, provider="mock-claude", model_used="x")


def test_inconsistent_usage_is_repaired_not_rejected():
    """Providers that return a slightly-wrong total_tokens shouldn't cause a
    hard failure -- the contract repairs it using prompt+completion."""
    request = _request()
    raw = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 999},
    }
    response = from_openai_shape(raw, request, 5.0, provider="mock-gpt", model_used="x")
    assert response.usage.total_tokens == 15


def test_unknown_finish_reason_defaults_to_stop():
    request = _request()
    raw = {
        "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "weird_value"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    response = from_openai_shape(raw, request, 1.0, provider="mock-gpt", model_used="x")
    assert response.finish_reason == FinishReason.stop
