"""The one shared httpx.AsyncClient.

Connection pooling matters here: a chat turn can fan out to the merchant backend and the
payment service several times. Owned by the app lifespan; the accessor exists so client
modules do not have to thread request state through every call.
"""

from __future__ import annotations

import httpx

from app.config import get_settings

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=get_settings().http_timeout_seconds)
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
