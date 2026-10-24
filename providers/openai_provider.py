"""Real OpenAI provider adapter.

Only activates when `OPENAI_API_KEY` is set in the environment. Uses plain
HTTPS via `httpx` against the chat-completions endpoint so the only hard
dependency is `httpx` (already required for the Ollama adapter), rather than
pulling in the full `openai` SDK.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from providers.base import BaseProviderAdapter
from providers.exceptions import (
    ProviderOutageError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from providers.normalize_utils import from_openai_shape
from schemas.contracts import ChatRequest, ChatResponse

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(BaseProviderAdapter):
    name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
        self.timeout_s = timeout_s

    def generate_raw(self, request: ChatRequest) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderUnavailableError(
                "OPENAI_API_KEY not configured; OpenAI provider is disabled in this environment",
                provider=self.name,
            )
        payload = {
            "model": self.model,
            "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout_s,
            )
        except httpx.RequestError as exc:
            raise ProviderOutageError(
                f"OpenAI request failed: {exc}", provider=self.name
            ) from exc

        if resp.status_code == 429:
            raise ProviderRateLimitedError(
                "OpenAI returned 429 rate-limit", provider=self.name, details={"status_code": 429}
            )
        if resp.status_code >= 500:
            raise ProviderOutageError(
                f"OpenAI returned {resp.status_code}",
                provider=self.name,
                details={"status_code": resp.status_code},
            )
        if resp.status_code >= 400:
            raise ProviderOutageError(
                f"OpenAI returned {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                details={"status_code": resp.status_code},
            )
        return resp.json()

    def normalize(
        self, raw: dict[str, Any], request: ChatRequest, latency_ms: float
    ) -> ChatResponse:
        return from_openai_shape(
            raw, request, latency_ms, provider=self.name, model_used=self.model
        )
