"""SSE event schema — the cross-team contract with Frontend.

The frontend renders structured UI from these typed events, never by parsing prose out of
the token stream. Coordinate before changing any payload here.

ProductCard is deliberately generic: roles come from the profile, and everything else lands
in an `attributes` list the frontend renders without knowing what any of it means. A card
that assumed a category-specific key would break the moment a different catalog is loaded.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.profile import AgentProfile


class Attribute(BaseModel):
    """One rendered attribute. `label` is the merchant-approved layman name."""

    column: str
    label: str
    value: Any
    unit: str | None = None
    display: str = ""


class ProductCard(BaseModel):
    id: str
    title: str
    price: float | None = None
    currency: str = "USD"
    image: str | None = None
    description: str | None = None
    in_stock: bool = True
    stock: float | None = None
    attributes: list[Attribute] = Field(default_factory=list)
    score: float | None = None


def _display(value: Any, unit: str | None) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value} {unit}".strip() if unit else str(value)


def product_card(
    row: dict,
    profile: AgentProfile,
    *,
    score: float | None = None,
    max_attributes: int = 6,
    prefer: list[str] | None = None,
) -> ProductCard:
    """Build a card from the profile's roles. No category-specific keys anywhere.

    `prefer` lets the caller surface the attributes this shopper has actually talked about
    ahead of the generic tier order.
    """
    roles = profile.roles
    prefer = prefer or []

    price = None
    if roles.price and row.get(roles.price) is not None:
        try:
            price = float(row[roles.price])
        except (TypeError, ValueError):
            price = None

    stock = None
    in_stock = True
    if roles.stock and row.get(roles.stock) is not None:
        raw = row[roles.stock]
        if isinstance(raw, bool):
            in_stock, stock = raw, None
        else:
            try:
                stock = float(raw)
                in_stock = stock > 0
            except (TypeError, ValueError):
                stock, in_stock = None, True

    description = None
    for column in roles.text:
        if row.get(column):
            description = str(row[column])
            break

    currency = "USD"
    price_spec = profile.field(roles.price) if roles.price else None
    if price_spec and price_spec.currency:
        currency = price_spec.currency

    ordered = sorted(
        profile.active_fields(),
        key=lambda spec: (0 if spec.column in prefer else 1, spec.tier, spec.column),
    )

    attributes: list[Attribute] = []
    for spec in ordered:
        if spec.column in (roles.price, roles.stock):
            continue
        value = row.get(spec.column)
        if value is None or value == "" or value == []:
            continue
        attributes.append(
            Attribute(
                column=spec.column,
                label=spec.display_name,
                value=value,
                unit=spec.unit,
                display=_display(value, spec.unit),
            )
        )
        if len(attributes) >= max_attributes:
            break

    return ProductCard(
        id=str(row.get(roles.id, "")),
        title=str(row.get(roles.title, "")) if roles.title else "",
        price=price,
        currency=currency,
        image=str(row[roles.image]) if roles.image and row.get(roles.image) else None,
        description=description,
        in_stock=in_stock,
        stock=stock,
        attributes=attributes,
        score=score,
    )


# --- events ------------------------------------------------------------------


class Event(BaseModel):
    type: str

    def sse(self) -> str:
        payload = self.model_dump(exclude={"type"}, mode="json")
        return f"event: {self.type}\ndata: {json.dumps(payload, default=str)}\n\n"


class TokenEvent(Event):
    type: Literal["token"] = "token"
    text: str


class ToolStartEvent(Event):
    type: Literal["tool_start"] = "tool_start"
    tool: str
    summary: str = ""


class ProductsEvent(Event):
    type: Literal["products"] = "products"
    items: list[ProductCard] = Field(default_factory=list)
    filters_applied: list[dict] = Field(default_factory=list)
    filters_relaxed: list[dict] = Field(default_factory=list)
    total_candidates: int = 0
    note: str | None = None


class ComparisonEvent(Event):
    type: Literal["comparison"] = "comparison"
    axes: list[dict] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)


class ProbeEvent(Event):
    type: Literal["probe"] = "probe"
    attribute: str
    question: str
    why_it_matters: str | None = None
    how_to_find_out: str | None = None
    options: list[str] = Field(default_factory=list)


class PreviewEvent(Event):
    type: Literal["preview"] = "preview"
    preview_id: str
    items: list[dict] = Field(default_factory=list)
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    currency: str = "USD"
    expires_at: str = ""


class ReceiptEvent(Event):
    type: Literal["receipt"] = "receipt"
    transaction_id: str
    items: list[dict] = Field(default_factory=list)
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    currency: str = "USD"
    timestamp: str = ""


class CartEvent(Event):
    type: Literal["cart"] = "cart"
    items: list[dict] = Field(default_factory=list)
    subtotal: float = 0.0
    currency: str = "USD"


class NoticeEvent(Event):
    """A merchant-approved cross-field warning. Never model-authored prose."""

    type: Literal["notice"] = "notice"
    level: str = "info"
    message: str
    columns: list[str] = Field(default_factory=list)


class ErrorEvent(Event):
    type: Literal["error"] = "error"
    code: str
    message: str


class DoneEvent(Event):
    type: Literal["done"] = "done"
    turn_id: str
