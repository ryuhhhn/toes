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
from app.clients.payment import PaymentError, get_payment_client
from app.tools.registry import ToolContext, ToolResult, object_schema, tool

log = logging.getLogger(__name__)


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
        receipt = await get_payment_client().confirm(
            preview_id=preview.preview_id,
            authorization_id=token.authorization_id,
            session_id=session.id,
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
    transaction_id = str(receipt.get("transaction_id"))

    record(
        "charge_completed",
        session=session.id,
        merchant_id=session.merchant_id,
        preview_id=preview.preview_id,
        transaction_id=transaction_id,
        cart=[i.model_dump() for i in preview.items],
        total=receipt.get("total"),
        currency=receipt.get("currency"),
        outcome="captured",
    )

    session.cart.items.clear()
    session.active_preview = None
    session.touch()

    return ToolResult(
        llm_content=(
            f"Payment complete. Transaction {transaction_id} for "
            f"{receipt.get('total')} {receipt.get('currency')}. Confirm this warmly and "
            "briefly, and mention the transaction id."
        ),
        events=[
            ReceiptEvent(
                transaction_id=transaction_id,
                items=receipt.get("items", []),
                subtotal=float(receipt.get("subtotal", 0.0)),
                tax=float(receipt.get("tax", 0.0)),
                total=float(receipt.get("total", 0.0)),
                currency=str(receipt.get("currency", "USD")),
                timestamp=str(receipt.get("timestamp", "")),
            )
        ],
        summary=f"charged {receipt.get('total')} {receipt.get('currency')}",
    )
