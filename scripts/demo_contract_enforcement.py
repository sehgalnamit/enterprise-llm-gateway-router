"""Demo (b): strict contract enforcement.

Feeds a deliberately malformed/unexpected provider payload through the real
adapter normalization logic and shows:
  1. A repairable defect (inconsistent usage totals) being silently fixed.
  2. An irreparable defect (no content anywhere in the payload) being
     cleanly rejected with a structured `GatewayError`.

Run: python scripts/demo_contract_enforcement.py
Fully offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.mock import MockProvider  # noqa: E402
from schemas.contracts import (  # noqa: E402
    ChatMessage,
    ChatRequest,
    MessageRole,
    SchemaContractError,
)

REQUEST = ChatRequest(messages=[ChatMessage(role=MessageRole.user, content="demo prompt")])


def repairable_case() -> None:
    print("--- Case 1: repairable defect (inconsistent usage totals) ---")
    provider = MockProvider(persona="mock-gpt", simulate_latency=False)
    malformed = {
        "choices": [
            {"message": {"role": "assistant", "content": "Paris."}, "finish_reason": "stop"}
        ],
        # total_tokens deliberately wrong -- the contract trusts prompt+completion.
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 9999},
    }
    response = provider.normalize_external(malformed, REQUEST)
    print("Repaired response accepted:")
    print(json.dumps(response.model_dump(mode="json"), indent=2, default=str))
    assert response.usage.total_tokens == 5, "repair logic should trust prompt+completion sum"
    print()


def irreparable_case() -> None:
    print("--- Case 2: irreparable defect (no content anywhere) ---")
    provider = MockProvider(persona="mock-gpt", simulate_latency=False)
    malformed = {"object": "chat.completion", "choices": []}
    try:
        provider.normalize_external(malformed, REQUEST)
    except SchemaContractError as exc:
        error = exc.to_gateway_error()
        print("Rejected with structured GatewayError:")
        print(json.dumps(error.model_dump(mode="json"), indent=2, default=str))
    else:
        raise AssertionError("expected SchemaContractError to be raised")


def main() -> None:
    repairable_case()
    irreparable_case()


if __name__ == "__main__":
    main()
