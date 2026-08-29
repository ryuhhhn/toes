"""Standalone Merchant + Payment stubs.

Run:  uv run uvicorn stubs.mock_services:app --port 9001

Implements exactly the contract we consume, and nothing else. Two things here exist
specifically so the *unhappy* paths can be demonstrated rather than described:

  * stock mutation, so "it went out of stock while you were deciding" is reproducible
  * failure injection on payment, so a declined card is a scripted demo beat

Every merchant catalog is a file dropped into fixtures/catalogs/. The merchant_id is the
filename stem. This stub stores rows; it does not interpret them — interpretation is ours.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.ingestion.loader import load_table

log = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "catalogs"
SUPPORTED = {".csv", ".tsv", ".txt", ".xlsx", ".xls"}

PREVIEW_TTL_SECONDS = 300
TAX_RATE = 0.08

FAILURE_MESSAGES = {
    "insufficient_funds": "The card was declined for insufficient funds.",
    "card_declined": "The card was declined by the issuer.",
    "network_error": "The payment network could not be reached.",
    "expired_preview": "This transaction preview has expired.",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Catalog:
    """Raw rows exactly as uploaded, indexed by whichever column looks like the id."""

    def __init__(self, merchant_id: str, rows: list[dict], id_column: str | None):
        self.merchant_id = merchant_id
        self.rows = rows
        self.id_column = id_column

    def index(self) -> dict[str, dict]:
        if not self.id_column:
            return {}
        return {str(row.get(self.id_column)): row for row in self.rows}

    def by_ids(self, ids: list[str]) -> list[dict]:
        lookup = self.index()
        return [lookup[i] for i in ids if i in lookup]


def _first_unique_column(rows: list[dict]) -> str | None:
    """Storage-level id detection only. Deriving roles properly is the agent's job."""
    if not rows:
        return None
    for column in rows[0]:
        values = [row.get(column) for row in rows]
        if all(v not in (None, "") for v in values) and len(set(map(str, values))) == len(values):
            return column
    return None


def load_catalogs() -> dict[str, Catalog]:
    catalogs: dict[str, Catalog] = {}
    if not CATALOG_DIR.exists():
        log.warning("no catalog directory at %s", CATALOG_DIR)
        return catalogs

    for path in sorted(CATALOG_DIR.iterdir()):
        if path.suffix.lower() not in SUPPORTED:
            continue
        try:
            table = load_table(path)
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop the others
            log.error("could not load %s: %s", path.name, exc)
            continue
        rows = [
            {str(k): (None if v is None else str(v)) for k, v in record.items()}
            for record in table.df.where(table.df.notna(), None).to_dict(orient="records")
        ]
        merchant_id = path.stem
        catalogs[merchant_id] = Catalog(merchant_id, rows, _first_unique_column(rows))
        log.info("loaded catalog %s (%d rows)", merchant_id, len(rows))

    return catalogs


CATALOGS: dict[str, Catalog] = {}
PREVIEWS: dict[str, dict] = {}
AUTHORIZATIONS: dict[str, dict] = {}
RECEIPTS: dict[str, dict] = {}

app = FastAPI(title="Merchant + Payment stubs", version="0.1.0")


@app.on_event("startup")
async def _startup() -> None:
    CATALOGS.update(load_catalogs())


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "merchants": {m: len(c.rows) for m, c in CATALOGS.items()},
        "previews": len(PREVIEWS),
        "receipts": len(RECEIPTS),
    }


# --- Merchant ----------------------------------------------------------------


@app.get("/merchants")
async def merchants() -> dict:
    return {
        "merchants": [
            {"merchant_id": m, "row_count": len(c.rows), "id_column": c.id_column}
            for m, c in CATALOGS.items()
        ]
    }


@app.get("/catalog")
async def catalog(
    merchant_id: str = Query(...),
    ids: str | None = Query(None, description="Comma-separated ids for reverification"),
) -> dict:
    entry = CATALOGS.get(merchant_id)
    if entry is None:
        raise HTTPException(404, f"unknown merchant_id {merchant_id!r}")

    if ids:
        wanted = [i.strip() for i in ids.split(",") if i.strip()]
        rows = entry.by_ids(wanted)
    else:
        rows = entry.rows

    return {
        "merchant_id": merchant_id,
        "id_column": entry.id_column,
        "row_count": len(rows),
        "rows": rows,
    }


class StockPatch(BaseModel):
    updates: dict[str, dict[str, Any]] = Field(
        ..., description="{row_id: {column: new_value}} — mutates the stored row in place"
    )


@app.post("/catalog/{merchant_id}/stock")
async def patch_stock(merchant_id: str, patch: StockPatch) -> dict:
    """Makes "it sold out while you were deciding" a demoable event, not a story."""
    entry = CATALOGS.get(merchant_id)
    if entry is None:
        raise HTTPException(404, f"unknown merchant_id {merchant_id!r}")

    lookup = entry.index()
    applied: dict[str, dict] = {}
    for row_id, changes in patch.updates.items():
        row = lookup.get(str(row_id))
        if row is None:
            continue
        for column, value in changes.items():
            row[column] = None if value is None else str(value)
        applied[row_id] = row

    return {"updated": len(applied), "rows": list(applied.values())}


