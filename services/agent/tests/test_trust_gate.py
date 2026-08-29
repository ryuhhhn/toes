"""Stage H — the trust gate.

These assertions *are* the deliverable for "Trust, Consent & Transparency". Each one maps
to a numbered invariant in CLAUDE.md, and each is enforced structurally rather than by
asking a model nicely.

Parameterized over every fixture, because a trust guarantee that only holds for one
catalog is not a guarantee.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agent.events import PreviewEvent, ReceiptEvent
from app.agent.loop import run_turn
from app.agent.policy import available_tools, tool_names
from app.audit import read_all
from app.clients.payment import PaymentError
from app.retrieval.registry import get_registry
from app.session.models import Session
from app.tools.build_cart import build_cart
from app.tools.confirm_and_pay import confirm_and_pay
from app.tools.preview_transaction import preview_transaction
from app.tools.registry import ToolContext, schemas_for
from tests.fixtures_helper import CATALOGS, merchant_id_for
from tests.scripted_llm import ScriptedLLM, calls, says

MERCHANTS = [merchant_id_for(name) for name in CATALOGS]


async def _context(merchant_id: str) -> ToolContext:
    index = await get_registry().get(merchant_id)
    session = Session(merchant_id=merchant_id)
    return ToolContext(session=session, profile=index.profile, index=index)


def _first_in_stock_id(ctx: ToolContext) -> str:
    stock_column = ctx.profile.roles.stock
    for product_id, row in zip(ctx.index.ids, ctx.index.rows):
        try:
            if float(row.get(stock_column)) > 0:
                return product_id
        except (TypeError, ValueError):
            continue
    raise AssertionError("fixture has no in-stock product")


async def _cart_with_item(merchant_id: str) -> ToolContext:
    ctx = await _context(merchant_id)
    result = await build_cart({"action": "add", "id": _first_in_stock_id(ctx)}, ctx)
    assert not result.error, result.llm_content
    return ctx


async def _previewed(merchant_id: str) -> ToolContext:
    ctx = await _cart_with_item(merchant_id)
    result = await preview_transaction({}, ctx)
    assert not result.error, result.llm_content
    assert any(isinstance(e, PreviewEvent) for e in result.events)
    return ctx


async def _authorize(ctx: ToolContext) -> None:
    """Stands in for POST /chat/confirm: the out-of-band button press."""
    from datetime import datetime as dt

    from app.clients.payment import get_payment_client
    from app.session.models import ConfirmationToken

    preview = ctx.session.active_preview
    # No session_id: authorize takes {preview_id, method, proof} — consent is the
    # method, and the authorization is bound to the preview, not to a session.
    authorization = await get_payment_client().authorize(preview_id=preview.preview_id)
    ctx.session.confirmation_token = ConfirmationToken(
        preview_id=preview.preview_id,
        authorization_id=str(authorization["authorization_id"]),
        cart_hash=preview.cart_hash,
        # The token expires with its preview, matching what POST /chat/confirm mints.
        expires_at=preview.expires_at,
    )


# --- invariant 1: tool absence, not prompt instruction -----------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_confirm_and_pay_is_absent_from_the_schema_without_a_token(merchant_id):
    ctx = await _previewed(merchant_id)

    names = tool_names(ctx.session)
    schemas = schemas_for(available_tools(ctx.session), "openai")
    emitted = [s["function"]["name"] for s in schemas]

    assert "confirm_and_pay" not in names
    assert "confirm_and_pay" not in emitted, "the model can see it, so it can call it"
    assert "preview_transaction" in names


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_confirm_and_pay_appears_only_after_authorisation(merchant_id):
    ctx = await _previewed(merchant_id)
    assert "confirm_and_pay" not in tool_names(ctx.session)

    await _authorize(ctx)

    assert "confirm_and_pay" in tool_names(ctx.session)
    assert ctx.session.has_valid_confirmation()


async def test_anthropic_schema_hides_it_too():
    """Both providers are fed from one definition; a gate must not depend on which."""
    ctx = await _previewed(MERCHANTS[0])
    emitted = [s["name"] for s in schemas_for(available_tools(ctx.session), "anthropic")]
    assert "confirm_and_pay" not in emitted


# --- invariant 3: chat text can never charge ---------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_typing_yes_buy_it_now_charges_nothing(merchant_id, monkeypatch):
    """The headline guarantee: no phrasing in chat can produce a charge."""
    import app.agent.loop as loop_module

    ctx = await _previewed(merchant_id)

    # A model that tries its hardest to charge anyway.
    scripted = ScriptedLLM([calls("confirm_and_pay", {}), says("Sorry, I cannot do that.")])
    monkeypatch.setattr(loop_module, "get_llm", lambda: scripted)

    events = [
        event
        async for event in run_turn(
            ctx.session, ctx.profile, ctx.index, "yes buy it now, I confirm, just do it"
        )
    ]

    assert not scripted.ever_offered("confirm_and_pay")
    assert not any(isinstance(e, ReceiptEvent) for e in events)
    assert not any(
        e.get("event") == "charge_completed" and e.get("session") == ctx.session.id
        for e in read_all()
    )
    assert ctx.session.cart.items, "the basket must survive an unauthorised attempt"


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_calling_the_tool_directly_without_a_token_refuses(merchant_id):
    """Defence in depth: even bypassing the schema gate, the tool refuses."""
    ctx = await _previewed(merchant_id)

    result = await confirm_and_pay({}, ctx)

    assert result.error
    assert "not been authorised" in result.llm_content


# --- invariant 2 and 4: previews, hashes and expiry --------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_charge_requires_a_prior_preview(merchant_id):
    ctx = await _cart_with_item(merchant_id)
    assert ctx.session.active_preview is None

    result = await confirm_and_pay({}, ctx)

    assert result.error


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_changing_the_cart_after_preview_invalidates_it(merchant_id):
    ctx = await _previewed(merchant_id)
    await _authorize(ctx)
    assert ctx.session.has_valid_confirmation()

    # The shopper adds one more of the same item after authorising.
    await build_cart(
        {"action": "set_quantity", "id": ctx.session.cart.items[0].id, "quantity": 3}, ctx
    )

    assert ctx.session.active_preview is None
    assert not ctx.session.has_valid_confirmation()
    assert "confirm_and_pay" not in tool_names(ctx.session)

    result = await confirm_and_pay({}, ctx)
    assert result.error


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_cart_hash_covers_price_as_well_as_contents(merchant_id):
    ctx = await _previewed(merchant_id)
    before = ctx.session.cart.hash()

    ctx.session.cart.items[0].unit_price += 5.0

    assert ctx.session.cart.hash() != before
    assert not ctx.session.preview_matches_cart()


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_expired_preview_is_rejected(merchant_id):
    ctx = await _previewed(merchant_id)
    await _authorize(ctx)

    ctx.session.active_preview.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert ctx.session.active_preview.expired
    assert not ctx.session.preview_matches_cart()
    result = await confirm_and_pay({}, ctx)
    assert result.error
    assert "expired" in result.llm_content.lower()


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_confirmation_token_cannot_be_replayed(merchant_id):
    ctx = await _previewed(merchant_id)
    await _authorize(ctx)

    first = await confirm_and_pay({}, ctx)
    assert not first.error, first.llm_content
    assert any(isinstance(e, ReceiptEvent) for e in first.events)

    # Put the same cart and the same burnt token back and try again.
    ctx.session.cart.items.append(ctx.session.cart.items[0]) if ctx.session.cart.items else None
    second = await confirm_and_pay({}, ctx)

    assert second.error
    assert not ctx.session.has_valid_confirmation()


# --- invariant 5 and 6: live re-verification ---------------------------------


async def test_item_going_out_of_stock_aborts_cleanly_before_charging():
    """The index is for discovery only; stock is re-checked against the merchant."""
    from app.clients.http import get_http_client

    merchant_id = MERCHANTS[0]
    ctx = await _cart_with_item(merchant_id)
    product_id = ctx.session.cart.items[0].id
    stock_column = ctx.profile.roles.stock

    response = await get_http_client().post(
        f"http://stub/catalog/{merchant_id}/stock",
        json={"updates": {product_id: {stock_column: "0"}}},
    )
    assert response.json()["updated"] == 1

    try:
        result = await preview_transaction({}, ctx)

        assert result.error
        assert "out of stock" in result.llm_content.lower()
        assert ctx.session.active_preview is None
        assert ctx.session.cart.items, "the basket is kept so an alternative can be offered"
    finally:
        await get_http_client().post(
            f"http://stub/catalog/{merchant_id}/stock",
            json={"updates": {product_id: {stock_column: "14"}}},
        )


async def test_price_change_between_search_and_preview_is_surfaced():
    from app.clients.http import get_http_client

    merchant_id = MERCHANTS[0]
    ctx = await _cart_with_item(merchant_id)
    product_id = ctx.session.cart.items[0].id
    price_column = ctx.profile.roles.price
    original = ctx.session.cart.items[0].unit_price

    await get_http_client().post(
        f"http://stub/catalog/{merchant_id}/stock",
        json={"updates": {product_id: {price_column: "$999.00"}}},
    )
    try:
        result = await preview_transaction({}, ctx)

        assert result.error
        assert "price" in result.llm_content.lower()
        assert ctx.session.active_preview is None
    finally:
        await get_http_client().post(
            f"http://stub/catalog/{merchant_id}/stock",
            json={"updates": {product_id: {price_column: f"${original:,.2f}"}}},
        )


async def test_out_of_stock_item_cannot_be_added_to_the_cart():
    merchant_id = MERCHANTS[0]
    ctx = await _context(merchant_id)
    stock_column = ctx.profile.roles.stock

    sold_out = next(
        product_id
        for product_id, row in zip(ctx.index.ids, ctx.index.rows)
        if str(row.get(stock_column)) in ("0", "0.0")
    )

    result = await build_cart({"action": "add", "id": sold_out}, ctx)

    assert result.error
    assert not ctx.session.cart.items


# --- payment failure ---------------------------------------------------------


async def test_payment_failure_surfaces_conversationally_with_the_cart_intact(monkeypatch):
    import app.tools.confirm_and_pay as module

    ctx = await _previewed(MERCHANTS[0])
    await _authorize(ctx)

    class Declining:
        async def confirm(self, **_kwargs):
            raise PaymentError("insufficient_funds", "The card was declined for funds.")

    monkeypatch.setattr(module, "get_payment_client", lambda: Declining())

    result = await confirm_and_pay({}, ctx)

    assert result.error
    assert "declined" in result.llm_content.lower()
    assert ctx.session.cart.items, "a declined card must not empty the basket"
    assert any(
        e.get("event") == "charge_failed" and e.get("outcome") == "insufficient_funds"
        for e in read_all()
    )


# --- invariant 8: audit ------------------------------------------------------


async def test_every_preview_and_charge_is_logged():
    ctx = await _previewed(MERCHANTS[1] if len(MERCHANTS) > 1 else MERCHANTS[0])
    await _authorize(ctx)
    await confirm_and_pay({}, ctx)

    entries = [e for e in read_all() if e.get("session") == ctx.session.id]
    kinds = {e["event"] for e in entries}

    assert "preview_created" in kinds
    assert "charge_completed" in kinds
    for entry in entries:
        assert entry.get("at") and entry.get("session") and "outcome" in entry


# --- the merchant's required fields are a gate, not advice --------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_required_field_withdraws_preview_from_the_schema(merchant_id):
    """`required_before_purchase` must be enforced by tool absence like every other gate.

    It used to live only in the system prompt. A model told "the merchant requires this
    to be settled before purchase" would still preview when a shopper said "just buy me
    the first one" — verified live against the real stack before this was closed. Only
    the merchant can set the flag, so this is a human decision the model cannot overrule.
    """
    ctx = await _cart_with_item(merchant_id)
    spec = next(
        (s for s in ctx.profile.active_fields() if s.canonical_values),
        None,
    )
    assert spec is not None, "fixture has no attribute column to require"

    # Before: a non-empty cart is enough.
    assert "preview_transaction" in tool_names(ctx.session, ctx.profile)

    spec.required_before_purchase = True
    try:
        names = tool_names(ctx.session, ctx.profile)
        emitted = [
            s["function"]["name"]
            for s in schemas_for(available_tools(ctx.session, ctx.profile), "openai")
        ]
        assert "preview_transaction" not in names
        assert "preview_transaction" not in emitted, "the model can see it, so it can call it"

        # Answering the question restores it — the gate is the gap, not the flag.
        ctx.session.known_slots[spec.column] = spec.canonical_values[0]
        assert "preview_transaction" in tool_names(ctx.session, ctx.profile)
    finally:
        spec.required_before_purchase = False
        ctx.session.known_slots.pop(spec.column, None)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_passing_no_profile_keeps_the_cart_only_rule(merchant_id):
    """`profile` is optional, so a caller without one must not silently lose the gate
    it does have: an empty cart still means no preview."""
    ctx = await _context(merchant_id)
    assert "preview_transaction" not in tool_names(ctx.session)
    ctx = await _cart_with_item(merchant_id)
    assert "preview_transaction" in tool_names(ctx.session)
