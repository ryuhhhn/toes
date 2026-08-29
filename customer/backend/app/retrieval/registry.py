"""Multiple merchant indices resident at once.

This is what makes the two-niche demo possible without a restart: onboard an unrelated
catalog mid-session, and both remain queryable. If switching catalogs meant restarting the
server, step 7 of the demo would not be a demo.
"""

from __future__ import annotations

import asyncio
import logging

from app.clients.merchant import MerchantUnavailable, get_merchant_client
from app.ingestion.pipeline import analyze_rows, row_hash
from app.ingestion.profile_store import get_profile_store
from app.models.profile import AgentProfile
from app.retrieval.enrich import enrich_rows
from app.retrieval.index import CatalogIndex, build_index, index_status, load_index, save_index

log = logging.getLogger(__name__)


class IndexRegistry:
    def __init__(self) -> None:
        self._indices: dict[str, CatalogIndex] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, merchant_id: str) -> asyncio.Lock:
        if merchant_id not in self._locks:
            self._locks[merchant_id] = asyncio.Lock()
        return self._locks[merchant_id]

    def loaded(self) -> list[str]:
        return sorted(self._indices)

    def peek(self, merchant_id: str) -> CatalogIndex | None:
        return self._indices.get(merchant_id)

    async def get(self, merchant_id: str, *, build_if_missing: bool = True) -> CatalogIndex | None:
        """Resident index, else disk, else built from the merchant's live catalog."""
        cached = self._indices.get(merchant_id)
        if cached is not None:
            return cached

        async with self._lock(merchant_id):
            cached = self._indices.get(merchant_id)
            if cached is not None:
                return cached

            profile = get_profile_store().load_for_runtime(merchant_id)
            if profile is None:
                if not build_if_missing:
                    return None
                profile = await self._profile_from_merchant(merchant_id)
                if profile is None:
                    return None

            from_disk = load_index(merchant_id, profile)
            if from_disk is not None and from_disk.meta.get("row_hash") == (
                profile.source.row_hash
            ):
                self._indices[merchant_id] = from_disk
                log.info("loaded index for %s from disk (%d rows)", merchant_id, from_disk.size)
                return from_disk

            if not build_if_missing:
                return from_disk

            return await self._build(merchant_id, profile)

    async def rebuild(self, merchant_id: str, profile: AgentProfile | None = None) -> dict:
        """Force a fresh index. Called after profile approval and by the manual endpoint."""
        async with self._lock(merchant_id):
            profile = profile or get_profile_store().load_for_runtime(merchant_id)
            if profile is None:
                # Reindexing a merchant nobody has ingested yet should onboard them
                # rather than refuse — it is the same cold start the chat path does.
                profile = await self._profile_from_merchant(merchant_id)
            if profile is None:
                return {"ok": False, "error": f"no catalogue available for {merchant_id!r}"}
            index = await self._build(merchant_id, profile)
            return {"ok": index is not None, **index_status(index)}

    def evict(self, merchant_id: str) -> None:
        self._indices.pop(merchant_id, None)

    # --- internals -----------------------------------------------------------

    async def _profile_from_merchant(self, merchant_id: str) -> AgentProfile | None:
        """Cold start: a shopper arrived before anyone approved a profile.

        Deriving a bootstrap profile on the spot is better than refusing to sell.
        """
        try:
            rows = await get_merchant_client().fetch_catalog(merchant_id)
        except MerchantUnavailable as exc:
            log.error("cannot cold-start %s: %s", merchant_id, exc)
            return None
        if not rows:
            return None

        result = analyze_rows(rows, merchant_id=merchant_id)
        store = get_profile_store()
        result.profile.version = store.next_version(merchant_id)
        result.profile.notes.append(
            "info: generated automatically on first use and not yet approved by the merchant."
        )
        store.save(result.profile)
        log.info("cold-started a bootstrap profile for %s", merchant_id)
        return result.profile

    async def _build(self, merchant_id: str, profile: AgentProfile) -> CatalogIndex | None:
        try:
            rows = await get_merchant_client().fetch_catalog(merchant_id)
        except MerchantUnavailable as exc:
            log.error("cannot build index for %s: %s", merchant_id, exc)
            return None

        # Re-run coercion so the index holds typed values, not the merchant's raw strings.
        analysed = analyze_rows(rows, merchant_id=merchant_id, version=profile.version)
        typed_rows = analysed.rows
        profile.source.row_hash = row_hash(typed_rows)
        profile.source.row_count = len(typed_rows)

        descriptors = await enrich_rows(typed_rows, profile)
        index = await build_index(merchant_id, typed_rows, profile, descriptors=descriptors)

        save_index(index)
        self._indices[merchant_id] = index
        log.info(
            "built index for %s: %d rows, vectors=%s", merchant_id, index.size, index.has_vectors
        )
        return index


_registry: IndexRegistry | None = None


def get_registry() -> IndexRegistry:
    global _registry
    if _registry is None:
        _registry = IndexRegistry()
    return _registry
