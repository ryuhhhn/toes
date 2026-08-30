"""confirm_and_pay — the only tool that moves money.

It is absent from the model's tool list unless a valid confirmation token exists
(invariant 1), and a token can only be minted by POST /chat/confirm — a separate HTTP
request from the shopper's own button press (invariant 3). No amount of the shopper typing
"yes buy it" can produce one.

The checks below are therefore defence in depth rather than the gate itself. They run
anyway, because a gate you can only see in one place is a gate that eventually moves.
"""

from __future__ import annotations

import logging

from app.agent.events import ReceiptEvent
from app.audit import record
from app.clients.merchant import MerchantUnavailable, get_merchant_client
from app.clients.payment import PaymentError, get_payment_client
from app.tools.registry import ToolContext, ToolResult, object_schema, tool

log = logging.getLogger(__name__)


async def _decrement_stock(ctx: ToolContext, items) -> None:
    """Take what was just sold out of the merchant's stock. Best-effort, always.

    Without this the shop never learns it made a sale: the merchant console shows the
    stock the spreadsheet was uploaded with, forever, and a one-of-a-kind item stays
    buyable by the next shopper until somebody re-uploads by hand.

    Three things make this safe to run after the charge rather than before it:

    - The charge has ALREADY been captured. Money moved. A failed inventory write is a
      bookkeeping problem, and raising here would turn it into a lost receipt — so every
      failure is logged and swallowed.
    - We send ABSOLUTE values, not deltas, computed from rows read moments earlier. The
      merchant writes cells; it does no arithmetic and holds no notion of stock (§0).
    - Stock is re-verified against these same rows immediately before every charge
      (invariant 5), so a value that races another sale cannot let one through.
    """
    stock_column = ctx.profile.roles.stock
    if not stock_column:
        return  # this catalogue does not track stock; nothing to write

    client = get_merchant_client()
    merchant_id = ctx.session.merchant_id
    ids = [item.id for item in items]

    try:
        live = await client.fetch_by_ids(merchant_id, ids)
    except MerchantUnavailable as exc:
        log.warning("stock writeback skipped for %s: %s", merchant_id, exc)
        return

    updates: dict[str, dict[str, object]] = {}
    for item in items:
        row = live.get(item.id)
        if row is None:
            continue
        try:
            remaining = float(row.get(stock_column))
        except (TypeError, ValueError):
            # A stock cell we cannot read is one we must not overwrite: "in stock" and
            # "sold out" are legitimate values in a merchant's own spreadsheet.
            continue
        new_value = max(0, int(remaining) - item.quantity)
        updates[item.id] = {stock_column: str(new_value)}

    if not updates:
        return

    try:
        written = await client.adjust_stock(merchant_id, updates)
        log.info("stock writeback: %d row(s) for %s", written, merchant_id)
    except MerchantUnavailable as exc:
        log.warning("stock writeback failed for %s: %s", merchant_id, exc)


@tool(
    name="confirm_and_pay",
    description=(
        "Complete the purchase the shopper has already authorised. Only ever available "
        "after they have pressed confirm on a transaction preview."
    ),
    start_summary="Completing the payment",
    parameters=object_schema({}),
)
async def confirm_and_pay(args: dict, ctx: ToolContext) -> ToolResult:
    session = ctx.session
    token = session.confirmation_token
    preview = session.active_preview

    if token is None or not token.valid:
        return ToolResult.failure(
            "This purchase has not been authorised by the shopper. Ask them to press "
            "confirm on the preview.",
            code="not_authorized",
        )
    if preview is None or preview.expired:
        return ToolResult.failure(
            "The transaction preview has expired. Offer to prepare a fresh one.",
            code="preview_expired",
        )
    # Invariant 4, checked again at the last possible moment.
    if token.cart_hash != session.cart.hash():
        session.invalidate_preview("cart changed after authorisation")
        return ToolResult.failure(
            "The basket changed after it was authorised, so this charge was stopped. "
            "Offer a fresh preview.",
            code="cart_changed",
        )

    try:
        transaction = await get_payment_client().confirm(
            preview_id=preview.preview_id,
            authorization_id=token.authorization_id,
            fail=session.inject_failure,
        )
    except PaymentError as exc:
        record(
            "charge_failed",
            session=session.id,
            merchant_id=session.merchant_id,
            preview_id=preview.preview_id,
            cart=[i.model_dump() for i in session.cart.items],
            outcome=exc.code,
            message=exc.message,
        )
        # The cart is deliberately left intact so the shopper can try another way to pay.
        return ToolResult.failure(exc.message, code=exc.code)

    session.burn_token()  # single use, always
    transaction_id = str(transaction.get("transaction_id"))

    # The response is a Transaction: the money field is `amount`, the clock field is
    # `created_at`, and it carries no line items at all. The receipt's lines come from
    # the preview the shopper actually saw and authorised — which the session holds,
    # and which is the honest answer to "what did I just buy?" regardless of what the
    # payment service chooses to echo back.
    total = float(transaction.get("amount", preview.total))
    currency = str(transaction.get("currency") or preview.currency)
    charged_at = str(transaction.get("created_at", ""))
    items = [i.model_dump() for i in preview.items]

    record(
        "charge_completed",
        session=session.id,
        merchant_id=session.merchant_id,
        preview_id=preview.preview_id,
        transaction_id=transaction_id,
        cart=[i.model_dump() for i in preview.items],
        total=total,
        currency=currency,
        outcome="captured",
    )

    # After the money, before the receipt: the shop should not still be advertising what
    # it just sold. Never raises — see _decrement_stock.
    await _decrement_stock(ctx, preview.items)

    session.cart.items.clear()
    session.active_preview = None
    session.touch()

    return ToolResult(
        llm_content=(
            f"Payment complete. Transaction {transaction_id} for "
            f"{total:g} {currency}. Confirm this warmly and "
            "briefly, and mention the transaction id."
        ),
        events=[
            ReceiptEvent(
                transaction_id=transaction_id,
                items=items,
                subtotal=preview.subtotal,
                tax=preview.tax,
                total=total,
                currency=currency,
                timestamp=charged_at,
            )
        ],
        summary=f"charged {total:g} {currency}",
    )
