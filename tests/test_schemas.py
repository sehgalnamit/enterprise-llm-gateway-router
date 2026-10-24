"""Schema validation + repair tests for the canonical contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.contracts import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FinishReason,
    GatewayError,
    MessageRole,
    SchemaContractError,
    Usage,
)


def test_chat_request_requires_at_least_one_message():
    with pytest.raises(ValidationError):
        ChatRequest(messages=[])


def test_chat_request_generates_request_id_by_default():
    req = ChatRequest(messages=[ChatMessage(role=MessageRole.user, content="hi")])
    assert req.request_id.startswith("req_")


def test_chat_request_last_user_text_returns_latest_user_turn():
    req = ChatRequest(
        messages=[
            ChatMessage(role=MessageRole.user, content="first"),
            ChatMessage(role=MessageRole.assistant, content="reply"),
            ChatMessage(role=MessageRole.user, content="second"),
        ]
    )
    assert req.last_user_text() == "second"


def test_usage_repairs_inconsistent_total():
    usage = Usage(prompt_tokens=10, completion_tokens=5, total_tokens=999)
    assert usage.total_tokens == 15


def test_chat_response_round_trips_through_json():
    response = ChatResponse(
        request_id="req_abc",
        model_used="mock-gpt-4o",
        provider="mock-gpt",
        content="hello world",
        finish_reason=FinishReason.stop,
        usage=Usage(prompt_tokens=2, completion_tokens=2, total_tokens=4),
        latency_ms=12.5,
    )
    payload = response.model_dump_json()
    restored = ChatResponse.model_validate_json(payload)
    assert restored.content == "hello world"
    assert restored.cache_hit is False


def test_schema_contract_error_converts_to_gateway_error():
    err = SchemaContractError(
        "bad payload", provider="mock-gpt", request_id="req_123", details={"x": 1}
    )
    gw = err.to_gateway_error()
    assert isinstance(gw, GatewayError)
    assert gw.error_type == "schema_violation"
    assert gw.provider == "mock-gpt"
    assert gw.details == {"x": 1}


def test_chat_message_rejects_none_content():
    with pytest.raises(ValidationError):
        ChatMessage(role=MessageRole.user, content=None)  # type: ignore[arg-type]
