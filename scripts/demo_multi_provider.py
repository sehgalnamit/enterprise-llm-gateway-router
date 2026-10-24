"""Demo (a): send an IDENTICAL natural-language chat request to 2+ backend
"models" and show the identical canonical Pydantic payload shape each one
produces, proving contract normalization despite completely different raw
upstream shapes (Anthropic-style, OpenAI-style, Ollama-style).

Run: python scripts/demo_multi_provider.py
Fully offline -- uses only MockProvider personas.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.mock import MockProvider  # noqa: E402
from schemas.contracts import ChatMessage, ChatRequest, MessageRole  # noqa: E402

PROMPT = "What is the capital of France?"
PERSONAS = ["mock-claude", "mock-gpt", "local-qwen"]


def main() -> None:
    print(f'Sending identical prompt to {len(PERSONAS)} backends: "{PROMPT}"\n')
    request = ChatRequest(messages=[ChatMessage(role=MessageRole.user, content=PROMPT)])

    for persona in PERSONAS:
        provider = MockProvider(persona=persona, simulate_latency=False)
        response = provider.chat(request)
        print(f"=== {persona} ===")
        print(json.dumps(response.model_dump(mode="json"), indent=2, default=str))
        print()

    print(
        "All three backends returned the SAME canonical ChatResponse contract "
        "(request_id, model_used, provider, content, finish_reason, usage, "
        "latency_ms, cache_hit) despite each adapter normalizing a different "
        "raw upstream shape (Anthropic messages / OpenAI chat-completions / "
        "Ollama /api/chat)."
    )


if __name__ == "__main__":
    main()
