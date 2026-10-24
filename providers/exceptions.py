"""Provider-specific exception hierarchy.

The router (`router/circuit_breaker.py`, `router/gateway_router.py`) only
needs to distinguish between "rate limited" (429-style, should trip the
breaker fast) and "generic provider error" (outage, malformed response,
timeout, etc. -- also trips the breaker, just without special 429 handling).
"""

from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """Base class for all provider-adapter failures."""

    def __init__(
        self, message: str, *, provider: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.details = details or {}


class ProviderRateLimitedError(ProviderError):
    """Upstream provider returned HTTP 429 / rate-limit equivalent."""


class ProviderOutageError(ProviderError):
    """Upstream provider is unreachable / returned a 5xx-equivalent fault."""


class ProviderUnavailableError(ProviderError):
    """Provider is not configured in this environment (e.g. missing API key
    or Ollama not running). Distinct from an outage: this is a static
    configuration gap, not a transient failure, but the router still treats
    it as a reason to fail over."""
