"""Shared raw-payload -> canonical-contract coercion helpers.

Each provider's upstream API returns responses in a different shape. These
helpers centralize the "validate, repair where safe, else raise
`SchemaContractError`" logic so both the mock personas (which deliberately
mimic each real API's shape) and the real adapters share exactly the same
normalization code path.
"""

from __future__ import annotations

from typing import Any

from schemas.contracts import ChatRequest, ChatResponse, FinishReason, SchemaContractError, Usage


def _repair_usage(
    prompt_tokens: Any, completion_tokens: Any, *, provider: str, request_id: str
) -> Usage:
    try:
        p = max(0, int(prompt_tokens)) if prompt_tokens is not None else 0
        c = max(0, int(completion_tokens)) if completion_tokens is not None else 0
    except (TypeError, ValueError) as exc:
        raise SchemaContractError(
            f"provider '{provider}' returned non-numeric token usage",
            provider=provider,
            request_id=request_id,
            details={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        ) from exc
    return Usage(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)


_FINISH_REASON_MAP = {
    "stop": FinishReason.stop,
    "end_turn": FinishReason.stop,
    "stop_sequence": FinishReason.stop,
    "done": FinishReason.stop,
    "length": FinishReason.length,
    "max_tokens": FinishReason.length,
    "content_filter": FinishReason.content_filter,
    "safety": FinishReason.content_filter,
}


def _map_finish_reason(raw_reason: Any) -> FinishReason:
    if raw_reason is None:
        return FinishReason.stop
    return _FINISH_REASON_MAP.get(str(raw_reason).lower(), FinishReason.stop)


def from_openai_shape(
    raw: dict[str, Any],
    request: ChatRequest,
    latency_ms: float,
    *,
    provider: str,
    model_used: str,
) -> ChatResponse:
    """Coerce an OpenAI chat-completions-shaped payload into the contract."""
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SchemaContractError(
            f"provider '{provider}' response missing non-empty 'choices' list",
            provider=provider,
            request_id=request.request_id,
            details={"raw_keys": list(raw.keys())},
        )
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or content == "":
        raise SchemaContractError(
            f"provider '{provider}' response missing non-empty message content",
            provider=provider,
            request_id=request.request_id,
            details={"choice": first},
        )
    usage = raw.get("usage") or {}
    return ChatResponse(
        request_id=request.request_id,
        session_id=request.session_id,
        model_used=model_used,
        provider=provider,
        content=content,
        finish_reason=_map_finish_reason(first.get("finish_reason")),
        usage=_repair_usage(
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            provider=provider,
            request_id=request.request_id,
        ),
        latency_ms=latency_ms,
    )


def from_anthropic_shape(
    raw: dict[str, Any],
    request: ChatRequest,
    latency_ms: float,
    *,
    provider: str,
    model_used: str,
) -> ChatResponse:
    """Coerce an Anthropic messages-API-shaped payload into the contract."""
    content_blocks = raw.get("content")
    if not isinstance(content_blocks, list) or not content_blocks:
        raise SchemaContractError(
            f"provider '{provider}' response missing non-empty 'content' blocks",
            provider=provider,
            request_id=request.request_id,
            details={"raw_keys": list(raw.keys())},
        )
    text_parts = [
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    content = "".join(text_parts)
    if not content:
        raise SchemaContractError(
            f"provider '{provider}' response contained no text content blocks",
            provider=provider,
            request_id=request.request_id,
            details={"content_blocks": content_blocks},
        )
    usage = raw.get("usage") or {}
    return ChatResponse(
        request_id=request.request_id,
        session_id=request.session_id,
        model_used=model_used,
        provider=provider,
        content=content,
        finish_reason=_map_finish_reason(raw.get("stop_reason")),
        usage=_repair_usage(
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            provider=provider,
            request_id=request.request_id,
        ),
        latency_ms=latency_ms,
    )


def from_ollama_shape(
    raw: dict[str, Any],
    request: ChatRequest,
    latency_ms: float,
    *,
    provider: str,
    model_used: str,
) -> ChatResponse:
    """Coerce an Ollama /api/chat-shaped payload into the contract."""
    message = raw.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or content == "":
        raise SchemaContractError(
            f"provider '{provider}' response missing non-empty message content",
            provider=provider,
            request_id=request.request_id,
            details={"raw_keys": list(raw.keys())},
        )
    finish_reason = FinishReason.stop if raw.get("done", True) else FinishReason.length
    return ChatResponse(
        request_id=request.request_id,
        session_id=request.session_id,
        model_used=model_used,
        provider=provider,
        content=content,
        finish_reason=finish_reason,
        usage=_repair_usage(
            raw.get("prompt_eval_count"),
            raw.get("eval_count"),
            provider=provider,
            request_id=request.request_id,
        ),
        latency_ms=latency_ms,
    )


def from_vertex_shape(
    raw: dict[str, Any],
    request: ChatRequest,
    latency_ms: float,
    *,
    provider: str,
    model_used: str,
) -> ChatResponse:
    """Coerce a Vertex AI generateContent-shaped payload into the contract."""
    candidates = raw.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise SchemaContractError(
            f"provider '{provider}' response missing non-empty 'candidates' list",
            provider=provider,
            request_id=request.request_id,
            details={"raw_keys": list(raw.keys())},
        )
    first = candidates[0]
    parts = (first.get("content") or {}).get("parts") if isinstance(first, dict) else None
    text_parts = [p.get("text", "") for p in parts] if isinstance(parts, list) else []
    content = "".join(text_parts)
    if not content:
        raise SchemaContractError(
            f"provider '{provider}' response contained no text parts",
            provider=provider,
            request_id=request.request_id,
            details={"candidate": first},
        )
    usage = raw.get("usageMetadata") or {}
    return ChatResponse(
        request_id=request.request_id,
        session_id=request.session_id,
        model_used=model_used,
        provider=provider,
        content=content,
        finish_reason=_map_finish_reason(first.get("finishReason")),
        usage=_repair_usage(
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            provider=provider,
            request_id=request.request_id,
        ),
        latency_ms=latency_ms,
    )
