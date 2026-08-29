"""Filter, then rank.

Hard constraints are applied first and are never ranked around. Cosine similarity runs
over the filtered subset only — ranking the whole catalog and filtering afterwards is both
slower and wrong, because it silently truncates good in-scope matches.

If the embedding backend is slow or dead, search degrades to filter-only ordering and says
so. A dead Ollama must never hang the stream.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.config import get_settings
from app.embeddings.base import EmbeddingUnavailable
from app.embeddings.factory import get_embedder
from app.retrieval.filters import (
    Predicate,
    filter_with_relaxation,
    in_stock_predicate,
    predicates_from_slots,
    sellable_predicate,
)
from app.retrieval.index import CatalogIndex

log = logging.getLogger(__name__)


@dataclass
class SearchResult:
    items: list[dict] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    filters_applied: list[dict] = field(default_factory=list)
    filters_relaxed: list[dict] = field(default_factory=list)
    total_candidates: int = 0
    candidate_ids: list[str] = field(default_factory=list)
    ranked_by: str = "similarity"
    degraded: str | None = None

    def as_dict(self) -> dict:
        return {
            "ids": self.ids,
            "scores": [round(s, 4) for s in self.scores],
            "filters_applied": self.filters_applied,
            "filters_relaxed": self.filters_relaxed,
            "total_candidates": self.total_candidates,
            "ranked_by": self.ranked_by,
            "degraded": self.degraded,
        }


async def embed_query(text: str, *, timeout: float | None = None) -> np.ndarray | None:
    """Embed with a hard timeout. Returns None rather than raising, so search can degrade."""
    settings = get_settings()
    timeout = timeout or settings.embed_timeout_seconds
    try:
        embedder = get_embedder()
        matrix = await asyncio.wait_for(embedder.embed([text], kind="query"), timeout=timeout)
    except (EmbeddingUnavailable, asyncio.TimeoutError) as exc:
        log.warning("query embedding unavailable, falling back to filter-only: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - never let retrieval kill a turn
        log.warning("query embedding failed: %s", exc)
        return None
    return matrix[0] if len(matrix) else None


def _fallback_order(index: CatalogIndex, candidates: list[int]) -> list[int]:
    """Without vectors, order by price ascending then stock descending.

    Cheapest-first is a defensible default and, unlike an arbitrary order, it is
    explainable to a shopper.
    """
    price_column = index.profile.roles.price
    stock_column = index.profile.roles.stock

    def key(position: int) -> tuple[float, float]:
        row = index.rows[position]
        try:
            price = float(row.get(price_column)) if price_column else 0.0
        except (TypeError, ValueError):
            price = float("inf")
        try:
            stock = float(row.get(stock_column)) if stock_column else 0.0
        except (TypeError, ValueError):
            stock = 0.0
        return (price, -stock)

    return sorted(candidates, key=key)


async def search(
    index: CatalogIndex,
    *,
    query: str = "",
    slots: dict[str, Any] | None = None,
    extra_predicates: list[Predicate] | None = None,
    hard_columns: set[str] | None = None,
    k: int | None = None,
    include_out_of_stock: bool = False,
) -> SearchResult:
    settings = get_settings()
    k = k or settings.search_top_k
    profile = index.profile

    predicates: list[Predicate] = []
    sellable = sellable_predicate(profile)
    if sellable is not None:
        predicates.append(sellable)
    if not include_out_of_stock:
        stock = in_stock_predicate(profile)
        if stock is not None:
            predicates.append(stock)

    predicates.extend(predicates_from_slots(slots or {}, profile, hard_columns=hard_columns))
    predicates.extend(extra_predicates or [])

    outcome = filter_with_relaxation(
        index.rows, predicates, min_results=settings.search_min_results
    )
    candidates = outcome.indices

    result = SearchResult(
        filters_applied=[p.as_dict() for p in outcome.applied],
        filters_relaxed=[p.as_dict() for p in outcome.relaxed],
        total_candidates=len(candidates),
        candidate_ids=[index.ids[i] for i in candidates],
    )

    if not candidates:
        result.ranked_by = "none"
        return result

    vector = await embed_query(query) if (query and index.has_vectors) else None

    if vector is not None:
        subset = index.matrix[candidates]  # rank over the filtered subset only
        scores = subset @ vector  # both sides are L2-normalised at the provider boundary
        order = np.argsort(-scores)[:k]
        chosen = [candidates[int(i)] for i in order]
        result.scores = [float(scores[int(i)]) for i in order]
        result.ranked_by = "similarity"
    else:
        chosen = _fallback_order(index, candidates)[:k]
        result.scores = [0.0] * len(chosen)
        result.ranked_by = "price"
        if query:
            result.degraded = (
                "ranked by price because semantic search was unavailable"
                if index.has_vectors
                else "ranked by price because this catalogue has no embeddings yet"
            )

    result.items = [index.rows[i] for i in chosen]
    result.ids = [index.ids[i] for i in chosen]
    return result
