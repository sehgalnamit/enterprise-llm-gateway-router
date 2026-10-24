"""Structured verdict schemas produced by the LLM-jury evaluator."""

from __future__ import annotations

from pydantic import BaseModel, Field


class JudgeVote(BaseModel):
    judge_name: str
    score: float = Field(ge=0.0, le=1.0)
    passed: bool
    rationale: str


class JuryVerdict(BaseModel):
    prompt: str
    provider_a: str
    provider_b: str
    votes: list[JudgeVote]
    aggregate_score: float = Field(ge=0.0, le=1.0)
    aggregate_method: str
    passed: bool
    rationale: str
