"""CI regression gate: sends a fixed evaluation set of prompts to 2+
providers and fails the build (non-zero exit) if jury-scored cross-provider
response equivalence drops below a configured threshold.

Intended to be wired into CI (see `.github/workflows/ci.yml`) as a guard
against a provider adapter regression silently producing contract-valid but
substantively broken/divergent output.

Run: python scripts/provider_regression_gate.py [--threshold 0.4]
Fully offline -- uses MockProvider personas, no network/API keys required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator.jury import Jury  # noqa: E402
from providers.mock import MockProvider  # noqa: E402
from schemas.contracts import ChatMessage, ChatRequest, MessageRole  # noqa: E402

EVAL_SET = [
    "What is the capital of France?",
    "Summarize the plot of Romeo and Juliet in two sentences.",
    "Give me three tips for writing clean Python code.",
    "Explain what a circuit breaker is in software engineering.",
    "What is 17 times 24?",
]

PROVIDER_PAIRS = [("mock-claude", "mock-gpt"), ("mock-gpt", "local-qwen")]


def run_gate(threshold: float) -> bool:
    jury = Jury(score_threshold=threshold)
    all_passed = True
    scores: list[float] = []

    for provider_a_name, provider_b_name in PROVIDER_PAIRS:
        provider_a = MockProvider(persona=provider_a_name, simulate_latency=False)
        provider_b = MockProvider(persona=provider_b_name, simulate_latency=False)
        print(f"\n### Evaluating pair: {provider_a_name} vs {provider_b_name} ###")

        for prompt in EVAL_SET:
            request = ChatRequest(messages=[ChatMessage(role=MessageRole.user, content=prompt)])
            response_a = provider_a.chat(request)
            response_b = provider_b.chat(request)
            verdict = jury.evaluate(prompt, response_a, response_b)
            scores.append(verdict.aggregate_score)
            status = "PASS" if verdict.passed else "FAIL"
            print(f"  [{status}] score={verdict.aggregate_score:.2f}  prompt={prompt!r}")
            if not verdict.passed:
                all_passed = False
                print(f"         rationale: {verdict.rationale}")

    mean_score = sum(scores) / len(scores) if scores else 0.0
    print(f"\nOverall mean jury score: {mean_score:.3f} (threshold={threshold})")
    return all_passed and mean_score >= threshold


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.4,
        help="minimum passing jury aggregate score (default: 0.4)",
    )
    args = parser.parse_args()

    passed = run_gate(args.threshold)
    if not passed:
        print("\nREGRESSION GATE FAILED: cross-provider equivalence below threshold.")
        sys.exit(1)
    print("\nREGRESSION GATE PASSED.")
    sys.exit(0)


if __name__ == "__main__":
    main()
