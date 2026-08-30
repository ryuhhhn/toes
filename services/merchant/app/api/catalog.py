"""Catalog routes.

Two audiences, deliberately separated (docs/CONTRACTS.md §0):

- **The agent** reads `GET /catalog/raw` and `GET /merchants`. It gets the uploaded sheet
  verbatim and derives all meaning itself.
- **The merchant console** reads `GET /catalog` and `GET /categories`, which return the
  normalized nine-field `Product` shape for its own product table.

The agent must never read the normalized view. Normalizing at upload destroys the raw
columns its profiler needs, and a normalized row cannot be un-normalized.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.normalize import normalize_csv
from app.schemas import ProductUpdate, UploadResponse
from app.storage import (
    get_catalog,
    get_categories,
    get_raw_rows,
    list_merchants,
    patch_raw_rows,
    replace_catalog,
    replace_raw_rows,
    update_product,
)
from app.tabular import ACCEPT_ATTRIBUTE, UnreadableUpload, read_upload

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/catalog/upload", response_model=UploadResponse)
async def upload_catalog(
    merchant_id: str = Form(...),
    file: UploadFile = File(...),
):
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if not file.filename:
        raise HTTPException(status_code=400, detail="CSV file is required")

    try:
        df = read_upload(await file.read(), file.filename)
    except UnreadableUpload as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read {file.filename!r} as a table ({ACCEPT_ATTRIBUTE}): {exc}",
        ) from exc

    # Store the raw sheet FIRST, before normalization runs and regardless of what it does
    # to the data. The agent's whole premise depends on these columns surviving intact.
    raw_rows = df.where(pd.notna(df), None).to_dict(orient="records")
    stored = replace_raw_rows(merchant_id, raw_rows, source_filename=file.filename)
    log.info(
        "stored %d raw rows for %s (id_column=%s) from %s",
        stored["row_count"], merchant_id, stored["id_column"], file.filename,
    )

    products, report = normalize_csv(df, merchant_id)

    # ALWAYS reset the console's view, including to nothing (docs/CONTRACTS.md §1.3).
    # Skipping this on a 422 is what let /catalog keep serving the PREVIOUS upload while
    # /catalog/raw served this one: the console showed one catalog and the agent sold
    # from another, and neither screen said anything was wrong.
    replace_catalog(merchant_id, products if report.ok else [])

    if not report.ok:
        # The raw rows are already stored, so the agent can still work with this upload
        # even when the console's normalization rejects it. `raw` says so out loud.
        return JSONResponse(
            status_code=422,
            content={
                "report": report.to_dict(),
                "products": [],
                "raw": {
                    "merchant_id": merchant_id,
                    "id_column": stored["id_column"],
                    "row_count": stored["row_count"],
                },
            },
        )

    return {"report": report.to_dict(), "products": products}


# --- the agent's view ---------------------------------------------------------


@router.get("/catalog/raw")
async def get_catalog_raw(
    merchant_id: str = Query(...),
    ids: Optional[str] = Query(None, description="Comma-separated ids; absent means all"),
):
    """Raw rows exactly as uploaded. See docs/CONTRACTS.md §1.1.

    `ids` returns exactly the rows requested — this is what makes pre-charge price and
    stock reverification (agent invariant 5) actually verify anything.
    """
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    wanted = ids.split(",") if ids is not None else None
    payload = get_raw_rows(merchant_id, wanted)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"unknown merchant_id {merchant_id!r}")
    return payload


@router.get("/merchants")
async def get_merchants():
    """Every merchant with raw rows stored. See docs/CONTRACTS.md §1.2."""
    return {"merchants": list_merchants()}


class StockPatch(BaseModel):
    """`{row_id: {column: new_value}}` — see docs/CONTRACTS.md §1.5.

    The CALLER names the column, and that is the whole point. This service does not know
    which of its columns means stock; deriving roles is the agent's job (§0), and a
    merchant that guessed would be normalizing by the back door. It writes the value it
    is handed into the cell it is told.
    """

    updates: dict[str, dict[str, Any]] = Field(default_factory=dict)


@router.post("/catalog/{merchant_id}/stock")
async def patch_stock(merchant_id: str, patch: StockPatch):
    """Write new values into stored raw rows — how inventory follows a sale.

    Unknown row ids are skipped rather than erroring, so a partially-stale cart still
    applies the part that is real.
    """
    applied = patch_raw_rows(merchant_id, patch.updates)
    if applied is None:
        raise HTTPException(status_code=404, detail=f"unknown merchant_id {merchant_id!r}")

    log.info("patched %d raw row(s) for %s", len(applied), merchant_id)
    return {"updated": len(applied), "rows": applied}


# --- the console's view -------------------------------------------------------


@router.get("/catalog")
async def list_catalog(merchant_id: str):
    """Normalized products for the merchant console's product table.

    NOT an agent contract — the agent reads /catalog/raw.
    """
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    return get_catalog(merchant_id)


@router.get("/categories")
async def list_categories(merchant_id: str):
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    return {"merchant_id": merchant_id, "categories": get_categories(merchant_id)}


@router.patch("/catalog/{product_id}")
async def patch_catalog(product_id: str, merchant_id: str, payload: ProductUpdate):
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    updated = update_product(
        merchant_id, product_id, payload.model_dump(exclude_unset=True, exclude_none=True)
    )
    if updated is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"Product {product_id} not found for merchant {merchant_id}"},
        )
    return updated
