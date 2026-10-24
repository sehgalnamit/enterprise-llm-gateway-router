"""Shared pytest fixtures.

Everything here runs fully offline: `MockProvider` personas + `fakeredis`
for the semantic cache. No network access, no API keys, no real Redis/Ollama
required.
"""

from __future__ import annotations

import pytest

from providers.fault_injection import fault_injector
from providers.mock import MockProvider


@pytest.fixture(autouse=True)
def _clear_fault_injector():
    fault_injector.clear()
    yield
    fault_injector.clear()


@pytest.fixture
def mock_providers():
    return {
        "mock-claude": MockProvider(persona="mock-claude", simulate_latency=False),
        "mock-gpt": MockProvider(persona="mock-gpt", simulate_latency=False),
        "local-qwen": MockProvider(persona="local-qwen", simulate_latency=False),
    }


@pytest.fixture
def fake_redis_client():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)
