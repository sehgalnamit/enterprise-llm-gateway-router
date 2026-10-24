"""Tests for the TypeSafe Jev integration: confidence-formula correctness
against documented examples, LocalJevStandIn determinism/scoping, and
ConfidenceGatedCascade routing behavior."""

from __future__ import annotations

import pytest

from evaluator.jev_gate import (
    CI_REGRESSION_THRESHOLDS,
    PRODUCTION_MONITORING_THRESHOLDS,
    CascadeThresholds,
    ConfidenceGatedCascade,
    JevClientError,
    LocalJevStandIn,
    NoulQuestion,
    ScoreQuestion,
    choice_confidence,
    noul_confidence,
    score_confidence,
    score_value,
)
from providers.mock import MockProvider
from schemas.contracts import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FinishReason,
    MessageRole,
    Usage,
)


def _req(prompt: str) -> ChatRequest:
    return ChatRequest(messages=[ChatMessage(role=MessageRole.user, content=prompt)])


def _mock_response(persona: str, prompt: str) -> ChatResponse:
    return MockProvider(persona=persona, simulate_latency=False).chat(_req(prompt))


# -----------------------------------------------------------------------------
# Confidence/score formulas -- pinned against exact worked examples published
# at https://docs.typesafe.ai/confidence.
# -----------------------------------------------------------------------------


def test_noul_confidence_matches_documented_formula():
    assert noul_confidence(0.5) == 0.0
    assert noul_confidence(0.0) == 1.0
    assert noul_confidence(1.0) == 1.0
    assert noul_confidence(0.99) == pytest.approx(0.98)


def test_choice_confidence_matches_documented_three_option_example():
    # docs.typesafe.ai/confidence: (0.6, 0.3, 0.1) and (0.6, 0.2, 0.2) both
    # have confidence 0.4 -- only the top probability counts.
    assert choice_confidence([0.6, 0.3, 0.1]) == pytest.approx(0.4)
    assert choice_confidence([0.6, 0.2, 0.2]) == pytest.approx(0.4)


def test_choice_confidence_is_zero_for_uniform_distribution():
    assert choice_confidence([1 / 3, 1 / 3, 1 / 3]) == pytest.approx(0.0, abs=1e-9)


