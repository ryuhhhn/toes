"""The only place an LLM SDK may be constructed."""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.llm.base import LLMClient


class LLMUnavailable(RuntimeError):
    """No usable provider is configured. Callers must degrade, not crash."""


@lru_cache
def get_llm() -> LLMClient:
    settings = get_settings()
    provider = settings.llm_provider.lower()

    if provider == "openai":
        if not settings.openai_api_key:
            raise LLMUnavailable("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
        from app.llm.openai_client import OpenAIClient

        return OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            timeout=settings.llm_timeout_seconds,
        )

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise LLMUnavailable("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        from app.llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout=settings.llm_timeout_seconds,
        )

    raise LLMUnavailable(f"unknown LLM_PROVIDER: {settings.llm_provider!r}")


def llm_available() -> bool:
    try:
        get_llm()
        return True
    except LLMUnavailable:
        return False
