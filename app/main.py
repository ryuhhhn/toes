from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.normalize import normalize_csv
from app.schemas import ChatRequest, ChatResponse, ProductUpdate, UploadResponse
from app.storage import get_catalog, get_categories, replace_catalog, search_catalog, update_product
from database import create_schema
from llm_client import OpenAIInferenceClient

app = FastAPI(title="Merchant Backend", version="1.0.0")
llm_client = None


def _fallback_chat_filters(message: str) -> dict[str, object]:
    """Extract basic search terms when OpenAI is unavailable."""
    ignored = {
        "a", "an", "and", "affordable", "below", "cheap", "dollars",
        "find", "fit", "for", "frames", "frame", "glasses", "me", "my",
        "of", "please", "price", "show", "some", "that", "the", "under",
        "want", "with",
    }
    words = re.findall(r"[a-z0-9]+", message.lower())
    query_words = [word for word in words if word not in ignored and not word.isdigit()]
    prices = [float(value) for value in re.findall(r"\$?\d+(?:\.\d+)?", message)]
    filters: dict[str, object] = {
        "query": " ".join(query_words) or None,
        "in_stock_only": True,
    }
    if prices and any(word in words for word in ("under", "below", "less")):
        filters["max_price"] = prices[-1]
    return filters


@app.get("/")
async def root():
    return {
        "service": "Merchant Backend",
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Find in-stock catalog products from a natural-language shopper request."""
    global llm_client
    if not request.merchant_id.strip():
        raise HTTPException(status_code=400, detail="merchant_id is required")

    if llm_client is None:
        try:
            llm_client = OpenAIInferenceClient()
        except ValueError:
            llm_client = False

    filters = None
    if llm_client:
        filters = llm_client.parse_search_request(request.message)

    if not filters:
        filters = _fallback_chat_filters(request.message)
    filters.setdefault("in_stock_only", True)
    products = search_catalog(merchant_id=request.merchant_id, **filters)

    if products:
        response_message = f"I found {len(products)} matching product{'s' if len(products) != 1 else ''}."
    else:
        response_message = "I couldn't find an in-stock product matching that request."
    return {"message": response_message, "products": products, "filters": filters}


def _seed_catalog_from_json() -> None:
    json_path = Path(__file__).resolve().parent.parent / "merchant" / "backend" / "eyewear.json"
    if not json_path.exists():
        return

    try:
        with json_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return

    report = payload.get("report", {})
    merchant_id = report.get("merchant_id")
    products = payload.get("products", [])
    if merchant_id and products:
        replace_catalog(merchant_id, products)


@app.on_event("startup")
async def startup_event() -> None:
    create_schema()
    _seed_catalog_from_json()


@app.post("/catalog/upload", response_model=UploadResponse)
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

    products, report = normalize_csv(df, merchant_id)
    if not report.ok:
        return JSONResponse(
            status_code=422,
            content={
                "report": report.to_dict(),
                "products": [],
            },
        )

    replace_catalog(merchant_id, products)
    return {"report": report.to_dict(), "products": products}


@app.get("/catalog")
async def list_catalog(merchant_id: str):
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    return get_catalog(merchant_id)


@app.get("/catalog/search")
async def search_catalog_endpoint(
    merchant_id: str,
    query: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock_only: bool = False,
):
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    return search_catalog(
        merchant_id=merchant_id,
        query=query,
        category=category,
        min_price=min_price,
        max_price=max_price,
        in_stock_only=in_stock_only,
    )


@app.get("/categories")
async def list_categories(merchant_id: str):
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")
    return {"merchant_id": merchant_id, "categories": get_categories(merchant_id)}


@app.patch("/catalog/{product_id}")
async def patch_catalog(product_id: str, merchant_id: str, payload: ProductUpdate):
    if not merchant_id:
        raise HTTPException(status_code=400, detail="merchant_id is required")

    updated = update_product(merchant_id, product_id, payload.model_dump(exclude_unset=True, exclude_none=True))
    if updated is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"Product {product_id} not found for merchant {merchant_id}"},
        )
    return updated


@app.get("/health")
async def healthcheck():
    return {"status": "ok"}
