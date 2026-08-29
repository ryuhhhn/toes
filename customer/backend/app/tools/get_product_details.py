"""get_product_details — one product, rendered generically from the profile."""

from __future__ import annotations

from app.agent.events import ProductsEvent, product_card
from app.tools.registry import ToolContext, ToolResult, object_schema, tool


@tool(
    name="get_product_details",
    description=(
        "Look up everything known about one product by its id. Use this when the shopper "
        "asks about a specific item they have seen."
    ),
    start_summary="Looking up the details",
    parameters=object_schema(
        {"id": {"type": "string", "description": "The product id, as shown on its card."}},
        required=["id"],
    ),
)
async def get_product_details(args: dict, ctx: ToolContext) -> ToolResult:
    product_id = str(args.get("id") or "").strip()
    row = ctx.index.row_by_id(product_id)

    if row is None:
        return ToolResult.failure(
            f"No product with id {product_id!r} is in this catalogue.", code="unknown_product"
        )

    card = product_card(
        row, ctx.profile, max_attributes=20, prefer=list(ctx.session.known_slots)
    )

    details = "\n".join(f"- {a.label}: {a.display}" for a in card.attributes)
    price = f"{card.price:g} {card.currency}" if card.price is not None else "price unavailable"
    stock = "in stock" if card.in_stock else "OUT OF STOCK — do not recommend this"

    content = (
        f"[{card.id}] {card.title}\nPrice: {price}\nAvailability: {stock}\n"
        f"{card.description or ''}\n{details}"
    )

    return ToolResult(
        llm_content=content,
        events=[ProductsEvent(items=[card], total_candidates=1)],
        summary=card.title,
    )
