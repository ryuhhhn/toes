"""Provider-neutral embedding interface.

The `kind` argument is not decoration. Several strong retrieval models are *asymmetric* and
expect different prefixes for queries and documents (nomic-embed-text wants
``search_query:`` / ``search_document:``). Putting that in the interface means swapping the
model cannot silently halve retrieval quality.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

import numpy as np

EmbedKind = Literal["query", "document"]

#: Models that need an asymmetric prefix, keyed by model-name prefix.
ASYMMETRIC_PREFIXES: dict[str, dict[str, str]] = {
    "nomic-embed-text": {"query": "search_query: ", "document": "search_document: "},
    "mxbai-embed-large": {
        "query": "Represent this sentence for searching relevant passages: ",
        "document": "",
    },
    "e5-": {"query": "query: ", "document": "passage: "},
}


def apply_prefix(model: str, texts: list[str], kind: EmbedKind) -> list[str]:
    for key, prefixes in ASYMMETRIC_PREFIXES.items():
        if model.startswith(key):
            prefix = prefixes[kind]
            return [prefix + t for t in texts] if prefix else list(texts)
    return list(texts)


def l2_normalise(matrix: np.ndarray) -> np.ndarray:
    """Normalise rows so cosine similarity is a plain dot product."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class EmbeddingUnavailable(RuntimeError):
    """The embedding backend is unreachable. Callers must degrade, never hang."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    model: str

    async def embed(self, texts: list[str], *, kind: EmbedKind = "document") -> np.ndarray: ...

    async def healthy(self) -> bool: ...
