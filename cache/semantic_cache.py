"""Redis-backed semantic cache.

Cache entries are stored as individual Redis keys (so native `EX` TTLs apply
per-entry) under a namespaced pattern. Lookups embed the incoming prompt and
scan candidate keys for the given scope, returning the highest-similarity
cached response if it clears `similarity_threshold`. This is O(n) per scope
and documented as a demo-scale approach (see README) -- a production
deployment would back this with a vector index (e.g. RediSearch's vector
similarity, pgvector, etc.).

Works against any client exposing the subset of the `redis-py` API used here
(`get`, `set`, `delete`, `scan_iter`), so both a real `redis.Redis` and
`fakeredis.FakeRedis` (used in tests) are interchangeable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from cache.embedder import Embedder, HashEmbedder, cosine_similarity
from schemas.contracts import ChatRequest, ChatResponse

DEFAULT_NAMESPACE = "semcache"
DEFAULT_TTL_SECONDS = 3600
DEFAULT_SIMILARITY_THRESHOLD = 0.92


class RedisLike(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, ex: int | None = None) -> Any: ...
    def delete(self, *keys: str) -> Any: ...
    def scan_iter(self, match: str | None = None) -> Any: ...


def cache_scope_for_request(request: ChatRequest) -> str:
    """Requests are only compared for semantic similarity within the same
    scope (requested model persona), since different models can legitimately
    give different answers to the same prompt."""
    return request.model or "default"


class SemanticCache:
    def __init__(
        self,
        redis_client: RedisLike,
        embedder: Embedder | None = None,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.redis = redis_client
        self.embedder = embedder or HashEmbedder()
        self.namespace = namespace
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds

    def _pattern(self, scope: str) -> str:
        return f"{self.namespace}:{scope}:*"

    def _key(self, scope: str, item_id: str) -> str:
        return f"{self.namespace}:{scope}:{item_id}"

    def get(self, prompt: str, *, scope: str) -> ChatResponse | None:
        """Return the best cached response for `prompt` within `scope` if it
        clears the similarity threshold, else None (cache miss)."""
        query_embedding = self.embedder.embed(prompt)
        best_score = -2.0
        best_payload: dict[str, Any] | None = None
        for key in self.redis.scan_iter(match=self._pattern(scope)):
            raw = self.redis.get(key)
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            score = cosine_similarity(query_embedding, entry.get("embedding", []))
            if score > best_score:
                best_score = score
                best_payload = entry
        if best_payload is not None and best_score >= self.similarity_threshold:
            response = ChatResponse.model_validate(best_payload["response"])
            response.cache_hit = True
            response.latency_ms = 0.0
            return response
        return None

    def set(self, prompt: str, response: ChatResponse, *, scope: str) -> str:
        """Store `response` keyed by the embedding of `prompt`. Returns the
        Redis key written (useful for invalidation tests)."""
        embedding = self.embedder.embed(prompt)
        item_id = hashlib.sha256(f"{scope}:{prompt}".encode()).hexdigest()[:20]
        key = self._key(scope, item_id)
        payload = {
            "embedding": embedding,
            "prompt": prompt,
            "response": json.loads(response.model_dump_json()),
        }
        self.redis.set(key, json.dumps(payload), ex=self.ttl_seconds)
        return key

    def invalidate_scope(self, scope: str) -> int:
        """Evict every cached entry for `scope`. Returns count removed."""
        removed = 0
        for key in list(self.redis.scan_iter(match=self._pattern(scope))):
            self.redis.delete(key)
            removed += 1
        return removed

    def invalidate_all(self) -> int:
        removed = 0
        for key in list(self.redis.scan_iter(match=f"{self.namespace}:*")):
            self.redis.delete(key)
            removed += 1
        return removed
