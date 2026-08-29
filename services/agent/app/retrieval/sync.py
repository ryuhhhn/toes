"""Catalog snapshot sync.

Reindexing is the expensive part of onboarding (one LLM descriptor pass plus an embedding
pass), so it is skipped whenever the merchant's rows hash to what we already indexed.
"""

from __future__ import annotations

import logging

from app.clients.merchant import MerchantUnavailable, get_merchant_client
from app.ingestion.pipeline import analyze_rows, row_hash
from app.retrieval.registry import get_registry

log = logging.getLogger(__name__)


async def catalog_changed(merchant_id: str) -> tuple[bool, str | None]:
    """(changed, new_hash). Unreachable merchant counts as unchanged, not as empty."""
    try:
        rows = await get_merchant_client().fetch_catalog(merchant_id)
    except MerchantUnavailable as exc:
        log.warning("sync check failed for %s: %s", merchant_id, exc)
        return False, None

    typed = analyze_rows(rows, merchant_id=merchant_id).rows
    current = row_hash(typed)

    index = get_registry().peek(merchant_id)
    known = index.meta.get("row_hash") if index else None
    return current != known, current


async def sync_if_changed(merchant_id: str) -> dict:
    changed, current = await catalog_changed(merchant_id)
    if not changed:
        return {"merchant_id": merchant_id, "reindexed": False, "row_hash": current}

    log.info("catalog for %s changed (hash=%s); reindexing", merchant_id, current)
    status = await get_registry().rebuild(merchant_id)
    return {"merchant_id": merchant_id, "reindexed": True, "row_hash": current, **status}
