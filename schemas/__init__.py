"""Canonical schema contracts shared by every provider adapter, the router,
the semantic cache, and the jury evaluator.

Every provider adapter MUST ultimately produce a `ChatResponse` instance.
Nothing downstream of `providers/` is allowed to know about provider-specific
response shapes.
"""

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

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "FinishReason",
    "GatewayError",
    "MessageRole",
    "SchemaContractError",
    "Usage",
]
