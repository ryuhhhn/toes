"""build_cart — add, remove, set quantity.

Every mutation invalidates any outstanding preview (invariant 4). Doing it here, at the
point of change, rather than checking at confirm time, means there is no window in which a
stale preview looks valid.
"""

from __future__ import annotations

import logging

from app.agent.events import CartEvent
from app.session.models import CartItem
from app.tools.registry import ToolContext, ToolResult, object_schema, tool

log = logging.getLogger(__name__)

MAX_QUANTITY = 99


def _price_of(row: dict, ctx: ToolContext) -> float | None:
    column = ctx.profile.roles.price
    if not column:
        return None
    try:
        return float(row[column])
    except (TypeError, ValueError, KeyError):
        return None


def _stock_of(row: dict, ctx: ToolContext) -> float | None:
    column = ctx.profile.roles.stock
    if not column or row.get(column) is None:
        return None
    value = row[column]
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cart_event(ctx: ToolContext) -> CartEvent:
    cart = ctx.session.cart
    return CartEvent(
        items=[i.model_dump() for i in cart.items],
        subtotal=cart.subtotal,
        currency=cart.currency,
    )


@tool(
    name="build_cart",
    description=(
        "Add a product to the basket, change how many, or remove one. Adding something "
        "does not buy it — the shopper still has to review a transaction preview and "
        "press confirm themselves."
    ),
    start_summary="Updating the basket",
    parameters=object_schema(
        {
            "action": {
                "type": "string",
                "enum": ["add", "remove", "set_quantity", "clear"],
                "description": "What to do to the basket.",
            },
            "id": {"type": "string", "description": "Product id. Required except for clear."},
            "quantity": {"type": "integer", "description": "How many. Defaults to 1."},
        },
        required=["action"],
    ),
)
async def build_cart(args: dict, ctx: ToolContext) -> ToolResult:
    session = ctx.session
    action = str(args.get("action") or "").strip().lower()
    product_id = str(args.get("id") or "").strip()
    quantity = int(args.get("quantity") or 1)

    if action == "clear":
        session.cart.items.clear()
        session.invalidate_preview("cart cleared")
        return ToolResult(
            llm_content="The basket is now empty.",
            events=[cart_event(ctx)],
            summary="cart cleared",
        )

    if not product_id:
        return ToolResult.failure("A product id is required.", code="bad_request")

    if action == "remove":
        removed = session.cart.remove(product_id)
        session.invalidate_preview("item removed")
        if not removed:
            return ToolResult.failure(f"{product_id} was not in the basket.", code="not_in_cart")
        return ToolResult(
            llm_content=f"Removed {product_id}. Basket total is now {session.cart.subtotal:g}.",
            events=[cart_event(ctx)],
            summary=f"removed {product_id}",
        )

    row = ctx.index.row_by_id(product_id)
    if row is None:
        return ToolResult.failure(
            f"No product with id {product_id!r} is in this catalogue.", code="unknown_product"
        )

    if action == "set_quantity":
        if not session.cart.find(product_id):
            return ToolResult.failure(f"{product_id} is not in the basket.", code="not_in_cart")
        session.cart.set_quantity(product_id, min(quantity, MAX_QUANTITY))
        session.invalidate_preview("quantity changed")
        return ToolResult(
            llm_content=f"Set {product_id} to {quantity}. Subtotal {session.cart.subtotal:g}.",
            events=[cart_event(ctx)],
            summary=f"{product_id} x{quantity}",
        )

    if action != "add":
        return ToolResult.failure(f"Unknown basket action {action!r}.", code="bad_request")

    # Invariant 6, enforced at the point of sale as well as in search.
    stock = _stock_of(row, ctx)
    if stock is not None and stock <= 0:
        return ToolResult.failure(
            "That item is out of stock, so it cannot be added. Offer an alternative.",
            code="out_of_stock",
        )

    price = _price_of(row, ctx)
    if price is None:
        return ToolResult.failure(
            "That item has no usable price, so it cannot be sold. Say so plainly.",
            code="no_price",
        )

    quantity = max(1, min(quantity, MAX_QUANTITY))
    if stock is not None and quantity > stock:
        quantity = int(stock)

    roles = ctx.profile.roles
    price_spec = ctx.profile.field(roles.price) if roles.price else None

    item = CartItem(
        id=product_id,
        title=str(row.get(roles.title, "")) if roles.title else product_id,
        quantity=quantity,
        unit_price=price,
        currency=(price_spec.currency if price_spec and price_spec.currency else "USD"),
        image=str(row[roles.image]) if roles.image and row.get(roles.image) else None,
    )
    session.cart.add(item)
    session.invalidate_preview("item added")

    return ToolResult(
        llm_content=(
            f"Added {quantity} x {item.title} at {item.unit_price:g} {item.currency}. "
            f"Basket subtotal is {session.cart.subtotal:g} {session.cart.currency}. "
            "Nothing has been charged. Offer to show the transaction preview."
        ),
        events=[cart_event(ctx)],
        summary=f"added {item.title}",
    )
