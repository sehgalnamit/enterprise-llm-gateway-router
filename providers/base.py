"""Abstract provider adapter base class.

Every concrete adapter implements `generate_raw()`, which returns a
provider-native (possibly malformed) payload, plus provider-specific
`normalize()` logic that coerces that payload into the canonical
`ChatResponse` contract. `chat()` is the single public entry point used by
the router: it measures latency, converts transport-level failures into the
`ProviderError` hierarchy, and calls `normalize()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from providers.exceptions import ProviderError
from providers.fault_injection import FaultMode, fault_injector
from schemas.contracts import ChatRequest, ChatResponse, SchemaContractError, Timer


class BaseProviderAdapter(ABC):
    """Base class for all LLM provider adapters."""

    #: Stable machine name used in circuit-breaker keys, cache keys, logs.
    name: str = "base"

    def current_fault(self) -> FaultMode:
        return fault_injector.current_fault(self.name)

    @abstractmethod
    def generate_raw(self, request: ChatRequest) -> dict[str, Any]:
        """Return a provider-native payload for `request`.

        Implementations should raise `ProviderRateLimitedError` /
        `ProviderOutageError` / `ProviderUnavailableError` on failure rather
        than returning a malformed payload, EXCEPT where the whole point is
        to demonstrate contract repair/rejection (see `normalize()`).
        """

    @abstractmethod
    def normalize(
        self, raw: dict[str, Any], request: ChatRequest, latency_ms: float
    ) -> ChatResponse:
        """Coerce a raw provider payload into the canonical `ChatResponse`.

        Must raise `SchemaContractError` if the payload cannot be safely
        repaired into the contract.
        """

    def chat(self, request: ChatRequest) -> ChatResponse:
        """Public entry point used by the router."""
        with Timer() as timer:
            raw = self.generate_raw(request)
        return self.normalize(raw, request, timer.elapsed_ms)

    def normalize_external(
        self, raw: dict[str, Any], request: ChatRequest, latency_ms: float = 0.0
    ) -> ChatResponse:
        """Exposed for the contract-enforcement demo/tests: lets callers feed
        an arbitrary (possibly malformed) payload through this adapter's
        normalization logic without making a real call."""
        return self.normalize(raw, request, latency_ms)


__all__ = ["BaseProviderAdapter", "ProviderError", "SchemaContractError"]
