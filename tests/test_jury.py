"""Jury aggregation tests."""

from __future__ import annotations

from evaluator.judges import ContractConformanceJudge
from evaluator.jury import Jury
from providers.mock import MockProvider
from schemas.contracts import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FinishReason,
    MessageRole,
    Usage,
)


def _request(prompt: str) -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role=MessageRole.user, content=prompt)])


def test_jury_scores_two_providers_answering_same_prompt_as_equivalent():
    prompt = "What is the capital of France?"
    response_a = MockProvider(persona="mock-claude", simulate_latency=False).chat(_request(prompt))
    response_b = MockProvider(persona="mock-gpt", simulate_latency=False).chat(_request(prompt))

    jury = Jury()
    verdict = jury.evaluate(prompt, response_a, response_b)

    assert verdict.provider_a == "mock-claude"
    assert verdict.provider_b == "mock-gpt"
    assert 0.0 <= verdict.aggregate_score <= 1.0
    assert verdict.passed is True
    assert len(verdict.votes) == 4


def test_jury_detects_contract_violation_via_error_finish_reason():
    good = ChatResponse(
        request_id="req_1",
        model_used="mock-gpt-4o",
        provider="mock-gpt",
        content="Paris is the capital of France.",
        finish_reason=FinishReason.stop,
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        latency_ms=1.0,
    )
    broken = ChatResponse(
        request_id="req_2",
        model_used="broken-model",
        provider="broken-provider",
        content="Paris is the capital of France.",
        finish_reason=FinishReason.error,
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        latency_ms=1.0,
    )
    judge = ContractConformanceJudge()
    vote = judge.evaluate("prompt", good, broken)
    assert vote.passed is False
    assert "error" in vote.rationale


def test_jury_fails_when_responses_are_completely_unrelated():
    prompt_a = "What is the capital of France?"
    prompt_b = "Write a haiku about quantum entanglement in laboratories"
    response_a = MockProvider(persona="mock-claude", simulate_latency=False).chat(
        _request(prompt_a)
    )
    response_b = MockProvider(persona="mock-gpt", simulate_latency=False).chat(_request(prompt_b))

    jury = Jury(score_threshold=0.9)
    verdict = jury.evaluate(prompt_a, response_a, response_b)
    assert verdict.passed is False
