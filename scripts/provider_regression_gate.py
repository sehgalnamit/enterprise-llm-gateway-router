"""CI regression gate: sends a fixed evaluation set of prompts to 2+
providers and fails the build (non-zero exit) if cross-provider response
equivalence drops below a configured threshold.

As of this revision, the gate runs TypeSafe Jev's confidence-gated cascade
(`evaluator.jev_gate.ConfidenceGatedCascade`) FIRST for every prompt pair --
three fast typed questions (schema conformance, no hallucinated fields,
semantic equivalence) answered offline by `LocalJevStandIn`. Only when Jev's
own confidence is below the CI-appropriate bar (`CI_REGRESSION_THRESHOLDS`,
deliberately stricter than the production-monitoring preset -- a
merge-blocking decision is higher-stakes than a single live request) does
the gate escalate to the slower, more expensive LLM-jury
(`evaluator.jury.Jury`) for a textual rationale. This mirrors TypeSafe's own
documented "confidence-gated routing" pattern
(https://docs.typesafe.ai/patterns/confidence-routing): previously, this
script always invoked the jury for every case; now the jury is reserved for
genuinely ambiguous/borderline cases, with Jev resolving the clear-cut ones
directly.

Intended to be wired into CI (see `.github/workflows/ci.yml`) as a guard
against a provider adapter regression silently producing contract-valid but
substantively broken/divergent output.

Run: python scripts/provider_regression_gate.py [--threshold 0.4]
Fully offline -- uses MockProvider personas + LocalJevStandIn, no
network/API keys required. (A real TYPESAFE_API_KEY would transparently
upgrade the fast path to the real hosted Jev model via `TypeSafeJevClient`;
see evaluator/jev_gate.py.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator.jev_gate import CI_REGRESSION_THRESHOLDS, ConfidenceGatedCascade  # noqa: E402
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
    cascade = ConfidenceGatedCascade(jury=jury, thresholds=CI_REGRESSION_THRESHOLDS)

    all_passed = True
    jev_resolved = 0
    jury_escalations = 0
    values: list[float] = []

    def run_case(label: str, provider_a_name: str, provider_b_name: str, prompt: str) -> None:
        nonlocal all_passed, jev_resolved, jury_escalations
        provider_a = MockProvider(persona=provider_a_name, simulate_latency=False)
        provider_b = MockProvider(persona=provider_b_name, simulate_latency=False)
        request = ChatRequest(messages=[ChatMessage(role=MessageRole.user, content=prompt)])
        response_a = provider_a.chat(request)
        response_b = provider_b.chat(request)
        verdict = cascade.evaluate(prompt, response_a, response_b)
        values.append(verdict.aggregate_value)

        if verdict.routed_to_jury:
            jury_escalations += 1
            path_label = (
                f"JURY (jev_confidence={verdict.aggregate_confidence:.2f}, "
                f"band={verdict.confidence_band})"
            )
        else:
            jev_resolved += 1
            path_label = f"JEV-ONLY (confidence={verdict.aggregate_confidence:.2f})"

        status = "PASS" if verdict.passed else "FAIL"
        print(
            f"  [{status}] ({label}) via {path_label}  value={verdict.aggregate_value:.2f}  "
            f"prompt={prompt!r}"
        )
        if not verdict.passed:
            all_passed = False
            print(f"         rationale: {verdict.rationale}")

    # --- Section 1: per-provider self-consistency smoke check -----------------
    # Same provider, same prompt, compared against itself. Expected to be
    # HIGH confidence (identical content -> identical schema/no-hallucination/
    # equivalence signals), so this section should resolve entirely via Jev's
    # fast path. If a provider adapter regresses into non-determinism/flakiness,
    # this is what would catch it cheaply, without waiting on the jury.
    distinct_providers = sorted({p for pair in PROVIDER_PAIRS for p in pair})
    print("### Section 1: per-provider self-consistency (expected: Jev fast path) ###")
    for provider_name in distinct_providers:
        run_case("self-consistency", provider_name, provider_name, EVAL_SET[0])

    # --- Section 2: cross-provider response equivalence ------------------------
    # The harder, genuinely ambiguous judgment this gate exists for: does
    # provider B still answer equivalently to provider A? Deliberately
    # higher-stakes (regression-blocking), so the gate is conservative about
    # trusting Jev alone here -- most/all cases are expected to escalate to
    # the jury for a textual rationale, and that is the INTENDED behavior of
    # confidence-gated routing under a strict CI threshold, not a bug.
    for provider_a_name, provider_b_name in PROVIDER_PAIRS:
        print(
            f"\n### Section 2: cross-provider equivalence: "
            f"{provider_a_name} vs {provider_b_name} ###"
        )
        for prompt in EVAL_SET:
            run_case("cross-provider", provider_a_name, provider_b_name, prompt)

    mean_value = sum(values) / len(values) if values else 0.0
    total = jev_resolved + jury_escalations
    print(
        f"\nResolved {jev_resolved}/{total} cases via Jev alone (fast path); "
        f"escalated {jury_escalations}/{total} to the jury (fallback path)."
    )
    print(f"Overall mean aggregate value: {mean_value:.3f} (threshold={threshold})")
    return all_passed and mean_value >= threshold


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
