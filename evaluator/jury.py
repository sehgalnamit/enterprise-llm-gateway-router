"""Jury aggregation: run every configured judge and combine votes into a
single structured `JuryVerdict`.

Aggregate method is a simple mean of per-judge scores (`aggregate_method =
"mean"`), gated additionally by a majority-pass rule so a handful of
borderline-but-passing scores cannot offset one judge screaming "contract
violation". This mirrors the panel-of-small-judges pattern used elsewhere in
this project: several cheap, independent, narrow signals outvote any single
judge's blind spot, at a fraction of the cost of one large LLM-as-judge call.
"""

from __future__ import annotations

import math

from evaluator.judges import BaseJudge, default_judges
from evaluator.schemas import JudgeVote, JuryVerdict
from schemas.contracts import ChatResponse

DEFAULT_SCORE_THRESHOLD = 0.4


class Jury:
    def __init__(
        self,
        judges: list[BaseJudge] | None = None,
        *,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        aggregate_method: str = "mean",
    ) -> None:
        self.judges = judges or default_judges()
        self.score_threshold = score_threshold
        self.aggregate_method = aggregate_method

    def evaluate(
        self, prompt: str, response_a: ChatResponse, response_b: ChatResponse
    ) -> JuryVerdict:
        votes: list[JudgeVote] = [
            judge.evaluate(prompt, response_a, response_b) for judge in self.judges
        ]
        aggregate_score = sum(v.score for v in votes) / len(votes) if votes else 0.0
        passed_votes = sum(1 for v in votes if v.passed)
        majority_needed = math.ceil(len(votes) / 2)
        passed = aggregate_score >= self.score_threshold and passed_votes >= majority_needed

        rationale = (
            f"{passed_votes}/{len(votes)} judges passed; aggregate_score="
            f"{aggregate_score:.2f} (threshold={self.score_threshold}) via "
            f"'{self.aggregate_method}' aggregation over judges "
            f"{[v.judge_name for v in votes]}"
        )
        return JuryVerdict(
            prompt=prompt,
            provider_a=response_a.provider,
            provider_b=response_b.provider,
            votes=votes,
            aggregate_score=aggregate_score,
            aggregate_method=self.aggregate_method,
            passed=passed,
            rationale=rationale,
        )


def default_jury() -> Jury:
    return Jury()
