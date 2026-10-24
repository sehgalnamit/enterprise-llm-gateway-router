"""Real local Ollama provider adapter.

Activates only if an Ollama server is actually reachable at `OLLAMA_HOST`
(default `http://localhost:11434`). If unreachable, raises
`ProviderUnavailableError` so the router fails over to the mock/local
persona instead -- this keeps the default demo path fully offline while
still exercising a real local model whenever Ollama happens to be running.
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
from providers.normalize_utils import from_ollama_shape
from schemas.contracts import ChatRequest, ChatResponse

DEFAULT_HOST = "http://localhost:11434"


class OllamaProvider(BaseProviderAdapter):
    name = "ollama"

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        host: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.model = model
        self.host = host or os.environ.get("OLLAMA_HOST", DEFAULT_HOST)
        self.timeout_s = timeout_s

    def is_available(self) -> bool:
        try:
            resp = httpx.get(f"{self.host}/api/tags", timeout=2.0)
            return resp.status_code == 200
        except httpx.RequestError:
            return False

    def generate_raw(self, request: ChatRequest) -> dict[str, Any]:
        if not self.is_available():
            raise ProviderUnavailableError(
                f"Ollama server not reachable at {self.host}; "
                "falling back to mock local-qwen persona",
                provider=self.name,
            )
        payload = {
            "model": self.model,
            "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
            "stream": False,
            "options": {"temperature": request.temperature},
        }
        try:
            resp = httpx.post(f"{self.host}/api/chat", json=payload, timeout=self.timeout_s)
        except httpx.RequestError as exc:
            raise ProviderOutageError(
                f"Ollama request failed: {exc}", provider=self.name
            ) from exc

        if resp.status_code == 429:
            raise ProviderRateLimitedError(
                "Ollama returned 429 rate-limit", provider=self.name, details={"status_code": 429}
            )
        if resp.status_code >= 400:
            raise ProviderOutageError(
                f"Ollama returned {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                details={"status_code": resp.status_code},
            )
        return resp.json()

    def normalize(
        self, raw: dict[str, Any], request: ChatRequest, latency_ms: float
    ) -> ChatResponse:
        return from_ollama_shape(
            raw, request, latency_ms, provider=self.name, model_used=self.model
        )