# --- Payment -----------------------------------------------------------------


class PreviewItem(BaseModel):
    id: str
    title: str = ""
    quantity: int = 1
    unit_price: float = 0.0


class PreviewRequest(BaseModel):
    session_id: str = ""
    merchant_id: str = ""
    currency: str = "USD"
    items: list[PreviewItem] = Field(default_factory=list)


def _fail(code: str | None) -> None:
    if code:
        raise HTTPException(
            status_code=402,
            detail={"code": code, "message": FAILURE_MESSAGES.get(code, "Payment failed.")},
        )


@app.post("/payment/preview")
async def payment_preview(request: PreviewRequest, fail: str | None = Query(None)) -> dict:
    _fail(fail)

    subtotal = round(sum(i.unit_price * i.quantity for i in request.items), 2)
    tax = round(subtotal * TAX_RATE, 2)
    preview_id = f"prev_{uuid.uuid4().hex[:12]}"
    expires_at = _now() + timedelta(seconds=PREVIEW_TTL_SECONDS)

    record = {
        "preview_id": preview_id,
        "session_id": request.session_id,
        "merchant_id": request.merchant_id,
        "currency": request.currency,
        "items": [i.model_dump() for i in request.items],
        "subtotal": subtotal,
        "tax": tax,
        "total": round(subtotal + tax, 2),
        "expires_at": expires_at.isoformat(),
        "created_at": _now().isoformat(),
    }
    PREVIEWS[preview_id] = record
    return record


class AuthorizeRequest(BaseModel):
    preview_id: str
    session_id: str = ""
    user_id: str = "demo-user"


@app.post("/payment/authorize")
async def payment_authorize(request: AuthorizeRequest, fail: str | None = Query(None)) -> dict:
    _fail(fail)

    preview = PREVIEWS.get(request.preview_id)
    if preview is None:
        raise HTTPException(404, {"code": "unknown_preview", "message": "No such preview."})

    if datetime.fromisoformat(preview["expires_at"]) < _now():
        raise HTTPException(
            402, {"code": "expired_preview", "message": FAILURE_MESSAGES["expired_preview"]}
        )

    authorization_id = f"auth_{uuid.uuid4().hex[:12]}"
    AUTHORIZATIONS[authorization_id] = {
        "authorization_id": authorization_id,
        "preview_id": request.preview_id,
        "user_id": request.user_id,
        "status": "authorized",
        "created_at": _now().isoformat(),
        "consumed": False,
    }
    return AUTHORIZATIONS[authorization_id]


class ConfirmRequest(BaseModel):
    preview_id: str
    authorization_id: str
    session_id: str = ""


@app.post("/payment/confirm")
async def payment_confirm(request: ConfirmRequest, fail: str | None = Query(None)) -> dict:
    _fail(fail)

    authorization = AUTHORIZATIONS.get(request.authorization_id)
    if authorization is None:
        raise HTTPException(404, {"code": "unknown_authorization", "message": "Not authorized."})
    if authorization["preview_id"] != request.preview_id:
        raise HTTPException(
            409, {"code": "preview_mismatch", "message": "Authorization is for another preview."}
        )
    if authorization["consumed"]:
        raise HTTPException(
            409, {"code": "already_charged", "message": "This authorization was already used."}
        )

    preview = PREVIEWS[request.preview_id]
    authorization["consumed"] = True

    transaction_id = f"txn_{uuid.uuid4().hex[:12]}"
    receipt = {
        "transaction_id": transaction_id,
        "preview_id": request.preview_id,
        "authorization_id": request.authorization_id,
        "merchant_id": preview["merchant_id"],
        "currency": preview["currency"],
        "items": preview["items"],
        "subtotal": preview["subtotal"],
        "tax": preview["tax"],
        "total": preview["total"],
        "status": "captured",
        "network": "visa-mock",
        "timestamp": _now().isoformat(),
    }
    RECEIPTS[transaction_id] = receipt
    return receipt


@app.get("/payment/receipt/{transaction_id}")
async def payment_receipt(transaction_id: str) -> dict:
    receipt = RECEIPTS.get(transaction_id)
    if receipt is None:
        raise HTTPException(404, {"code": "unknown_transaction", "message": "No such receipt."})
    return receipt


@app.post("/admin/reset")
async def reset() -> dict:
    """Between demo runs: fresh stock, no stale previews."""
    PREVIEWS.clear()
    AUTHORIZATIONS.clear()
    RECEIPTS.clear()
    CATALOGS.clear()
    CATALOGS.update(load_catalogs())
    return {"status": "reset", "merchants": list(CATALOGS)}
