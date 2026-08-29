"""Session state.

`last_candidate_ids` is the load-bearing field: probing ranks attributes over the *live*
candidate set, not the whole catalog, which is what makes the question asked actually
relevant to what is currently on screen. It is repopulated on every search.

The preview and confirmation token structures encode the trust gate. Both carry an expiry
and the cart hash they were minted against, so a cart edited after a preview cannot be
charged against that preview.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.llm.base import AssistantMsg, Msg, ToolResultMsg, UserMsg


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class CartItem(BaseModel):
    id: str
    title: str = ""
    quantity: int = 1
    unit_price: float = 0.0
    currency: str = "USD"
    image: str | None = None

    @property
    def line_total(self) -> float:
        return round(self.unit_price * self.quantity, 2)


class Cart(BaseModel):
    items: list[CartItem] = Field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round(sum(i.line_total for i in self.items), 2)

    @property
    def currency(self) -> str:
        return self.items[0].currency if self.items else "USD"

    def find(self, product_id: str) -> CartItem | None:
        return next((i for i in self.items if i.id == str(product_id)), None)

    def add(self, item: CartItem) -> CartItem:
        existing = self.find(item.id)
        if existing is None:
            self.items.append(item)
            return item
        existing.quantity += item.quantity
        existing.unit_price = item.unit_price  # always the freshest verified price
        return existing

    def set_quantity(self, product_id: str, quantity: int) -> bool:
        item = self.find(product_id)
        if item is None:
            return False
        if quantity <= 0:
            self.items.remove(item)
        else:
            item.quantity = quantity
        return True

    def remove(self, product_id: str) -> bool:
        item = self.find(product_id)
        if item is None:
            return False
        self.items.remove(item)
        return True

    def hash(self) -> str:
        """Identity of *what is being bought*, for invariant 4.

        Covers id, quantity and unit price: a price change between preview and confirm is
        as much a reason to re-preview as an added item.
        """
        payload = sorted(
            (i.id, i.quantity, round(i.unit_price, 2)) for i in self.items
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]


class ActivePreview(BaseModel):
    preview_id: str
    cart_hash: str
    items: list[CartItem] = Field(default_factory=list)
    subtotal: float = 0.0
    tax: float = 0.0
    total: float = 0.0
    currency: str = "USD"
    expires_at: datetime
    created_at: datetime = Field(default_factory=_now)

    @property
    def expired(self) -> bool:
        return _now() >= self.expires_at


class ConfirmationToken(BaseModel):
    """Minted only by POST /chat/confirm, and burnt on use.

    Its existence is what makes confirm_and_pay visible to the model at all. Chat text can
    never create one.
    """

    token: str = Field(default_factory=lambda: new_id("cnf"))
    preview_id: str
    authorization_id: str
    cart_hash: str
    expires_at: datetime
    used: bool = False

    @property
    def valid(self) -> bool:
        return not self.used and _now() < self.expires_at


class ToolCallRecord(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    ok: bool = True
    summary: str = ""
    at: datetime = Field(default_factory=_now)


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("ses"))
    merchant_id: str

    messages: list[dict] = Field(default_factory=list)
    cart: Cart = Field(default_factory=Cart)

    known_slots: dict[str, Any] = Field(default_factory=dict)
    asked_slots: list[str] = Field(default_factory=list)
    declined_slots: list[str] = Field(default_factory=list)
    probe_count: int = 0

    last_candidate_ids: list[str] = Field(default_factory=list)
    last_shown_ids: list[str] = Field(default_factory=list)

    active_preview: ActivePreview | None = None
    confirmation_token: ConfirmationToken | None = None

    tool_history: list[ToolCallRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # --- conversation ---------------------------------------------------------

    def touch(self) -> None:
        self.updated_at = _now()

    def append(self, message: Msg) -> None:
        self.messages.append(_serialise(message))
        self.touch()

    def llm_messages(self) -> list[Msg]:
        return [_deserialise(m) for m in self.messages]

    # --- slots ----------------------------------------------------------------

    def answered_slots(self) -> set[str]:
        return set(self.known_slots) | set(self.asked_slots) | set(self.declined_slots)

    def record_answer(self, column: str, value: Any) -> None:
        if value is None:
            self.declined_slots.append(column)
        else:
            self.known_slots[column] = value
        if column not in self.asked_slots:
            self.asked_slots.append(column)
        self.touch()

    def record_asked(self, columns: list[str]) -> None:
        for column in columns:
            if column not in self.asked_slots:
                self.asked_slots.append(column)
        self.probe_count += len(columns)
        self.touch()

    # --- trust gate -----------------------------------------------------------

    def preview_matches_cart(self) -> bool:
        return (
            self.active_preview is not None
            and not self.active_preview.expired
            and self.active_preview.cart_hash == self.cart.hash()
        )

    def invalidate_preview(self, reason: str = "") -> None:
        """Any cart mutation drops the preview and any token minted against it."""
        self.active_preview = None
        self.confirmation_token = None
        self.touch()

    def burn_token(self) -> None:
        if self.confirmation_token is not None:
            self.confirmation_token.used = True
        self.touch()

    def has_valid_confirmation(self) -> bool:
        token = self.confirmation_token
        return bool(
            token
            and token.valid
            and self.active_preview
            and token.preview_id == self.active_preview.preview_id
            and token.cart_hash == self.cart.hash()
        )


def new_preview(
    *, preview_id: str, cart: Cart, subtotal: float, tax: float, total: float, ttl_seconds: int
) -> ActivePreview:
    return ActivePreview(
        preview_id=preview_id,
        cart_hash=cart.hash(),
        items=[i.model_copy(deep=True) for i in cart.items],
        subtotal=subtotal,
        tax=tax,
        total=total,
        currency=cart.currency,
        expires_at=_now() + timedelta(seconds=ttl_seconds),
    )


# --- neutral message (de)serialisation ---------------------------------------


def _serialise(message: Msg) -> dict:
    if isinstance(message, UserMsg):
        return {"role": "user", "content": message.content}
    if isinstance(message, AssistantMsg):
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in message.tool_calls
            ],
        }
    return {
        "role": "tool",
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "content": message.content,
        "is_error": message.is_error,
    }


def _deserialise(raw: dict) -> Msg:
    from app.llm.base import ToolCall

    role = raw.get("role")
    if role == "user":
        return UserMsg(raw.get("content", ""))
    if role == "assistant":
        return AssistantMsg(
            content=raw.get("content", "") or "",
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
                for tc in raw.get("tool_calls", [])
            ],
        )
    return ToolResultMsg(
        tool_call_id=raw.get("tool_call_id", ""),
        name=raw.get("name", ""),
        content=raw.get("content", ""),
        is_error=raw.get("is_error", False),
    )
