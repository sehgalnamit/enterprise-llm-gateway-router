"""Deterministic mock provider used for offline tests and as the safe
default demo backend.

A single `MockProvider` class is instantiated three times with different
`persona` values to simulate three distinguishable backends:

* ``mock-claude``  -- Anthropic-messages-shaped raw payload, slower/verbose.
* ``mock-gpt``     -- OpenAI-chat-completions-shaped raw payload, medium speed.
* ``local-qwen``   -- Ollama-shaped raw payload, fastest/terse (stands in for
  a real local Ollama model when Ollama isn't installed).

Responses are generated deterministically from a hash of the prompt so tests
and demos are fully reproducible with no network access.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Literal

from providers.base import BaseProviderAdapter
from providers.exceptions import ProviderOutageError, ProviderRateLimitedError
from providers.normalize_utils import from_anthropic_shape, from_ollama_shape, from_openai_shape
from schemas.contracts import ChatRequest, ChatResponse

Persona = Literal["mock-claude", "mock-gpt", "local-qwen"]

_PROFILES: dict[str, dict[str, Any]] = {
    "mock-claude": {
        "display_model": "mock-claude-3-sonnet",
        "base_latency_s": 0.045,
        "jitter_s": 0.015,
        "style": "verbose",
    },
    "mock-gpt": {
        "display_model": "mock-gpt-4o",
        "base_latency_s": 0.028,
        "jitter_s": 0.010,
        "style": "balanced",
    },
    "local-qwen": {
        "display_model": "mock-qwen2.5-7b-local",
        "base_latency_s": 0.012,
        "jitter_s": 0.006,
        "style": "terse",
    },
}


def _deterministic_unit_interval(seed_text: str) -> float:
    """Map arbitrary text to a reproducible float in [0, 1)."""
    digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _compose_answer(persona: str, prompt: str) -> str:
    profile = _PROFILES[persona]
    style = profile["style"]
    seed = _deterministic_unit_interval(f"{persona}:{prompt}")
    if style == "verbose":
        return (
            f"[{profile['display_model']}] Considering your request -- \"{prompt.strip()}\" "
            f"-- here is a thorough answer (det. seed {seed:.4f}): "
            f"the gateway normalized this reply from an Anthropic-shaped payload."
        )
    if style == "balanced":
        return (
            f"[{profile['display_model']}] Answer to \"{prompt.strip()}\" "
            f"(det. seed {seed:.4f}): normalized from an OpenAI-shaped payload."
        )
    # terse / local-qwen
    return f"[{profile['display_model']}] {prompt.strip()[:60]} -> ok (seed {seed:.4f})"


class MockProvider(BaseProviderAdapter):
    """Deterministic, network-free provider adapter with a selectable persona."""

    def __init__(self, persona: Persona, *, simulate_latency: bool = True) -> None:
        if persona not in _PROFILES:
            raise ValueError(f"unknown mock persona: {persona}")
        self.persona: Persona = persona
        self.name = persona
        self.simulate_latency = simulate_latency

    def _simulate_latency(self, prompt: str) -> None:
        if not self.simulate_latency:
            return
        profile = _PROFILES[self.persona]
        jitter = _deterministic_unit_interval(f"latency:{self.persona}:{prompt}")
        time.sleep(profile["base_latency_s"] + jitter * profile["jitter_s"])

    def generate_raw(self, request: ChatRequest) -> dict[str, Any]:
        fault = self.current_fault()
        if fault == "rate_limited":
            raise ProviderRateLimitedError(
                f"mock provider '{self.name}' injected 429 rate-limit",
                provider=self.name,
                details={"status_code": 429},
            )
        if fault == "outage":
            raise ProviderOutageError(
                f"mock provider '{self.name}' injected outage",
                provider=self.name,
                details={"status_code": 503},
            )

        prompt = request.last_user_text()
        self._simulate_latency(prompt)
        answer = _compose_answer(self.persona, prompt)
        prompt_tokens = max(1, len(prompt.split()))
        completion_tokens = max(1, len(answer.split()))

        if self.persona == "mock-claude":
            return {
                "id": f"msg_{request.request_id}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": answer}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
            }
        if self.persona == "mock-gpt":
            return {
                "id": f"chatcmpl_{request.request_id}",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        # local-qwen -> Ollama shape
        return {
            "model": "qwen2.5:7b",
            "created_at": "1970-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": answer},
            "done": True,
            "prompt_eval_count": prompt_tokens,
            "eval_count": completion_tokens,
        }

    def normalize(
        self, raw: dict[str, Any], request: ChatRequest, latency_ms: float
    ) -> ChatResponse:
        profile = _PROFILES[self.persona]
        if self.persona == "mock-claude":
            return from_anthropic_shape(
                raw, request, latency_ms, provider=self.name, model_used=profile["display_model"]
            )
        if self.persona == "mock-gpt":
            return from_openai_shape(
                raw, request, latency_ms, provider=self.name, model_used=profile["display_model"]
            )
        return from_ollama_shape(
            raw, request, latency_ms, provider=self.name, model_used=profile["display_model"]
        )
