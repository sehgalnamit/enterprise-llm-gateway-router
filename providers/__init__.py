"""Provider adapter registry.

`build_default_providers()` returns the demo/test provider set: three
deterministic `MockProvider` personas (`mock-claude`, `mock-gpt`,
`local-qwen`) plus best-effort real adapters (`OpenAIProvider`,
`VertexAIProvider`, `OllamaProvider`) that self-disable when not configured.
Nothing here requires network access or API keys to construct or to run
against the mock personas.
"""

from __future__ import annotations

from providers.base import BaseProviderAdapter
from providers.mock import MockProvider
from providers.ollama_provider import OllamaProvider
from providers.openai_provider import OpenAIProvider
from providers.vertex_provider import VertexAIProvider

__all__ = [
    "BaseProviderAdapter",
    "MockProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "VertexAIProvider",
    "build_default_providers",
]


def build_default_providers() -> dict[str, BaseProviderAdapter]:
    """Return the standard provider registry keyed by provider name.

    Real adapters are included for completeness/demo purposes but will raise
    `ProviderUnavailableError` on first use if not configured -- the router
    treats that exactly like any other failover trigger.
    """
    return {
        "mock-claude": MockProvider(persona="mock-claude"),
        "mock-gpt": MockProvider(persona="mock-gpt"),
        "local-qwen": MockProvider(persona="local-qwen"),
        "openai": OpenAIProvider(),
        "vertex_ai": VertexAIProvider(),
        "ollama": OllamaProvider(),
    }
