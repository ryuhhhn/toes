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
from io import StringIO
from typing import Optional

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from app.normalize import normalize_csv
from app.schemas import ProductUpdate, UploadResponse
from app.storage import (
    get_catalog,
    get_categories,
    get_raw_rows,
    list_merchants,
    replace_catalog,
    replace_raw_rows,
    update_product,
)

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
        raw = await file.read()
        text = raw.decode("utf-8-sig")
        df = pd.read_csv(StringIO(text))
    except Exception as exc:  # pragma: no cover - defensive guard for malformed uploads
        raise HTTPException(status_code=400, detail=f"Unable to parse CSV: {exc}") from exc

    # Store the raw sheet FIRST, before normalization runs and regardless of what it does
    # to the data. The agent's whole premise depends on these columns surviving intact.
    raw_rows = df.where(pd.notna(df), None).to_dict(orient="records")
    stored = replace_raw_rows(merchant_id, raw_rows, source_filename=file.filename)
    log.info(
        "stored %d raw rows for %s (id_column=%s) from %s",
        stored["row_count"], merchant_id, stored["id_column"], file.filename,
    )

    products, report = normalize_csv(df, merchant_id)
    if not report.ok:
        # The raw rows are already stored, so the agent can still work with this upload
        # even when the console's normalization rejects it.
        return JSONResponse(
            status_code=422,
            content={"report": report.to_dict(), "products": []},
        )

    replace_catalog(merchant_id, products)
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
