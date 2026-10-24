"""Canonical request/response contracts for the gateway.

Design goal: every provider adapter (OpenAI, Vertex AI, Ollama, Mock, ...)
returns a *raw* provider-shaped payload internally, and the adapter's
`normalize()` step must coerce (validate + repair where safe) that payload
into the single `ChatResponse` contract defined here. If a payload cannot be
coerced, the adapter must raise `SchemaContractError` with structured detail
so callers get a clear, typed error instead of a partially-shaped object.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MessageRole(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


class FinishReason(str, Enum):
    stop = "stop"
    length = "length"
    content_filter = "content_filter"
    error = "error"


class ChatMessage(BaseModel):
    """A single provider-agnostic conversation turn."""

    role: MessageRole
    content: str

    @field_validator("content")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if v is None:
            raise ValueError("message content cannot be None")
        return v


class ChatRequest(BaseModel):
    """Canonical inbound request. This is the ONLY request shape the router,
    cache, and evaluator ever see -- adapters translate it to provider-native
    payloads internally.
    """

    request_id: str = Field(default_factory=_new_request_id)
    session_id: str | None = Field(
        default=None,
        description="Opaque conversation/session identifier. When set, the "
        "router carries message history across provider failover with zero "
        "context loss.",
    )
    messages: list[ChatMessage]
    model: str | None = Field(
        default=None,
        description="Requested model persona/alias, e.g. 'mock-claude', "
        "'mock-gpt', 'local-qwen'. The router resolves this to a provider "
        "chain; it is NOT a literal upstream model id.",
    )
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("messages")
    @classmethod
    def _must_have_messages(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if not v:
            raise ValueError("messages must contain at least one turn")
        return v

    def last_user_text(self) -> str:
        for msg in reversed(self.messages):
            if msg.role == MessageRole.user:
                return msg.content
        return self.messages[-1].content


class Usage(BaseModel):
    """Token accounting, normalized from whatever field names/units a
    provider returns (e.g. OpenAI's prompt_tokens/completion_tokens vs.
    Vertex's promptTokenCount/candidatesTokenCount)."""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @field_validator("total_tokens")
    @classmethod
    def _total_consistent(cls, v: int, info: Any) -> int:
        # Repair rather than reject: if upstream total disagrees with the
        # sum (common with providers that round/estimate), trust the sum.
        data = info.data
        if "prompt_tokens" in data and "completion_tokens" in data:
            expected = data["prompt_tokens"] + data["completion_tokens"]
            if v != expected:
                return expected
        return v


class ChatResponse(BaseModel):
    """Canonical response contract. EVERY provider adapter must produce this
    shape. Fields are intentionally provider-neutral."""

    request_id: str
    session_id: str | None = None
    model_used: str
    provider: str
    content: str
    finish_reason: FinishReason
    usage: Usage
    latency_ms: float = Field(ge=0.0)
    cache_hit: bool = False
    created_at: datetime = Field(default_factory=_utcnow)

    model_config = {"protected_namespaces": ()}


class GatewayError(BaseModel):
    """Structured, typed error returned to callers when a provider's output
    cannot be safely coerced into `ChatResponse`, or when all providers in a
    failover chain are unavailable."""

    request_id: str
    error_type: Literal[
        "schema_violation",
        "provider_error",
        "rate_limited",
        "circuit_open",
        "all_providers_unavailable",
    ]
    provider: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SchemaContractError(Exception):
    """Raised by a provider adapter when a raw payload cannot be repaired
    into the canonical `ChatResponse` contract."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        request_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.request_id = request_id
        self.details = details or {}

    def to_gateway_error(self) -> GatewayError:
        return GatewayError(
            request_id=self.request_id,
            error_type="schema_violation",
            provider=self.provider,
            message=self.message,
            details=self.details,
        )


class Timer:
    """Tiny helper so adapters report real wall-clock latency consistently."""

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0
