"""Index status and manual reindex."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.retrieval.index import index_status
from app.retrieval.registry import get_registry
from app.retrieval.sync import sync_if_changed

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/status")
async def status() -> dict:
    registry = get_registry()
    return {
        "loaded": registry.loaded(),
        "indices": {m: index_status(registry.peek(m)) for m in registry.loaded()},
    }


@router.get("/status/{merchant_id}")
async def status_for(merchant_id: str) -> dict:
    index = await get_registry().get(merchant_id, build_if_missing=False)
    return {"merchant_id": merchant_id, **index_status(index)}


@router.post("/reindex/{merchant_id}")
async def reindex(merchant_id: str) -> dict:
    result = await get_registry().rebuild(merchant_id)
    if not result.get("ok"):
        raise HTTPException(422, result.get("error", "reindex failed"))
    return result


@router.post("/sync/{merchant_id}")
async def sync(merchant_id: str) -> dict:
    """Reindex only if the merchant's rows actually changed."""
    return await sync_if_changed(merchant_id)
