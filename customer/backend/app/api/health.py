"""Provider reachability in one call.

Every dependency that can be down on demo day is checked here, individually, with the
configured value echoed back. Finding out that Ollama is not running from a 20-second hang
mid-demo is the failure this endpoint exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter

from app.clients.http import get_http_client
from app.config import get_settings
from app.embeddings.base import EmbeddingUnavailable
from app.embeddings.factory import embedding_model_id, get_embedder
from app.llm.factory import LLMUnavailable, get_llm

log = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def _check_llm() -> dict:
    settings = get_settings()
    try:
        client = get_llm()
    except LLMUnavailable as exc:
        return {"ok": False, "provider": settings.llm_provider, "detail": str(exc)}
    return {"ok": True, "provider": settings.llm_provider, "model": client.model}


async def _check_embeddings() -> dict:
    info: dict = {"model_id": embedding_model_id()}
    try:
        embedder = get_embedder()
    except EmbeddingUnavailable as exc:
        return {**info, "ok": False, "detail": str(exc)}

    try:
        ok = await embedder.healthy()
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {**info, "ok": False, "detail": str(exc)}

    return {
        **info,
        "ok": ok,
        "detail": None if ok else f"model {embedder.model!r} not reachable or not pulled",
    }


async def _check_service(name: str, base_url: str) -> dict:
    try:
        response = await get_http_client().get(f"{base_url.rstrip('/')}/health", timeout=3.0)
        return {"ok": response.status_code < 500, "name": name, "url": base_url}
    except httpx.HTTPError as exc:
        return {"ok": False, "name": name, "url": base_url, "detail": str(exc)}


@router.get("/health")
async def health() -> dict:
    settings = get_settings()

    llm, embeddings, merchant, payment = await asyncio.gather(
        _check_llm(),
        _check_embeddings(),
        _check_service("merchant", settings.merchant_base_url),
        _check_service("payment", settings.payment_base_url),
    )

    checks = {
        "llm": llm,
        "embeddings": embeddings,
        "merchant": merchant,
        "payment": payment,
    }
    # Degraded rather than down: the agent still runs on a bootstrap profile with
    # filter-only search when the model providers are missing.
    required_ok = merchant["ok"] and payment["ok"]

    return {
        "status": "ok" if all(c["ok"] for c in checks.values()) else (
            "degraded" if required_ok else "down"
        ),
        "checks": checks,
    }
