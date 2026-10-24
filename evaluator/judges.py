"""Deterministic judges used by the jury evaluator.

Each judge is intentionally simple, fast, and network-free so the jury runs
in CI with zero external dependencies. The design is "many small, cheap,
independent signals voting" rather than "one large LLM-as-judge call" --
see README for the (deliberately modest, non-fabricated) rationale. Swap in
a judge backed by a real small model by implementing `BaseJudge`.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from cache.embedder import HashEmbedder, cosine_similarity
from evaluator.schemas import JudgeVote
from schemas.contracts import ChatResponse, FinishReason

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


class BaseJudge(ABC):
    name: str

    @abstractmethod
    def evaluate(
        self, prompt: str, response_a: ChatResponse, response_b: ChatResponse
    ) -> JudgeVote: ...


class ContractConformanceJudge(BaseJudge):
    """Checks both responses are well-formed, contract-conformant
    `ChatResponse` instances with no sign of hallucinated/invalid fields
    (empty content, non-stop/error finish reasons, impossible usage, etc.)."""

    name = "contract_conformance"

    def evaluate(
        self, prompt: str, response_a: ChatResponse, response_b: ChatResponse
    ) -> JudgeVote:
        problems: list[str] = []
        for label, resp in (("a", response_a), ("b", response_b)):
            if not resp.content.strip():
                problems.append(f"response_{label} has empty content")
            if resp.finish_reason == FinishReason.error:
                problems.append(f"response_{label} finished with error")
            if resp.usage.total_tokens != resp.usage.prompt_tokens + resp.usage.completion_tokens:
                problems.append(f"response_{label} usage totals inconsistent")
            if not resp.provider:
                problems.append(f"response_{label} missing provider field")
        passed = not problems
        score = 1.0 if passed else max(0.0, 1.0 - 0.25 * len(problems))
        rationale = "both responses conform to the canonical contract" if passed else "; ".join(
            problems
        )
        return JudgeVote(judge_name=self.name, score=score, passed=passed, rationale=rationale)


class LengthRatioJudge(BaseJudge):
    """Proxy for stylistic/quality equivalence: flags wildly mismatched
    response lengths (e.g. one provider truncating or erroring short)."""

    name = "length_ratio"
    min_ratio_to_pass = 0.15

    def evaluate(
        self, prompt: str, response_a: ChatResponse, response_b: ChatResponse
    ) -> JudgeVote:
        len_a, len_b = len(response_a.content.split()), len(response_b.content.split())
        longer, shorter = max(len_a, len_b), min(len_a, len_b)
        ratio = (shorter / longer) if longer > 0 else 0.0
        passed = ratio >= self.min_ratio_to_pass
        rationale = f"word-count ratio shorter/longer = {ratio:.2f} (a={len_a}, b={len_b})"
        return JudgeVote(judge_name=self.name, score=ratio, passed=passed, rationale=rationale)


class KeywordOverlapJudge(BaseJudge):
    """Jaccard token overlap between the two responses, as a cheap lexical
    equivalence signal."""

    name = "keyword_overlap"
    min_score_to_pass = 0.05

    def evaluate(
        self, prompt: str, response_a: ChatResponse, response_b: ChatResponse
    ) -> JudgeVote:
        tokens_a, tokens_b = _tokens(response_a.content), _tokens(response_b.content)
        union = tokens_a | tokens_b
        score = (len(tokens_a & tokens_b) / len(union)) if union else 0.0
        passed = score >= self.min_score_to_pass
        rationale = f"jaccard token overlap = {score:.2f}"
        return JudgeVote(judge_name=self.name, score=score, passed=passed, rationale=rationale)


class SemanticOverlapJudge(BaseJudge):
    """Cosine similarity between hash-embeddings of the two responses, as a
    cheap semantic-equivalence proxy (same embedder used by the semantic
    cache, reused here to avoid adding another dependency)."""

    name = "semantic_overlap"
    min_score_to_pass = 0.05

    def __init__(self) -> None:
        self._embedder = HashEmbedder()

    def evaluate(
        self, prompt: str, response_a: ChatResponse, response_b: ChatResponse
    ) -> JudgeVote:
        emb_a = self._embedder.embed(response_a.content)
        emb_b = self._embedder.embed(response_b.content)
        score = min(1.0, max(0.0, cosine_similarity(emb_a, emb_b)))
        passed = score >= self.min_score_to_pass
        rationale = f"hash-embedding cosine similarity = {score:.2f}"
        return JudgeVote(judge_name=self.name, score=score, passed=passed, rationale=rationale)


def default_judges() -> list[BaseJudge]:
    return [
        ContractConformanceJudge(),
        LengthRatioJudge(),
        KeywordOverlapJudge(),
        SemanticOverlapJudge(),
    ]
