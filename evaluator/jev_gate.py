"""TypeSafe Jev ("System One") integration: typed, fast, calibrated
decisions as a confidence-gated front door to this repo's existing LLM-jury
evaluator.

Jev is TypeSafe's real, commercially-available "System One" model: instead
of generating text that your code then has to parse, you send a `state` plus
one or more typed `questions` (Noul/Choice/Score) and get back typed answers
with calibrated probabilities -- no text generation, no JSON recovery.
Source of truth for everything in this module (request/response shapes,
confidence formulas): https://docs.typesafe.ai/introduction,
https://docs.typesafe.ai/confidence,
https://docs.typesafe.ai/primitives/noul,
https://docs.typesafe.ai/primitives/choice,
https://docs.typesafe.ai/primitives/score.

Two `JevClient` implementations are provided:

* `TypeSafeJevClient` -- the REAL adapter. Calls
  `POST https://api.typesafe.ai/v1/systemone` directly via `httpx`. Only
  activates when `TYPESAFE_API_KEY` is set; otherwise raises
  `JevUnavailableError`, exactly like this repo's other optional real
  provider adapters (`providers/openai_provider.py`, etc.).
* `LocalJevStandIn` -- a deterministic, network-free, OFFLINE stand-in that
  implements the identical typed contract (same Pydantic answer shapes, the
  exact same confidence formulas reproduced from docs.typesafe.ai/confidence)
  so the rest of this module (`ConfidenceGatedCascade`) and the CI
  regression gate can be fully exercised with zero API keys. It is
  deliberately narrow in scope -- it only knows how to answer the three
  specific response-equivalence questions this repo asks (see
  `QUESTION_SCHEMA_CONFORMANT`, `QUESTION_NO_HALLUCINATED_FIELDS`,
  `QUESTION_SEMANTIC_EQUIVALENCE` below) by deriving deterministic values
  from this repo's existing judges (`evaluator/judges.py`). It is NOT a
  general-purpose reasoning substitute for the real Jev model; swapping in
  the real `TypeSafeJevClient` via `TYPESAFE_API_KEY` is how you'd get actual
  general-purpose typed judgment.

`ConfidenceGatedCascade` implements TypeSafe's own documented
"confidence-gated routing" pattern
(https://docs.typesafe.ai/patterns/confidence-routing): confidence is a
second decision axis, orthogonal to the answer itself. High confidence ->
act on Jev's typed answer directly (fast, cheap -- the right shape for
high-volume live production traffic). Medium/low confidence -> fall back to
a slower, more expensive system that can produce a textual rationale -- here,
this repo's existing `evaluator/jury.py` LLM-jury. TypeSafe's docs are
explicit that threshold boundaries should scale with the stakes of the
decision, not be one global number; see `PRODUCTION_MONITORING_THRESHOLDS`
vs `CI_REGRESSION_THRESHOLDS` below.

The per-question aggregate "health value" (`CascadeVerdict.aggregate_value`)
is a weighted mean of normalized per-question values, in the same spirit as
DeepEval's `JevEval` metric (https://deepeval.com/blog/introducing-jev-as-a-judge):
``JevEval = sum(w_i * v_i) / sum(w_i)``. We cite that formula as the
inspiration for combining several typed Jev questions into one score; the
routing decision itself is governed separately by confidence, not by this
value.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from evaluator.judges import ContractConformanceJudge, KeywordOverlapJudge, SemanticOverlapJudge
from evaluator.jury import Jury, default_jury
from evaluator.schemas import JuryVerdict
from schemas.contracts import ChatResponse

DEFAULT_SYSTEMONE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------


class JevClientError(Exception):
    """Base class for all Jev-client failures."""


class JevUnavailableError(JevClientError):
    """Raised when a Jev client is not configured/usable in this environment
    (e.g. `TYPESAFE_API_KEY` unset) -- a static configuration gap, not a
    transient failure, but callers should treat it as a reason to fall back."""


class JevRequestError(JevClientError):
    """Raised when a real Jev API call fails (network error or non-2xx)."""


# -----------------------------------------------------------------------------
# Typed question specs (request side) -- field names/shapes match
# docs.typesafe.ai/primitives/{noul,choice,score} exactly.
# -----------------------------------------------------------------------------


class NoulCriteria(BaseModel):
    true: str
    false: str


class NoulQuestion(BaseModel):
    type: Literal["noul"] = "noul"
    instructions: str
    criteria: NoulCriteria | None = None


class ChoiceQuestion(BaseModel):
    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str]


class ScoreQuestion(BaseModel):
    type: Literal["score"] = "score"
    instructions: str
    criteria: list[str] = Field(min_length=2, max_length=10)


JevQuestion = NoulQuestion | ChoiceQuestion | ScoreQuestion


# -----------------------------------------------------------------------------
# Typed answers (response side) -- shapes match the real API's documented
# JSON responses exactly (https://docs.typesafe.ai/primitives/*#response-structure).
# -----------------------------------------------------------------------------


class NoulAnswer(BaseModel):
    type: Literal["noul"] = "noul"
    noul: float = Field(ge=0.0, le=1.0)


class ChoiceAnswer(BaseModel):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)


class ScoreAnswer(BaseModel):
    type: Literal["score"] = "score"
    score: float
    probabilities: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)
    legend: dict[str, str] | None = None


JevAnswer = NoulAnswer | ChoiceAnswer | ScoreAnswer


def _parse_answer(raw: dict[str, Any]) -> JevAnswer:
    answer_type = raw.get("type")
    if answer_type == "noul":
        return NoulAnswer.model_validate(raw)
    if answer_type == "choice":
        return ChoiceAnswer.model_validate(raw)
    if answer_type == "score":
        return ScoreAnswer.model_validate(raw)
    raise JevRequestError(f"unrecognized Jev answer type: {answer_type!r}")


# -----------------------------------------------------------------------------
# Confidence + score formulas, reproduced EXACTLY from
# https://docs.typesafe.ai/confidence ("How confidence is calculated").
# These are the real, published formulas -- not our own invention. Keeping
# them as standalone functions means both `LocalJevStandIn` (which must
# compute them itself) and anyone validating a real `TypeSafeJevClient`
# response (which already includes `confidence` from the server) can use the
# identical, independently-verifiable math.
# -----------------------------------------------------------------------------


def noul_confidence(p: float) -> float:
    """confidence = |2p - 1|. 0 at p=0.5 (maximally unsure), 1 at p=0 or p=1.
    The real Noul API does not return this field -- it's a derived
    convenience so Nouls can be gated on the same 0-1 scale as Choice/Score.
    """
    return abs(2 * p - 1)


def choice_confidence(probabilities: list[float]) -> float:
    """confidence = (p_max - 1/n) / (1 - 1/n), equivalently (n*p_max - 1)/(n-1)."""
    n = len(probabilities)
    if n <= 1:
        return 1.0
    p_max = max(probabilities)
    return (p_max - 1 / n) / (1 - 1 / n)


def score_value(probabilities: list[float]) -> float:
    """score = sum(i * p_i) for ordered levels i = 0..n-1."""
    return sum(i * p for i, p in enumerate(probabilities))


def score_confidence(probabilities: list[float]) -> float:
    """Reference implementation reproduced verbatim from
    docs.typesafe.ai/confidence ('Score' section)."""
    n = len(probabilities)
    if n <= 1:
        return 1.0
    m = probabilities.index(max(probabilities))
    spread = sum(p * abs(i - m) for i, p in enumerate(probabilities))
    even_spread = sum(abs(i - (n - 1) / 2) for i in range(n)) / n
    if even_spread == 0:
        return 1.0
    return max(0.0, 1 - spread / even_spread)


# -----------------------------------------------------------------------------
# JevClient abstraction
# -----------------------------------------------------------------------------


class JevClient(ABC):
    @abstractmethod
    def ask(
        self, state: Any, questions: dict[str, JevQuestion]
    ) -> dict[str, JevAnswer]:
        """Send `state` plus `questions` (keyed by caller-chosen id) to Jev
        and return one `JevAnswer` per question id."""


class TypeSafeJevClient(JevClient):
    """Real adapter for TypeSafe's hosted Jev System One model.

    Calls ``POST https://api.typesafe.ai/v1/systemone`` directly via
    ``httpx`` -- functionally equivalent to, and interchangeable with, the
    official ``typesafe-sdk`` package's
    ``TypeSafeClient(model=...).system_one(state=..., questions=...)`` call.
    We implement this ourselves with plain ``httpx`` (already a hard
    dependency of this repo) rather than depending on ``typesafe-sdk``
    directly, to keep the default install fully offline-safe; see
    ``requirements-jev.txt`` to install the official SDK as a drop-in
    alternative if you prefer it.

    Only usable when ``TYPESAFE_API_KEY`` is set in the environment --
    otherwise raises `JevUnavailableError`, exactly like this repo's other
    optional real provider adapters.

    NOTE: this adapter was implemented directly against the documented API
    (see module docstring for sources) but has NOT been exercised against
    the live TypeSafe API in this repository's test/CI environment, since no
    `TYPESAFE_API_KEY` was available when this was built. Only
    `LocalJevStandIn` is exercised by the test suite and CI.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        endpoint: str = DEFAULT_SYSTEMONE_ENDPOINT,
        timeout_s: float = 30.0,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        self.endpoint = endpoint
        self.timeout_s = timeout_s

    def ask(
        self, state: Any, questions: dict[str, JevQuestion]
    ) -> dict[str, JevAnswer]:
        if not self.api_key:
            raise JevUnavailableError(
                "TYPESAFE_API_KEY not configured; TypeSafe Jev provider is "
                "disabled in this environment"
            )
        payload = {
            "state": state,
            "model": self.model,
            "questions": {
                qid: question.model_dump(exclude_none=True)
                for qid, question in questions.items()
            },
        }
        try:
            resp = httpx.post(
                self.endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout_s,
            )
        except httpx.RequestError as exc:
            raise JevRequestError(f"TypeSafe Jev request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise JevRequestError(
                f"TypeSafe Jev returned HTTP {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json()
        answers_raw = body.get("answers", {})
        return {qid: _parse_answer(raw) for qid, raw in answers_raw.items()}


# -----------------------------------------------------------------------------
# LocalJevStandIn: deterministic, offline, contract-compatible stand-in
# -----------------------------------------------------------------------------

#: Known question ids this repo's cascade asks. LocalJevStandIn only knows
#: how to answer these -- see class docstring for why that's a deliberate
#: scope limit rather than an oversight.
QUESTION_SCHEMA_CONFORMANT = "schema_conformant"
QUESTION_NO_HALLUCINATED_FIELDS = "no_hallucinated_fields"
QUESTION_SEMANTIC_EQUIVALENCE = "semantic_equivalence"

EQUIVALENCE_LEVELS = [
    "not equivalent -- different topic or contradicts the baseline",
    "partially equivalent -- same topic, diverges in substance",
    "mostly equivalent -- same substance, differs only in phrasing/detail",
    "fully equivalent -- same meaning and completeness as the baseline",
]


def _triangular_distribution(center: float, n: int, spread: float = 1.4) -> list[float]:
    """Synthesize a deterministic probability vector over `n` ordered levels,
    peaked at the (possibly fractional) index `center` with linearly
    decaying mass out to +/- `spread` levels away.

    This is OUR OWN heuristic (not part of the real TypeSafe API) used only
    by `LocalJevStandIn` to turn a continuous similarity signal into a
    plausible-looking probability distribution so the real `score_value` /
    `score_confidence` formulas have something to operate on offline. The
    default `spread=1.4` is deliberately > 1 level so neighboring levels
    always retain some probability mass (avoiding a spuriously "fully
    confident" delta spike whenever `center` happens to land on an exact
    integer index) -- see `_semantic_equivalence_answer` for how `spread` is
    additionally widened when the two underlying similarity signals
    disagree with each other. The real hosted Jev model would return actual
    model-calibrated probabilities, not a triangular kernel.
    """
    weights = [max(0.0, 1.0 - abs(k - center) / max(spread, 1e-6)) for k in range(n)]
    total = sum(weights)
    if total <= 0:
        return [1.0 / n] * n
    return [w / total for w in weights]


class LocalJevStandIn(JevClient):
    """Deterministic, network-free stand-in for the real TypeSafe Jev model.

    Implements the IDENTICAL `JevClient` contract (same Pydantic answer
    types, the same published confidence formulas) so `ConfidenceGatedCascade`
    and `scripts/provider_regression_gate.py` can run fully offline with zero
    API keys. It is intentionally scoped to exactly the three questions this
    repo's cascade asks about a pair of canonical `ChatResponse` objects
    (`state = {"prompt": ..., "response_a": {...}, "response_b": {...}}`):
    it is NOT a general-purpose reasoning substitute, and raises
    `JevClientError` for any other question id or state shape rather than
    silently guessing. Swap in `TypeSafeJevClient` (real `TYPESAFE_API_KEY`)
    for actual general-purpose typed judgment.

    Per-question values are derived deterministically from this repo's
    existing deterministic judges (`evaluator/judges.py`), so the same input
    always produces the exact same output -- no randomness anywhere.
    """

    def ask(
        self, state: Any, questions: dict[str, JevQuestion]
    ) -> dict[str, JevAnswer]:
        if not isinstance(state, dict) or not {"prompt", "response_a", "response_b"} <= set(
            state
        ):
            raise JevClientError(
                "LocalJevStandIn only understands "
                "state={'prompt': str, 'response_a': dict, 'response_b': dict} "
                "-- this is a scoped stand-in for this repo's response-"
                "equivalence cascade, not a general-purpose Jev substitute."
            )
        prompt = state["prompt"]
        raw_a, raw_b = state["response_a"], state["response_b"]
        response_a = ChatResponse.model_validate(raw_a)
        response_b = ChatResponse.model_validate(raw_b)

        answers: dict[str, JevAnswer] = {}
        for qid, question in questions.items():
            if qid == QUESTION_SCHEMA_CONFORMANT:
                answers[qid] = NoulAnswer(noul=self._schema_conformant_noul(response_a, response_b))
            elif qid == QUESTION_NO_HALLUCINATED_FIELDS:
                answers[qid] = NoulAnswer(
                    noul=self._no_hallucinated_fields_noul(raw_a, raw_b)
                )
            elif qid == QUESTION_SEMANTIC_EQUIVALENCE:
                if not isinstance(question, ScoreQuestion):
                    raise JevClientError(
                        f"question id {qid!r} must be a ScoreQuestion for LocalJevStandIn"
                    )
                answers[qid] = self._semantic_equivalence_answer(
                    prompt, response_a, response_b, question
                )
            else:
                raise JevClientError(
                    f"LocalJevStandIn does not know how to answer question id "
                    f"{qid!r} -- it is scoped to {QUESTION_SCHEMA_CONFORMANT!r}, "
                    f"{QUESTION_NO_HALLUCINATED_FIELDS!r}, and "
                    f"{QUESTION_SEMANTIC_EQUIVALENCE!r} only. Use TypeSafeJevClient "
                    "for general-purpose questions."
                )
        return answers

    @staticmethod
    def _schema_conformant_noul(response_a: ChatResponse, response_b: ChatResponse) -> float:
        # Reuses this repo's existing deterministic contract-conformance
        # signal (evaluator/judges.py) rather than duplicating its logic.
        vote = ContractConformanceJudge().evaluate("", response_a, response_b)
        return vote.score

    @staticmethod
    def _no_hallucinated_fields_noul(raw_a: dict[str, Any], raw_b: dict[str, Any]) -> float:
        allowed = set(ChatResponse.model_fields.keys())
        extras = (set(raw_a) - allowed) | (set(raw_b) - allowed)
        if not extras:
            return 1.0
        return max(0.0, 1.0 - 0.5 * len(extras))

    @staticmethod
    def _semantic_equivalence_answer(
        prompt: str, response_a: ChatResponse, response_b: ChatResponse, question: ScoreQuestion
    ) -> ScoreAnswer:
        keyword_vote = KeywordOverlapJudge().evaluate(prompt, response_a, response_b)
        semantic_vote = SemanticOverlapJudge().evaluate(prompt, response_a, response_b)
        similarity = (keyword_vote.score + semantic_vote.score) / 2.0
        # When the two underlying similarity signals disagree with each
        # other, that disagreement is itself a signal of uncertainty -- widen
        # the kernel (flatter distribution -> lower confidence) accordingly.
        disagreement = abs(keyword_vote.score - semantic_vote.score)
        spread = 1.4 + 1.2 * disagreement

        levels = question.criteria
        n = len(levels)
        center = similarity * (n - 1)
        probabilities = _triangular_distribution(center, n, spread=spread)
        return ScoreAnswer(
            score=score_value(probabilities),
            probabilities={str(i): p for i, p in enumerate(probabilities)},
            confidence=score_confidence(probabilities),
            legend={str(i): levels[i] for i in range(n)},
        )


# -----------------------------------------------------------------------------
# Confidence-gated cascade: Jev fast path, Jury fallback
# -----------------------------------------------------------------------------


@dataclass
class CascadeThresholds:
    """Confidence-band boundaries for routing decisions.

    TypeSafe's own guidance (docs.typesafe.ai/confidence,
    docs.typesafe.ai/patterns/confidence-routing) is explicit that these
    should scale with the stakes of the action, not be one global number.
    `high` is the floor above which Jev's own typed answer is trusted
    directly; below it (down to `low`, and below `low`) the cascade falls
    back to the jury. We collapse "medium" and "low" into the same fallback
    action (ask the jury) but still report the distinct band for
    observability -- see `CascadeVerdict.confidence_band`.
    """

    high: float = 0.6
    low: float = 0.3


#: Fast, high-volume live traffic: trust Jev more readily (lower bar) so the
#: jury (slow/expensive) is only invoked for genuinely ambiguous cases.
PRODUCTION_MONITORING_THRESHOLDS = CascadeThresholds(high=0.6, low=0.3)

#: CI/CD regression gate: a merge-blocking decision is higher-stakes than a
#: single production request, so we deliberately require noticeably higher
#: Jev confidence before skipping the jury's textual rationale than the
#: production-monitoring preset uses. In practice (see
#: `scripts/provider_regression_gate.py`), this lets confidently-identical
#: "did this provider regress against itself" self-consistency checks
#: resolve via Jev alone, while still sending every genuinely harder
#: cross-provider equivalence judgment to the jury for a rationale.
CI_REGRESSION_THRESHOLDS = CascadeThresholds(high=0.75, low=0.5)


class CascadeVerdict(BaseModel):
    prompt: str
    provider_a: str
    provider_b: str
    schema_conformant_noul: float
    no_hallucinated_fields_noul: float
    equivalence_score: float
    equivalence_confidence: float
    aggregate_value: float = Field(
        description="JevEval-style weighted mean of normalized per-question values "
        "(inspired by DeepEval's JevEval = sum(w_i*v_i)/sum(w_i))."
    )
    aggregate_confidence: float = Field(
        description="Weakest-link (min) confidence across all answers -- the "
        "routing signal for confidence-gated routing."
    )
    confidence_band: Literal["high", "medium", "low"]
    routed_to_jury: bool
    passed: bool
    rationale: str
    jury_verdict: JuryVerdict | None = None


class ConfidenceGatedCascade:
    """Implements TypeSafe's own documented "confidence-gated routing"
    pattern (docs.typesafe.ai/patterns/confidence-routing) for cross-provider
    response-equivalence checks: Jev answers three fast typed questions;
    confidence decides whether to trust that answer directly (fast path) or
    escalate to this repo's existing LLM-jury for a textual rationale (slow,
    expensive fallback path).
    """

    def __init__(
        self,
        jev_client: JevClient | None = None,
        jury: Jury | None = None,
        thresholds: CascadeThresholds | None = None,
        question_weights: dict[str, float] | None = None,
    ) -> None:
        self.jev_client = jev_client or LocalJevStandIn()
        self.jury = jury or default_jury()
        self.thresholds = thresholds or PRODUCTION_MONITORING_THRESHOLDS
        self.question_weights = question_weights or {
            QUESTION_SCHEMA_CONFORMANT: 1.0,
            QUESTION_NO_HALLUCINATED_FIELDS: 1.0,
            QUESTION_SEMANTIC_EQUIVALENCE: 1.0,
        }

    def _questions(self) -> dict[str, JevQuestion]:
        return {
            QUESTION_SCHEMA_CONFORMANT: NoulQuestion(
                instructions=(
                    "Is the response schema-conformant with the canonical "
                    "ChatResponse contract (non-empty content, a valid "
                    "finish_reason, internally consistent usage totals, and a "
                    "non-empty provider field)?"
                )
            ),
            QUESTION_NO_HALLUCINATED_FIELDS: NoulQuestion(
                instructions=(
                    "Is the response free of any fields that are not part of "
                    "the canonical ChatResponse contract (i.e. no hallucinated "
                    "extra fields)?"
                )
            ),
            QUESTION_SEMANTIC_EQUIVALENCE: ScoreQuestion(
                instructions=(
                    "How semantically equivalent is response_b to the "
                    "baseline response_a, for the same prompt?"
                ),
                criteria=EQUIVALENCE_LEVELS,
            ),
        }

    def _band(self, confidence: float) -> Literal["high", "medium", "low"]:
        if confidence >= self.thresholds.high:
            return "high"
        if confidence >= self.thresholds.low:
            return "medium"
        return "low"

    def evaluate(
        self, prompt: str, response_a: ChatResponse, response_b: ChatResponse
    ) -> CascadeVerdict:
        state = {
            "prompt": prompt,
            "response_a": response_a.model_dump(mode="json"),
            "response_b": response_b.model_dump(mode="json"),
        }
        questions = self._questions()
        answers = self.jev_client.ask(state, questions)

        schema_ans = answers[QUESTION_SCHEMA_CONFORMANT]
        halluc_ans = answers[QUESTION_NO_HALLUCINATED_FIELDS]
        equiv_ans = answers[QUESTION_SEMANTIC_EQUIVALENCE]
        assert isinstance(schema_ans, NoulAnswer)
        assert isinstance(halluc_ans, NoulAnswer)
        assert isinstance(equiv_ans, ScoreAnswer)

        n_levels = len(questions[QUESTION_SEMANTIC_EQUIVALENCE].criteria)  # type: ignore[union-attr]
        equivalence_normalized = equiv_ans.score / max(n_levels - 1, 1)

        confidences = {
            QUESTION_SCHEMA_CONFORMANT: noul_confidence(schema_ans.noul),
            QUESTION_NO_HALLUCINATED_FIELDS: noul_confidence(halluc_ans.noul),
            QUESTION_SEMANTIC_EQUIVALENCE: equiv_ans.confidence,
        }
        aggregate_confidence = min(confidences.values())

        values = {
            QUESTION_SCHEMA_CONFORMANT: schema_ans.noul,
            QUESTION_NO_HALLUCINATED_FIELDS: halluc_ans.noul,
            QUESTION_SEMANTIC_EQUIVALENCE: equivalence_normalized,
        }
        weights = self.question_weights
        aggregate_value = sum(weights[k] * values[k] for k in values) / sum(
            weights[k] for k in values
        )

        band = self._band(aggregate_confidence)
        routed_to_jury = band != "high"

        jury_verdict: JuryVerdict | None = None
        if routed_to_jury:
            jury_verdict = self.jury.evaluate(prompt, response_a, response_b)
            passed = jury_verdict.passed
            rationale = (
                f"Jev confidence band={band} (aggregate_confidence="
                f"{aggregate_confidence:.2f}, high-threshold={self.thresholds.high}). "
                "Escalated to jury fallback per confidence-gated routing "
                "(docs.typesafe.ai/patterns/confidence-routing): "
                f"jury.passed={jury_verdict.passed}, jury.rationale={jury_verdict.rationale}"
            )
        else:
            jev_passed = schema_ans.noul >= 0.5 and halluc_ans.noul >= 0.5 and (
                equivalence_normalized >= 0.5
            )
            passed = jev_passed
            rationale = (
                f"Jev confidence band=high (aggregate_confidence="
                f"{aggregate_confidence:.2f} >= {self.thresholds.high}); trusted "
                f"directly with no jury call (schema_conformant={schema_ans.noul:.2f}, "
                f"no_hallucinated_fields={halluc_ans.noul:.2f}, "
                f"equivalence={equiv_ans.score:.2f}/{n_levels - 1})."
            )

        return CascadeVerdict(
            prompt=prompt,
            provider_a=response_a.provider,
            provider_b=response_b.provider,
            schema_conformant_noul=schema_ans.noul,
            no_hallucinated_fields_noul=halluc_ans.noul,
            equivalence_score=equiv_ans.score,
            equivalence_confidence=equiv_ans.confidence,
            aggregate_value=aggregate_value,
            aggregate_confidence=aggregate_confidence,
            confidence_band=band,
            routed_to_jury=routed_to_jury,
            passed=passed,
            rationale=rationale,
            jury_verdict=jury_verdict,
        )


def default_cascade(thresholds: CascadeThresholds | None = None) -> ConfidenceGatedCascade:
    return ConfidenceGatedCascade(thresholds=thresholds)
