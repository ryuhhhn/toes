"""Build, persist and load the per-merchant embedding index.

A numpy matrix and cosine similarity. Hundreds to thousands of rows per catalog does not
justify a vector database, and the ops burden of one would cost more than it buys.

The embedding model id and dimension are stamped into meta.json and checked on load. A
silent dimension change is a nasty, late-surfacing failure — it does not error, it just
returns nonsense — so a mismatch forces a rebuild instead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings
from app.embeddings.base import EmbeddingUnavailable
from app.embeddings.factory import embedding_model_id, get_embedder
from app.models.profile import AgentProfile

log = logging.getLogger(__name__)

VECTORS_FILE = "vectors.npy"
ROWS_FILE = "rows.json"
META_FILE = "meta.json"

MAX_ATTR_VALUES_IN_TEXT = 6


@dataclass
class CatalogIndex:
    """Everything search needs for one merchant, resident in memory."""

    merchant_id: str
    profile: AgentProfile
    rows: list[dict] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    matrix: np.ndarray | None = None
    meta: dict = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def has_vectors(self) -> bool:
        return self.matrix is not None and len(self.matrix) == len(self.rows)

    def row_by_id(self, product_id: str) -> dict | None:
        try:
            return self.rows[self.ids.index(str(product_id))]
        except ValueError:
            return None

    def rows_by_ids(self, product_ids: list[str]) -> list[dict]:
        lookup = {pid: row for pid, row in zip(self.ids, self.rows)}
        return [lookup[str(p)] for p in product_ids if str(p) in lookup]


def _attribute_phrases(row: dict, profile: AgentProfile) -> list[str]:
    """Attributes in words, so a vector match can see them at all.

    A bare value like "18" carries no meaning in embedding space; the field's own display
    name paired with that value and its unit does. That name comes from the profile, which
    is why this stays category-agnostic.
    """
    phrases: list[str] = []
    for spec in profile.active_fields():
        value = row.get(spec.column)
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple, set)):
            rendered = ", ".join(str(v) for v in list(value)[:MAX_ATTR_VALUES_IN_TEXT])
        elif isinstance(value, bool):
            rendered = "yes" if value else "no"
        elif isinstance(value, float) and value.is_integer():
            rendered = str(int(value))
        else:
            rendered = str(value)
        unit = f" {spec.unit}" if spec.unit else ""
        phrases.append(f"{spec.display_name}: {rendered}{unit}")
    return phrases


def build_document(row: dict, profile: AgentProfile, descriptor: str | None = None) -> str:
    """One embeddable string per product: name, description, attributes, descriptor."""
    roles = profile.roles
    parts: list[str] = []

    if roles.title and row.get(roles.title):
        parts.append(str(row[roles.title]))
    for column in roles.text:
        value = row.get(column)
        if value:
            parts.append(str(value))

    parts.extend(_attribute_phrases(row, profile))

    if descriptor:
        parts.append(descriptor)

    return ". ".join(p.strip().rstrip(".") for p in parts if str(p).strip())


def index_dir(merchant_id: str) -> Path:
    return get_settings().index_dir / merchant_id


async def build_index(
    merchant_id: str,
    rows: list[dict],
    profile: AgentProfile,
    *,
    descriptors: dict[str, str] | None = None,
) -> CatalogIndex:
    """Embed every row. Falls back to a vector-free index if embeddings are unavailable.

    A vector-free index still filters, still ranks by price, and still sells — it just
    stops understanding paraphrase. That is a much better outcome than a failed ingest.
    """
    descriptors = descriptors or {}
    id_column = profile.roles.id

    ids: list[str] = []
    documents: list[str] = []
    for position, row in enumerate(rows):
        product_id = str(row.get(id_column)) if id_column else str(position)
        ids.append(product_id)
        documents.append(build_document(row, profile, descriptors.get(product_id)))

    matrix: np.ndarray | None = None
    model_id = embedding_model_id()
    error: str | None = None

    try:
        embedder = get_embedder()
        matrix = await embedder.embed(documents, kind="document")
    except EmbeddingUnavailable as exc:
        error = str(exc)
        log.warning("index for %s built without vectors: %s", merchant_id, exc)

    meta = {
        "merchant_id": merchant_id,
        "embedding_model": model_id,
        "dim": int(matrix.shape[1]) if matrix is not None and matrix.size else 0,
        "row_count": len(rows),
        "row_hash": profile.source.row_hash,
        "profile_version": profile.version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "descriptors": len(descriptors),
        "vectors": matrix is not None,
        "error": error,
    }

    return CatalogIndex(
        merchant_id=merchant_id, profile=profile, rows=rows, ids=ids, matrix=matrix, meta=meta
    )


def save_index(index: CatalogIndex) -> None:
    directory = index_dir(index.merchant_id)
    directory.mkdir(parents=True, exist_ok=True)

    (directory / ROWS_FILE).write_text(
        json.dumps({"ids": index.ids, "rows": index.rows}, default=str), encoding="utf-8"
    )
    (directory / META_FILE).write_text(json.dumps(index.meta, indent=2), encoding="utf-8")

    vectors_path = directory / VECTORS_FILE
    if index.matrix is not None:
        np.save(vectors_path, index.matrix)
    elif vectors_path.exists():
        # Never leave vectors from a previous model beside rows they no longer describe.
        vectors_path.unlink()


def load_index(merchant_id: str, profile: AgentProfile) -> CatalogIndex | None:
    """Load from disk, rejecting anything built by a different embedding model."""
    directory = index_dir(merchant_id)
    meta_path, rows_path = directory / META_FILE, directory / ROWS_FILE
    if not meta_path.exists() or not rows_path.exists():
        return None

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        payload = json.loads(rows_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("index for %s is unreadable, will rebuild: %s", merchant_id, exc)
        return None

    current_model = embedding_model_id()
    if meta.get("vectors") and meta.get("embedding_model") != current_model:
        log.warning(
            "index for %s was built with %s but %s is configured; rebuilding",
            merchant_id,
            meta.get("embedding_model"),
            current_model,
        )
        return None

    matrix: np.ndarray | None = None
    vectors_path = directory / VECTORS_FILE
    if vectors_path.exists():
        try:
            matrix = np.load(vectors_path)
        except (OSError, ValueError) as exc:
            log.warning("vectors for %s unreadable, will rebuild: %s", merchant_id, exc)
            return None

    rows = payload.get("rows", [])
    ids = [str(i) for i in payload.get("ids", [])]
    if matrix is not None and len(matrix) != len(rows):
        log.warning("index for %s has %d vectors for %d rows; rebuilding", merchant_id,
                    len(matrix), len(rows))
        return None

    return CatalogIndex(
        merchant_id=merchant_id, profile=profile, rows=rows, ids=ids, matrix=matrix, meta=meta
    )


def index_status(index: CatalogIndex | None) -> dict[str, Any]:
    if index is None:
        return {"built": False}
    return {
        "built": True,
        "rows": index.size,
        "vectors": index.has_vectors,
        **{k: index.meta.get(k) for k in ("embedding_model", "dim", "built_at", "row_hash",
                                          "profile_version", "descriptors", "error")},
    }
