"""Process-wide singletons: Redis client (with automatic fakeredis fallback
for local/offline demo use), the semantic cache, the provider registry, and
the gateway router. FastAPI endpoints depend on `get_router()` /
`get_jury()`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import Settings, get_settings
from cache.embedder import HashEmbedder
from cache.semantic_cache import SemanticCache
from evaluator.jury import Jury
from providers import build_default_providers
from router.circuit_breaker import CircuitBreakerConfig
from router.gateway_router import GatewayRouter


def _build_redis_client(settings: Settings) -> Any:
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception:
        if not settings.use_fake_redis_if_unreachable:
            raise
        import fakeredis

        # No real Redis reachable (e.g. local dev without `docker compose up`,
        # or CI). Transparently fall back to an in-memory fake with the same
        # client API so the semantic cache still works end-to-end.
        return fakeredis.FakeRedis(decode_responses=True)


@lru_cache(maxsize=1)
def get_settings_cached() -> Settings:
    return get_settings()


@lru_cache(maxsize=1)
def get_router() -> GatewayRouter:
    settings = get_settings_cached()
    redis_client = _build_redis_client(settings)
    cache = SemanticCache(
        redis_client,
        HashEmbedder(),
        similarity_threshold=settings.cache_similarity_threshold,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    breaker_config = CircuitBreakerConfig(
        failure_threshold=settings.circuit_failure_threshold,
        window_seconds=settings.circuit_window_seconds,
        recovery_timeout=settings.circuit_recovery_timeout_s,
        max_recovery_timeout=settings.circuit_max_recovery_timeout_s,
    )
    providers = build_default_providers()
    return GatewayRouter(providers, breaker_config=breaker_config, cache=cache)


@lru_cache(maxsize=1)
def get_jury() -> Jury:
    settings = get_settings_cached()
    return Jury(score_threshold=settings.jury_score_threshold)


def reset_singletons() -> None:
    """Test helper: clears cached singletons between test modules."""
    get_router.cache_clear()
    get_jury.cache_clear()
    get_settings_cached.cache_clear()
