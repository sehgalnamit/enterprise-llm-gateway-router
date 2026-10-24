"""End-to-end FastAPI endpoint tests exercising the three demo scenarios."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.dependencies import reset_singletons
from app.main import app
from providers.fault_injection import fault_injector


@pytest.fixture(autouse=True)
def _reset_app_state():
    reset_singletons()
    fault_injector.clear()
    yield
    reset_singletons()
    fault_injector.clear()


@pytest.fixture
def client():
    return TestClient(app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_chat_multi_returns_identical_contract_shape_for_every_backend(client):
    """Demo (a): one request, 2+ backends, identical canonical shape."""
    resp = client.post(
        "/v1/chat/multi",
        json={
            "message": "What is the capital of France?",
            "models": ["mock-claude", "mock-gpt", "local-qwen"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"mock-claude", "mock-gpt", "local-qwen"}
    contract_keys = {
        "request_id",
        "model_used",
        "provider",
        "content",
        "finish_reason",
        "usage",
        "latency_ms",
        "cache_hit",
    }
    for payload in body.values():
        assert contract_keys <= set(payload.keys())


def test_contract_check_repairs_inconsistent_usage(client):
    resp = client.post(
        "/v1/contract-check",
        json={
            "provider": "mock-gpt",
            "prompt": "demo prompt",
            "raw_payload": {
                "choices": [
                    {"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 999},
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["response"]["usage"]["total_tokens"] == 5


def test_contract_check_rejects_malformed_payload(client):
    """Demo (b): a deliberately malformed provider response is rejected with
    a clear structured error."""
    resp = client.post(
        "/v1/contract-check",
        json={"provider": "mock-gpt", "prompt": "demo prompt", "raw_payload": {"choices": []}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["error"]["error_type"] == "schema_violation"
    assert body["error"]["provider"] == "mock-gpt"


def test_fault_injection_and_failover_end_to_end(client):
    """Demo (c): inject a 429/outage on the primary, watch the breaker trip
    via /admin/circuit-breakers, and see the session fail over mid-conversation
    with zero dropped context."""
    session_id = "api-session-1"

    r1 = client.post(
        "/v1/chat", json={"session_id": session_id, "message": "turn one", "model": "mock-claude"}
    )
    assert r1.json()["provider"] == "mock-claude"

    client.post("/admin/fault-inject", json={"provider": "mock-claude", "mode": "outage"})

    # Breaker config default failure_threshold=3 -> three failing calls trip it.
    providers_hit = []
    for i in range(3):
        resp = client.post(
            "/v1/chat",
            json={"session_id": session_id, "message": f"turn {i + 2}", "model": "mock-claude"},
        )
        providers_hit.append(resp.json()["provider"])

    assert all(p == "mock-gpt" for p in providers_hit)

    breakers = client.get("/admin/circuit-breakers").json()
    assert breakers["mock-claude"]["state"] == "open"

    session = client.get(f"/v1/session/{session_id}").json()
    user_messages = [m["content"] for m in session["messages"] if m["role"] == "user"]
    assert user_messages == ["turn one", "turn 2", "turn 3", "turn 4"]
    assert len(session["messages"]) == 8  # 4 user + 4 assistant turns, nothing dropped


def test_evaluate_endpoint_returns_jury_verdict(client):
    resp = client.post(
        "/v1/evaluate",
        json={
            "message": "What is the capital of France?",
            "model_a": "mock-claude",
            "model_b": "mock-gpt",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "verdict" in body
    assert 0.0 <= body["verdict"]["aggregate_score"] <= 1.0
