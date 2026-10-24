"""FastAPI gateway application.

Endpoints map directly to the three demo requirements described in the
README's "Demo Script" section:

* ``POST /v1/chat/multi``        -> (a) identical request fanned out to 2+
                                        backends, proving contract normalization.
* ``POST /v1/contract-check``    -> (b) feed a malformed provider payload
                                        through an adapter, see repair/rejection.
* ``POST /admin/fault-inject``
  + ``POST /v1/chat``            -> (c) inject a 429/outage, watch the
                                        circuit breaker trip and the router
                                        fail over mid-session with zero
                                        context loss.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.dependencies import get_jury, get_router
from providers.fault_injection import FaultMode, fault_injector
from router.gateway_router import AllProvidersUnavailableError
from schemas.contracts import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    GatewayError,
    MessageRole,
    SchemaContractError,
)

app = FastAPI(
    title="Enterprise LLM Gateway Router",
    description=(
        "Provider-agnostic LLM gateway with schema contracts, circuit "
        "breakers, and semantic caching."
    ),
    version="0.1.0",
)


# --------------------------------------------------------------------------
# Request/response models for the HTTP API (distinct from the canonical
# internal ChatRequest/ChatResponse, which stay provider/session agnostic).
# --------------------------------------------------------------------------


class ChatTurn(BaseModel):
    session_id: str
    message: str
    model: str | None = Field(default=None, description="e.g. mock-claude, mock-gpt, local-qwen")
    temperature: float = 0.7


class MultiProviderRequest(BaseModel):
    message: str
    models: list[str] = Field(default_factory=lambda: ["mock-claude", "mock-gpt", "local-qwen"])
    temperature: float = 0.7


class ContractCheckRequest(BaseModel):
    provider: str = Field(description="One of the registered provider names, e.g. mock-gpt")
    raw_payload: dict
    prompt: str = "demo prompt"


class FaultInjectRequest(BaseModel):
    provider: str
    mode: FaultMode = "rate_limited"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/chat", response_model=ChatResponse, responses={503: {"model": GatewayError}})
def chat(turn: ChatTurn) -> ChatResponse:
    router = get_router()
    try:
        result = router.route_with_session(
            turn.session_id, turn.message, model=turn.model, temperature=turn.temperature
        )
    except AllProvidersUnavailableError as exc:
        raise HTTPException(status_code=503, detail=exc.error.model_dump(mode="json")) from exc
    return result.response


@app.post(
    "/v1/chat/completions", response_model=ChatResponse, responses={503: {"model": GatewayError}}
)
def chat_completions(request: ChatRequest) -> ChatResponse:
    """OpenAI-style raw completion endpoint: full canonical ChatRequest in,
    canonical ChatResponse out. No session continuity (stateless)."""
    router = get_router()
    try:
        result = router.route(request)
    except AllProvidersUnavailableError as exc:
        raise HTTPException(status_code=503, detail=exc.error.model_dump(mode="json")) from exc
    return result.response


@app.post("/v1/chat/multi")
def chat_multi(body: MultiProviderRequest) -> dict[str, ChatResponse | dict]:
    """Demo (a): send an IDENTICAL request to 2+ backends and return each
    one's canonical ChatResponse, proving contract normalization despite
    each backend's raw payload shape being completely different upstream."""
    router = get_router()
    results: dict[str, ChatResponse | dict] = {}
    for model in body.models:
        request = ChatRequest(
            messages=[ChatMessage(role=MessageRole.user, content=body.message)],
            model=model,
            temperature=body.temperature,
        )
        try:
            route_result = router.route(request, use_cache=False)
            results[model] = route_result.response
        except AllProvidersUnavailableError as exc:
            results[model] = exc.error.model_dump(mode="json")
    return results


@app.post("/v1/contract-check")
def contract_check(body: ContractCheckRequest):
    """Demo (b): feed a deliberately malformed/unexpected provider payload
    through the real adapter normalization logic and show it being repaired
    or cleanly rejected with a structured error."""
    router = get_router()
    adapter = router.providers.get(body.provider)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"unknown provider '{body.provider}'")
    request = ChatRequest(
        messages=[ChatMessage(role=MessageRole.user, content=body.prompt)],
        model=body.provider,
    )
    try:
        response = adapter.normalize_external(body.raw_payload, request)
    except SchemaContractError as exc:
        return {
            "accepted": False,
            "error": exc.to_gateway_error().model_dump(mode="json"),
        }
    return {"accepted": True, "response": response.model_dump(mode="json")}


@app.post("/admin/fault-inject")
def fault_inject(body: FaultInjectRequest) -> dict:
    fault_injector.set_fault(body.provider, body.mode)
    return {"provider": body.provider, "mode": body.mode}


@app.get("/admin/fault-inject")
def fault_inject_status() -> dict:
    return fault_injector.snapshot()


@app.post("/admin/fault-clear")
def fault_clear(provider: str | None = None) -> dict:
    fault_injector.clear(provider)
    return {"cleared": provider or "all"}


@app.get("/admin/circuit-breakers")
def circuit_breakers() -> dict:
    router = get_router()
    return router.breaker_snapshot()


@app.post("/admin/circuit-breakers/reset")
def circuit_breakers_reset() -> dict:
    router = get_router()
    for breaker in router.breakers.values():
        breaker.reset()
    fault_injector.clear()
    return {"status": "reset"}


@app.get("/v1/session/{session_id}")
def get_session(session_id: str) -> dict:
    router = get_router()
    record = router.sessions.get_or_create(session_id)
    return {
        "session_id": session_id,
        "messages": [m.model_dump() for m in record.messages],
        "served_by": record.served_by,
    }


@app.get("/v1/providers")
def list_providers() -> dict:
    router = get_router()
    return {"providers": list(router.providers.keys()), "failover_chains": router.failover_chains}


class JuryEvalRequest(BaseModel):
    message: str
    model_a: str = "mock-claude"
    model_b: str = "mock-gpt"

    model_config = {"protected_namespaces": ()}


@app.post("/v1/evaluate")
def evaluate(body: JuryEvalRequest) -> dict:
    """Cross-provider jury evaluation: send the same prompt to two backends
    and score their response equivalence with the deterministic judge panel."""
    router = get_router()
    jury = get_jury()
    req_a = ChatRequest(
        messages=[ChatMessage(role=MessageRole.user, content=body.message)], model=body.model_a
    )
    req_b = ChatRequest(
        messages=[ChatMessage(role=MessageRole.user, content=body.message)], model=body.model_b
    )
    result_a = router.route(req_a, use_cache=False)
    result_b = router.route(req_b, use_cache=False)
    verdict = jury.evaluate(body.message, result_a.response, result_b.response)
    return {
        "response_a": result_a.response.model_dump(mode="json"),
        "response_b": result_b.response.model_dump(mode="json"),
        "verdict": verdict.model_dump(mode="json"),
    }
