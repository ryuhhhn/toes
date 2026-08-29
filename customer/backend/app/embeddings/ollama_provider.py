"""Ollama embeddings via ``langchain_ollama``.

LangChain owns the transport and batching; we own the asymmetric prefixes, because
``OllamaEmbeddings`` sends whatever string it is given. ``nomic-embed-text`` is asymmetric
(``search_query:`` / ``search_document:``) and silently loses retrieval quality without them,
so ``apply_prefix`` runs here, before the text reaches LangChain.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import numpy as np
from langchain_ollama import OllamaEmbeddings

from app.embeddings.base import (
    EmbedKind,
    EmbeddingUnavailable,
    apply_prefix,
    l2_normalise,
)

log = logging.getLogger(__name__)


class OllamaEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout: float = 20.0,
        batch_size: int = 32,
    ):
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._batch_size = batch_size

    def _make_client(self) -> OllamaEmbeddings:
        """A client per call, deliberately.

        The provider is cached by get_embedder(), so a long-lived LangChain client would
        bind its async connection pool to whichever event loop happened to construct it
        and then fail on every other one. That surfaces as EmbeddingUnavailable and a
        silent downgrade to filter-only search, which is far harder to notice than a
        crash. Constructing the wrapper is cheap next to a network round trip.
        """
        return OllamaEmbeddings(
            model=self.model,
            base_url=self._base_url,
            client_kwargs={"timeout": self._timeout},
        )

    async def embed(self, texts: list[str], *, kind: EmbedKind = "document") -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        prepared = apply_prefix(self.model, texts, kind)
        vectors: list[list[float]] = []
        client = self._make_client()

        try:
            for start in range(0, len(prepared), self._batch_size):
                batch = prepared[start : start + self._batch_size]
                batch_vectors = await asyncio.wait_for(
                    client.aembed_documents(batch),
                    timeout=self._timeout * max(1, len(batch) / self._batch_size) + self._timeout,
                )
                if not batch_vectors:
                    raise EmbeddingUnavailable(
                        f"Ollama returned no embeddings for model {self.model!r}"
                    )
                vectors.extend(batch_vectors)
        except asyncio.TimeoutError as exc:
            raise EmbeddingUnavailable(
                f"Ollama embedding timed out after {self._timeout}s at {self._base_url}"
            ) from exc
        except EmbeddingUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - a dead Ollama must degrade, never hang a stream
            raise EmbeddingUnavailable(f"Ollama unreachable at {self._base_url}: {exc}") from exc

        return l2_normalise(np.asarray(vectors, dtype=np.float32))

    async def healthy(self) -> bool:
        """Confirms the daemon is up *and* the configured model is actually pulled."""
        try:
            async with httpx.AsyncClient(timeout=min(self._timeout, 5.0)) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                names = {m.get("name", "") for m in response.json().get("models", [])}
                # Ollama reports "nomic-embed-text:latest" for a "nomic-embed-text" request.
                return any(n == self.model or n.startswith(f"{self.model}:") for n in names)
        except (httpx.HTTPError, ValueError):
            return False