def test_choice_confidence_is_one_for_fully_concentrated_distribution():
    assert choice_confidence([1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_score_value_and_confidence_match_documented_bug_severity_example():
    # docs.typesafe.ai/primitives/score + docs.typesafe.ai/confidence:
    # probabilities (0, 0.57, 0.43) -> score 1.43, confidence ~= 0.35.
    probabilities = [0.0, 0.57, 0.43]
    assert score_value(probabilities) == pytest.approx(1.43)
    assert score_confidence(probabilities) == pytest.approx(0.35, abs=0.01)


def test_score_confidence_matches_documented_three_level_edge_cases():
    # docs.typesafe.ai/confidence: torn between adjacent levels -> 0.25;
    # torn between opposite ends -> 0.0.
    assert score_confidence([0.0, 0.5, 0.5]) == pytest.approx(0.25)
    assert score_confidence([0.5, 0.0, 0.5]) == pytest.approx(0.0, abs=1e-9)


def test_score_confidence_is_one_for_fully_concentrated_distribution():
    assert score_confidence([0.0, 1.0, 0.0]) == pytest.approx(1.0)


# -----------------------------------------------------------------------------
# LocalJevStandIn
# -----------------------------------------------------------------------------


def test_local_jev_standin_is_deterministic():
    client = LocalJevStandIn()
    prompt = "What is the capital of France?"
    response_a = _mock_response("mock-claude", prompt)
    response_b = _mock_response("mock-gpt", prompt)
    state = {
        "prompt": prompt,
        "response_a": response_a.model_dump(mode="json"),
        "response_b": response_b.model_dump(mode="json"),
    }
    questions = {
        "schema_conformant": NoulQuestion(instructions="conformant?"),
        "semantic_equivalence": ScoreQuestion(
            instructions="how equivalent?",
            criteria=["not equivalent", "somewhat", "mostly", "fully"],
        ),
    }
    first = client.ask(state, questions)
    second = client.ask(state, questions)
    assert first["schema_conformant"].noul == second["schema_conformant"].noul
    assert first["semantic_equivalence"].score == second["semantic_equivalence"].score
    assert first["semantic_equivalence"].confidence == second["semantic_equivalence"].confidence


def test_local_jev_standin_rejects_unknown_question_id():
    client = LocalJevStandIn()
    prompt = "hi"
    response = _mock_response("mock-gpt", prompt)
    state = {
        "prompt": prompt,
        "response_a": response.model_dump(mode="json"),
        "response_b": response.model_dump(mode="json"),
    }
    with pytest.raises(JevClientError):
        client.ask(state, {"some_unsupported_question": NoulQuestion(instructions="?")})


def test_local_jev_standin_rejects_unrecognized_state_shape():
    client = LocalJevStandIn()
    with pytest.raises(JevClientError):
        client.ask("just a plain string", {"schema_conformant": NoulQuestion(instructions="?")})


def test_local_jev_standin_detects_contract_violation_in_schema_conformant_noul():
    good = ChatResponse(
        request_id="r1",
        model_used="m",
        provider="mock-gpt",
        content="Paris is the capital of France.",
        finish_reason=FinishReason.stop,
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        latency_ms=1.0,
    )
    broken = ChatResponse(
        request_id="r2",
        model_used="m",
        provider="broken",
        content="Paris is the capital of France.",
        finish_reason=FinishReason.error,
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        latency_ms=1.0,
    )
    client = LocalJevStandIn()
    state = {
        "prompt": "x",
        "response_a": good.model_dump(mode="json"),
        "response_b": broken.model_dump(mode="json"),
    }
    answers = client.ask(state, {"schema_conformant": NoulQuestion(instructions="conformant?")})
    assert answers["schema_conformant"].noul < 1.0


# -----------------------------------------------------------------------------
# ConfidenceGatedCascade routing
# -----------------------------------------------------------------------------


def test_cascade_high_confidence_skips_jury():
    prompt = "What is the capital of France?"
    response_a = _mock_response("mock-claude", prompt)
    response_b = _mock_response("mock-claude", prompt)  # same persona+prompt -> identical
    cascade = ConfidenceGatedCascade(thresholds=PRODUCTION_MONITORING_THRESHOLDS)
    verdict = cascade.evaluate(prompt, response_a, response_b)
    assert verdict.confidence_band == "high"
    assert verdict.routed_to_jury is False
    assert verdict.jury_verdict is None
    assert verdict.passed is True


def test_cascade_ambiguous_confidence_routes_to_jury_under_stricter_ci_thresholds():
    # Cross-provider comparisons (different personas, different phrasing
    # styles) never reach the CI preset's stricter 0.75 confidence bar with
    # this repo's deterministic lexical/hash-embedding similarity proxies --
    # exactly the intended behavior: a merge-blocking regression gate should
    # not trust a cheap heuristic alone for a genuinely harder judgment.
    prompt = "What is the capital of France?"
    response_a = _mock_response("mock-claude", prompt)
    response_b = _mock_response("mock-gpt", prompt)
    cascade = ConfidenceGatedCascade(thresholds=CI_REGRESSION_THRESHOLDS)
    verdict = cascade.evaluate(prompt, response_a, response_b)
    assert verdict.confidence_band != "high"
    assert verdict.routed_to_jury is True
    assert verdict.jury_verdict is not None


def test_cascade_self_consistency_check_clears_even_the_stricter_ci_threshold():
    # A provider-against-itself "did this adapter regress into
    # non-determinism" check is exactly the kind of case confidence-gated
    # routing should resolve via Jev alone even under a strict CI bar.
    prompt = "What is the capital of France?"
    response_a = _mock_response("mock-claude", prompt)
    response_b = _mock_response("mock-claude", prompt)
    cascade = ConfidenceGatedCascade(thresholds=CI_REGRESSION_THRESHOLDS)
    verdict = cascade.evaluate(prompt, response_a, response_b)
    assert verdict.confidence_band == "high"
    assert verdict.routed_to_jury is False
    assert verdict.jury_verdict is None


def test_cascade_routes_different_providers_to_jury_by_default():
    prompt = "What is the capital of France?"
    response_a = _mock_response("mock-claude", prompt)
    response_b = _mock_response("mock-gpt", prompt)
    cascade = ConfidenceGatedCascade(thresholds=PRODUCTION_MONITORING_THRESHOLDS)
    verdict = cascade.evaluate(prompt, response_a, response_b)
    assert verdict.routed_to_jury is True
    assert verdict.jury_verdict is not None
    assert verdict.provider_a == "mock-claude"
    assert verdict.provider_b == "mock-gpt"


def test_cascade_contract_violation_lowers_confidence_and_escalates():
    good = ChatResponse(
        request_id="r1",
        model_used="m",
        provider="mock-gpt",
        content="Paris is the capital of France.",
        finish_reason=FinishReason.stop,
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        latency_ms=1.0,
    )
    broken = ChatResponse(
        request_id="r2",
        model_used="m",
        provider="broken",
        content="Paris is the capital of France.",
        finish_reason=FinishReason.error,
        usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        latency_ms=1.0,
    )
    cascade = ConfidenceGatedCascade(thresholds=PRODUCTION_MONITORING_THRESHOLDS)
    verdict = cascade.evaluate("prompt", good, broken)
    assert verdict.schema_conformant_noul < 1.0
    assert verdict.routed_to_jury is True
    assert verdict.jury_verdict is not None


def test_cascade_thresholds_default_to_production_monitoring():
    cascade = ConfidenceGatedCascade()
    assert cascade.thresholds == PRODUCTION_MONITORING_THRESHOLDS


def test_cascade_thresholds_are_distinct_presets():
    assert CI_REGRESSION_THRESHOLDS.high > PRODUCTION_MONITORING_THRESHOLDS.high
    assert isinstance(CI_REGRESSION_THRESHOLDS, CascadeThresholds)
