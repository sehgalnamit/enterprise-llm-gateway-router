"""Semantic cache hit/miss, scoping, and invalidation tests."""

from __future__ import annotations

from cache.embedder import HashEmbedder
from cache.semantic_cache import SemanticCache
from schemas.contracts import ChatResponse, FinishReason, Usage


def _response(content: str = "Paris is the capital of France.") -> ChatResponse:
    return ChatResponse(
        request_id="req_x",
        model_used="mock-gpt-4o",
        provider="mock-gpt",
        content=content,
        finish_reason=FinishReason.stop,
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        latency_ms=10.0,
    )


def test_cache_miss_when_empty(fake_redis_client):
    cache = SemanticCache(fake_redis_client, HashEmbedder(), similarity_threshold=0.9)
    assert cache.get("What is the capital of France?", scope="mock-gpt") is None


def test_cache_hit_for_identical_prompt(fake_redis_client):
    cache = SemanticCache(fake_redis_client, HashEmbedder(), similarity_threshold=0.9)
    cache.set("What is the capital of France?", _response(), scope="mock-gpt")
    hit = cache.get("What is the capital of France?", scope="mock-gpt")
    assert hit is not None
    assert hit.cache_hit is True
    assert hit.content == "Paris is the capital of France."
    assert hit.latency_ms == 0.0


def test_cache_hit_for_near_duplicate_prompt_above_threshold(fake_redis_client):
    cache = SemanticCache(fake_redis_client, HashEmbedder(), similarity_threshold=0.6)
    cache.set("What is the capital of France?", _response(), scope="mock-gpt")
    # Same bag of words, different casing/punctuation/order-insensitive hash
    # embedding -> should still be judged near-identical.
    hit = cache.get("what is the capital of france", scope="mock-gpt")
    assert hit is not None


def test_cache_miss_for_dissimilar_prompt(fake_redis_client):
    cache = SemanticCache(fake_redis_client, HashEmbedder(), similarity_threshold=0.9)
    cache.set("What is the capital of France?", _response(), scope="mock-gpt")
    miss = cache.get("Explain quantum entanglement in detail please", scope="mock-gpt")
    assert miss is None


def test_cache_is_scoped_per_model_persona(fake_redis_client):
    cache = SemanticCache(fake_redis_client, HashEmbedder(), similarity_threshold=0.9)
    cache.set("What is the capital of France?", _response(), scope="mock-gpt")
    miss = cache.get("What is the capital of France?", scope="mock-claude")
    assert miss is None


def test_invalidate_scope_removes_only_that_scope(fake_redis_client):
    cache = SemanticCache(fake_redis_client, HashEmbedder(), similarity_threshold=0.9)
    cache.set("prompt one", _response(), scope="mock-gpt")
    cache.set("prompt two", _response(), scope="mock-claude")
    removed = cache.invalidate_scope("mock-gpt")
    assert removed == 1
    assert cache.get("prompt one", scope="mock-gpt") is None
    assert cache.get("prompt two", scope="mock-claude") is not None


def test_ttl_is_applied_on_write(fake_redis_client):
    cache = SemanticCache(
        fake_redis_client, HashEmbedder(), similarity_threshold=0.9, ttl_seconds=1
    )
    key = cache.set("short lived prompt", _response(), scope="mock-gpt")
    ttl = fake_redis_client.ttl(key)
    assert 0 < ttl <= 1
