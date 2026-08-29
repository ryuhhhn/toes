from __future__ import annotations

import numpy as np
from openai import AsyncOpenAI

from app.embeddings.base import EmbedKind, EmbeddingUnavailable, l2_normalise


class OpenAIEmbeddingProvider:
    """Escape hatch for deployments where running Ollama alongside the API is impractical."""

    def __init__(self, *, api_key: str, model: str, timeout: float = 20.0, batch_size: int = 64):
        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout)
        self.model = model
        self._batch_size = batch_size

    async def embed(self, texts: list[str], *, kind: EmbedKind = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), self._batch_size):
                batch = texts[start : start + self._batch_size]
                response = await self._client.embeddings.create(model=self.model, input=batch)
                vectors.extend(item.embedding for item in response.data)
        except Exception as exc:  # noqa: BLE001 - degrade rather than crash a search
            raise EmbeddingUnavailable(f"OpenAI embeddings failed: {exc}") from exc

        return l2_normalise(np.asarray(vectors, dtype=np.float32))

    async def healthy(self) -> bool:
        try:
            await self.embed(["health check"], kind="query")
            return True
        except EmbeddingUnavailable:
            return False
