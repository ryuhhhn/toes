"""Merchant Backend client.

We consume exactly two things: the full catalog, and a by-ids lookup for reverifying price
and stock immediately before a charge. Their GET /catalog/search is cancelled — retrieval
is ours.

The index is for discovery only (invariant 5), which makes fetch_by_ids the single most
safety-critical call in this file.
"""

from __future__ import annotations

import logging

import httpx

from app.clients.http import get_http_client
from app.config import get_settings

log = logging.getLogger(__name__)


class MerchantUnavailable(RuntimeError):
    """The merchant backend could not be reached or answered unusably."""


class MerchantClient:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        settings = get_settings()
        self._base_url = (base_url or settings.merchant_base_url).rstrip("/")
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client or get_http_client()

    async def _get(self, path: str, params: dict) -> dict:
        url = f"{self._base_url}{path}"
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise MerchantUnavailable(f"merchant backend unreachable at {url}: {exc}") from exc
        except ValueError as exc:
            raise MerchantUnavailable(f"merchant backend returned invalid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise MerchantUnavailable("merchant backend returned an unexpected payload")
        return payload

    async def fetch_catalog(self, merchant_id: str) -> list[dict]:
        payload = await self._get("/catalog", {"merchant_id": merchant_id})
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise MerchantUnavailable("merchant backend returned no rows")
        return rows

    async def fetch_by_ids(self, merchant_id: str, ids: list[str]) -> dict[str, dict]:
        """Live price and stock for specific products, keyed by id.

        Used immediately before every preview. A missing id means the product is gone,
        which the caller must treat as a hard stop rather than a stale-cache fallback.
        """
        if not ids:
            return {}

        payload = await self._get(
            "/catalog", {"merchant_id": merchant_id, "ids": ",".join(ids)}
        )
        rows = payload.get("rows") or []
        id_column = payload.get("id_column")

        result: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = row.get(id_column) if id_column else None
            if key is None:
                # Fall back to any column whose value matches a requested id.
                key = next((v for v in row.values() if str(v) in set(ids)), None)
            if key is not None:
                result[str(key)] = row
        return result

    async def list_merchants(self) -> list[str]:
        try:
            payload = await self._get("/merchants", {})
        except MerchantUnavailable:
            return []
        return [
            str(m.get("merchant_id"))
            for m in payload.get("merchants", [])
            if m.get("merchant_id")
        ]


def get_merchant_client() -> MerchantClient:
    return MerchantClient()
