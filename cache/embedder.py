"""Lightweight, deterministic embedding for semantic cache similarity.

For a self-contained, network-free demo we implement a reproducible
hash-based bag-of-words embedding rather than pulling in a heavy
sentence-transformers model. It is intentionally simple: tokens are hashed
into a fixed-dimensional vector (a classic "hashing trick" / feature
hashing), then L2-normalized so cosine similarity behaves sensibly.

This is explicitly documented as a fallback embedder (see README "Honest
limitations"). The `Embedder` protocol makes it trivial to swap in a real
embedding model (e.g. `sentence-transformers`) in a production deployment
without touching any cache/router code.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Deterministic, dependency-light fallback embedder."""

    def __init__(self, dimensions: int = 256) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        vector = np.zeros(self.dimensions, dtype=np.float64)
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            return vector.tolist()
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        return vector.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)
