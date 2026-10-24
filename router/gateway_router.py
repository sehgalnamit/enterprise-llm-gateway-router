"""The failover-aware router: ties together provider adapters, per-provider
circuit breakers, the semantic cache, and the session store.

Routing algorithm for `route()`:
  1. Semantic-cache lookup (scoped by requested model persona). Cache hit ->
     return immediately with `cache_hit=True`, no provider call.
  2. Walk the configured failover chain for the requested persona. For each
     provider: skip if its breaker is OPEN; otherwise call it.
       - Success: record breaker success, populate cache, return.
       - `ProviderRateLimitedError` / `ProviderError`: record breaker
         failure, continue to the next provider in the chain.
  3. If every provider in the chain is exhausted/unavailable, raise
     `AllProvidersUnavailableError` with a structured `GatewayError`.

`route_with_session()` layers provider-agnostic session continuity on top:
message history lives in the `SessionStore`, keyed only by `session_id` --
never by provider -- so failing over mid-conversation is invisible to the
conversation itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cache.semantic_cache import SemanticCache, cache_scope_for_request
from providers.base import BaseProviderAdapter
from providers.exceptions import ProviderError, ProviderRateLimitedError
from router.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from router.session import InMemorySessionStore
from schemas.contracts import ChatRequest, ChatResponse, GatewayError

DEFAULT_PERSONAS = ("mock-claude", "mock-gpt", "local-qwen")


def default_failover_chains(personas: tuple[str, ...] = DEFAULT_PERSONAS) -> dict[str, list[str]]:
    """Each persona fails over to the others, primary-first."""
    chains: dict[str, list[str]] = {}
    for primary in personas:
        rest = [p for p in personas if p != primary]
        chains[primary] = [primary, *rest]
    chains["default"] = list(personas)
    return chains


@dataclass
class RouteAttempt:
    provider: str
    outcome: str  # "success" | "rate_limited" | "provider_error" | "breaker_open"
    detail: str | None = None


@dataclass
class RouteResult:
    response: ChatResponse
    attempts: list[RouteAttempt] = field(default_factory=list)


class AllProvidersUnavailableError(Exception):
    def __init__(self, error: GatewayError, attempts: list[RouteAttempt]) -> None:
        super().__init__(error.message)
        self.error = error
        self.attempts = attempts


class GatewayRouter:
    def __init__(
        self,
        providers: dict[str, BaseProviderAdapter],
        *,
        failover_chains: dict[str, list[str]] | None = None,
        breaker_config: CircuitBreakerConfig | None = None,
        cache: SemanticCache | None = None,
        session_store: InMemorySessionStore | None = None,
    ) -> None:
        self.providers = providers
        self.failover_chains = failover_chains or default_failover_chains(tuple(providers.keys()))
        self.cache = cache
        self.sessions = session_store or InMemorySessionStore()
        self.breakers: dict[str, CircuitBreaker] = {
            name: CircuitBreaker(name, breaker_config) for name in providers
        }

    def _chain_for(self, model: str | None) -> list[str]:
        key = model if model and model in self.failover_chains else "default"
        chain = self.failover_chains.get(key)
        if not chain:
            chain = list(self.providers.keys())
        return [p for p in chain if p in self.providers]

    def route(self, request: ChatRequest, *, use_cache: bool = True) -> RouteResult:
        attempts: list[RouteAttempt] = []

        if use_cache and self.cache is not None:
            scope = cache_scope_for_request(request)
            cached = self.cache.get(request.last_user_text(), scope=scope)
            if cached is not None:
                cached.request_id = request.request_id
                cached.session_id = request.session_id
                attempts.append(RouteAttempt(provider=cached.provider, outcome="cache_hit"))
                return RouteResult(response=cached, attempts=attempts)

        chain = self._chain_for(request.model)
        for provider_name in chain:
            breaker = self.breakers[provider_name]
            if not breaker.allow_request():
                attempts.append(RouteAttempt(provider=provider_name, outcome="breaker_open"))
                continue
            adapter = self.providers[provider_name]
            try:
                response = adapter.chat(request)
            except ProviderRateLimitedError as exc:
                breaker.record_failure(status_code=429)
                attempts.append(
                    RouteAttempt(provider=provider_name, outcome="rate_limited", detail=str(exc))
                )
                continue
            except ProviderError as exc:
                breaker.record_failure()
                attempts.append(
                    RouteAttempt(provider=provider_name, outcome="provider_error", detail=str(exc))
                )
                continue

            breaker.record_success()
            attempts.append(RouteAttempt(provider=provider_name, outcome="success"))
            if use_cache and self.cache is not None:
                scope = cache_scope_for_request(request)
                self.cache.set(request.last_user_text(), response, scope=scope)
            return RouteResult(response=response, attempts=attempts)

        error = GatewayError(
            request_id=request.request_id,
            error_type="all_providers_unavailable",
            message=(
                f"all providers in failover chain {chain} were unavailable "
                f"for request {request.request_id}"
            ),
            details={"attempts": [a.__dict__ for a in attempts]},
        )
        raise AllProvidersUnavailableError(error, attempts)

    def route_with_session(
        self,
        session_id: str,
        user_text: str,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        use_cache: bool = True,
    ) -> RouteResult:
        """Append `user_text` to the session's provider-agnostic history,
        route the full conversation, then append the assistant reply.
        Session continuity survives provider failover transparently."""
        self.sessions.append_user_message(session_id, user_text)
        history = self.sessions.history(session_id)
        request = ChatRequest(
            session_id=session_id,
            messages=history,
            model=model,
            temperature=temperature,
        )
        result = self.route(request, use_cache=use_cache)
        self.sessions.append_assistant_message(
            session_id, result.response.content, result.response.provider
        )
        return result

    def breaker_snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "state": breaker.snapshot().state.value,
                "failure_count": breaker.snapshot().failure_count,
                "recovery_timeout_s": breaker.snapshot().current_recovery_timeout,
            }
            for name, breaker in self.breakers.items()
        }


__all__ = [
    "AllProvidersUnavailableError",
    "DEFAULT_PERSONAS",
    "GatewayRouter",
    "RouteAttempt",
    "RouteResult",
    "default_failover_chains",
]
