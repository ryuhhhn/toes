from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.embeddings.base import EmbeddingProvider, EmbeddingUnavailable


@lru_cache
def get_embedder() -> EmbeddingProvider:
    settings = get_settings()
    provider = settings.embedding_provider.lower()

    if provider == "ollama":
        from app.embeddings.ollama_provider import OllamaEmbeddingProvider

        return OllamaEmbeddingProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_embed_model,
            timeout=settings.embed_timeout_seconds,
            batch_size=settings.embed_batch_size,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise EmbeddingUnavailable(
                "EMBEDDING_PROVIDER=openai but OPENAI_API_KEY is not set"
            )
        from app.embeddings.openai_provider import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_embed_model,
            timeout=settings.embed_timeout_seconds,
        )

    raise EmbeddingUnavailable(f"unknown EMBEDDING_PROVIDER: {settings.embedding_provider!r}")


def embedding_model_id() -> str:
    """Stamped into index metadata so a model change forces a rebuild."""
    settings = get_settings()
    provider = settings.embedding_provider.lower()
    model = (
        settings.ollama_embed_model if provider == "ollama" else settings.openai_embed_model
    )
    return f"{provider}:{model}"
