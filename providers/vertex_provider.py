"""Real GCP Vertex AI provider adapter.

Activates only when `GOOGLE_APPLICATION_CREDENTIALS` (or an explicit
`VERTEX_ACCESS_TOKEN` for simpler demos) and `VERTEX_PROJECT_ID` are set.
Uses the REST `generateContent` endpoint directly via `httpx` to avoid a
hard dependency on `google-cloud-aiplatform` for a self-contained demo repo.
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
from providers.normalize_utils import from_vertex_shape
from schemas.contracts import ChatRequest, ChatResponse


class VertexAIProvider(BaseProviderAdapter):
    name = "vertex_ai"

    def __init__(
        self,
        model: str = "gemini-1.5-flash",
        project_id: str | None = None,
        location: str = "us-central1",
        access_token: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.model = model
        self.project_id = project_id or os.environ.get("VERTEX_PROJECT_ID")
        self.location = location or os.environ.get("VERTEX_LOCATION", "us-central1")
        # In production, exchange GOOGLE_APPLICATION_CREDENTIALS for a bearer
        # token via google-auth. To keep this adapter dependency-light for the
        # demo, we accept a pre-fetched token via env var/constructor arg.
        self.access_token = access_token or os.environ.get("VERTEX_ACCESS_TOKEN")
        self.timeout_s = timeout_s

    def _endpoint(self) -> str:
        return (
            f"https://{self.location}-aiplatform.googleapis.com/v1/projects/"
            f"{self.project_id}/locations/{self.location}/publishers/google/"
            f"models/{self.model}:generateContent"
        )

    def generate_raw(self, request: ChatRequest) -> dict[str, Any]:
        if not self.project_id or not self.access_token:
            raise ProviderUnavailableError(
                "VERTEX_PROJECT_ID / VERTEX_ACCESS_TOKEN not configured; "
                "Vertex AI provider is disabled in this environment",
                provider=self.name,
            )
        contents = [
            {
                "role": "user" if m.role.value != "assistant" else "model",
                "parts": [{"text": m.content}],
            }
            for m in request.messages
        ]
        payload = {
            "contents": contents,
            "generationConfig": {"temperature": request.temperature},
        }
        try:
            resp = httpx.post(
                self._endpoint(),
                json=payload,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=self.timeout_s,
            )
        except httpx.RequestError as exc:
            raise ProviderOutageError(
                f"Vertex AI request failed: {exc}", provider=self.name
            ) from exc

        if resp.status_code == 429:
            raise ProviderRateLimitedError(
                "Vertex AI returned 429 rate-limit",
                provider=self.name,
                details={"status_code": 429},
            )
        if resp.status_code >= 500:
            raise ProviderOutageError(
                f"Vertex AI returned {resp.status_code}",
                provider=self.name,
                details={"status_code": resp.status_code},
            )
        if resp.status_code >= 400:
            raise ProviderOutageError(
                f"Vertex AI returned {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                details={"status_code": resp.status_code},
            )
        return resp.json()

    def normalize(
        self, raw: dict[str, Any], request: ChatRequest, latency_ms: float
    ) -> ChatResponse:
        return from_vertex_shape(
            raw, request, latency_ms, provider=self.name, model_used=self.model
        )
