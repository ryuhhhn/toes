"""preview_transaction — the last honest moment before money moves.

Order matters and is not negotiable:

  1. re-verify price and stock against the Merchant Backend, and abort on any mismatch.
     The index is for discovery only (invariant 5); it is a snapshot, and a snapshot is
     not something to charge a card against.
  2. call the payment service for the real totals
  3. mint a preview_id bound to a hash of the cart, with an expiry (invariant 2)
  4. emit the preview event that the frontend turns into a confirm button

This tool cannot charge anything. Charging requires a separate HTTP POST from the shopper.
"""

from __future__ import annotations

import logging

from app.agent.events import PreviewEvent
from app.audit import record
from app.clients.merchant import MerchantUnavailable, get_merchant_client
from app.clients.payment import PaymentError, get_payment_client
from app.config import get_settings
from app.session.models import new_preview
from app.tools.registry import ToolContext, ToolResult, object_schema, tool

log = logging.getLogger(__name__)


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def reverify_cart(ctx: ToolContext) -> list[str]:
    """Refresh price and stock from the merchant. Returns human-readable problems.

    Prices are corrected in place when they move, because charging the price we quoted
    is not an option; a moved price is reported so the agent says so out loud.
    """
    session, profile = ctx.session, ctx.profile
    roles = profile.roles
    ids = [item.id for item in session.cart.items]
    if not ids:
        return ["The basket is empty."]

    try:
        live = await get_merchant_client().fetch_by_ids(session.merchant_id, ids)
    except MerchantUnavailable as exc:
        return [f"Could not re-check stock and prices with the merchant ({exc})."]

    from app.ingestion.coerce import coerce_column

    import pandas as pd

    problems: list[str] = []
    for item in list(session.cart.items):
        row = live.get(item.id)
        if row is None:
            problems.append(f"{item.title} is no longer in the merchant's catalogue.")
            continue

        if roles.stock and row.get(roles.stock) is not None:
            stock_series, _ = coerce_column(pd.Series([row[roles.stock]], dtype=object))
            stock = _to_float(stock_series.iloc[0])
            if stock is not None and stock <= 0:
                problems.append(f"{item.title} has just gone out of stock.")
                continue
            if stock is not None and item.quantity > stock:
                problems.append(
                    f"Only {int(stock)} of {item.title} left, not {item.quantity}."
                )

        if roles.price and row.get(roles.price) is not None:
            price_series, _ = coerce_column(pd.Series([row[roles.price]], dtype=object))
            price = _to_float(price_series.iloc[0])
            if price is not None and abs(price - item.unit_price) > 0.005:
                problems.append(
                    f"The price of {item.title} changed from {item.unit_price:g} to {price:g}."
                )
                item.unit_price = price

    return problems


@tool(
    name="preview_transaction",
    description=(
        "Show the shopper exactly what they would pay, with live prices and stock "
        "re-checked. This does not charge anything: it produces a preview the shopper "
        "must confirm themselves with a button press. Call this when they have decided."
    ),
    start_summary="Checking live prices and stock",
    parameters=object_schema({}),
)
async def preview_transaction(args: dict, ctx: ToolContext) -> ToolResult:
    settings = get_settings()
    session = ctx.session

    if not session.cart.items:
        return ToolResult.failure(
            "The basket is empty, so there is nothing to preview.", code="empty_cart"
        )

    problems = await reverify_cart(ctx)
    if problems:
        session.invalidate_preview("reverification failed")
        detail = " ".join(problems)
        record(
            "preview_aborted",
            session=session.id,
            merchant_id=session.merchant_id,
            cart=[i.model_dump() for i in session.cart.items],
            outcome="reverification_failed",
            problems=problems,
        )
        return ToolResult.failure(
            f"{detail} Tell the shopper plainly what changed and what they can do next.",
            code="verification_failed",
        )

    try:
        response = await get_payment_client().preview(
            session_id=session.id,
            merchant_id=session.merchant_id,
            items=session.cart.items,
            currency=session.cart.currency,
        )
    except PaymentError as exc:
        record(
            "preview_failed",
            session=session.id,
            merchant_id=session.merchant_id,
            outcome=exc.code,
        )
        return ToolResult.failure(exc.message, code=exc.code)

    preview = new_preview(
        preview_id=str(response.get("preview_id")),
        cart=session.cart,
        subtotal=float(response.get("subtotal", session.cart.subtotal)),
        tax=float(response.get("tax", 0.0)),
        total=float(response.get("total", session.cart.subtotal)),
        ttl_seconds=settings.preview_ttl_seconds,
    )
    session.active_preview = preview
    # A fresh preview always supersedes any earlier authorisation.
    session.confirmation_token = None
    session.touch()

    record(
        "preview_created",
        session=session.id,
        merchant_id=session.merchant_id,
        preview_id=preview.preview_id,
        cart_hash=preview.cart_hash,
        cart=[i.model_dump() for i in preview.items],
        total=preview.total,
        currency=preview.currency,
        outcome="ok",
    )

    return ToolResult(
        llm_content=(
            f"Preview {preview.preview_id} is ready: {preview.subtotal:g} plus "
            f"{preview.tax:g} tax = {preview.total:g} {preview.currency}. It expires at "
            f"{preview.expires_at.isoformat()}. The shopper must press confirm themselves. "
            "Tell them what the total is and that nothing has been charged yet. Do not "
            "claim the purchase is complete."
        ),
        events=[
            PreviewEvent(
                preview_id=preview.preview_id,
                items=[i.model_dump() for i in preview.items],
                subtotal=preview.subtotal,
                tax=preview.tax,
                total=preview.total,
                currency=preview.currency,
                expires_at=preview.expires_at.isoformat(),
            )
        ],
        summary=f"total {preview.total:g} {preview.currency}",
    )
