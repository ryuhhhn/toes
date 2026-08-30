"""The checkout button, and the sale reaching the shop.

Two failures that both looked like nothing happening:

- The storefront's Checkout button sent the chat message "I'm ready to check out" and
  waited for the model to call `preview_transaction`. The panel stayed shut whenever it
  answered in prose instead — and stayed shut identically for every legitimate refusal,
  because `policy.available_tools` WITHDRAWS the preview tool when the basket is empty
  or a required field is unsettled. Same nothing, four different reasons.
- A completed purchase never reached the merchant's stored rows, so the shop went on
  advertising stock it had already sold and the merchant console showed the numbers the
  spreadsheet was uploaded with, forever.

Parameterized over every fixture, because behaviour that only holds for one catalog is
not behaviour worth relying on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.events import PreviewEvent
from app.clients.merchant import get_merchant_client
from app.main import create_app
from app.retrieval.registry import get_registry
from app.session.models import ConfirmationToken, Session
from app.session.store import get_session_store
from app.tools.build_cart import build_cart
from app.tools.confirm_and_pay import confirm_and_pay
from app.tools.preview_transaction import preview_transaction
from app.tools.registry import ToolContext
from tests.fixtures_helper import CATALOGS, merchant_id_for

MERCHANTS = [merchant_id_for(name) for name in CATALOGS]


@pytest.fixture
def client():
    """No `with`, deliberately: entering the context runs the app's lifespan, and its
    shutdown closes the shared httpx client that conftest pointed at the in-process
    stubs — taking every later test in the session with it."""
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def restore_stub_stock():
    """These tests SELL things, and the stub's catalogs are session-scoped.

    Without this, a test that drains a product to zero silently changes the fixture
    every later test reads.
    """
    from stubs.mock_services import CATALOGS

    snapshot = {m: [dict(r) for r in c.rows] for m, c in CATALOGS.items()}
    yield
    for merchant_id, rows in snapshot.items():
        CATALOGS[merchant_id].rows[:] = [dict(r) for r in rows]


async def _context(merchant_id: str) -> ToolContext:
    index = await get_registry().get(merchant_id)
    session = Session(merchant_id=merchant_id)
    return ToolContext(session=session, profile=index.profile, index=index)


def _in_stock_id(ctx: ToolContext, minimum: float = 1.0) -> str:
    stock_column = ctx.profile.roles.stock
    for product_id, row in zip(ctx.index.ids, ctx.index.rows):
        try:
            if float(row.get(stock_column)) >= minimum:
                return product_id
        except (TypeError, ValueError):
            continue
    raise AssertionError("fixture has no product with enough stock")


async def _session_with_cart(merchant_id: str) -> Session:
    """A session the HTTP layer can find, holding one real product."""
    ctx = await _context(merchant_id)
    result = await build_cart({"action": "add", "id": _in_stock_id(ctx)}, ctx)
    assert not result.error, result.llm_content
    await get_session_store().save(ctx.session)
    return ctx.session


def _events(response) -> list[tuple[str, str]]:
    """(event_type, data) pairs from an SSE body."""
    frames = []
    for frame in response.text.split("\n\n"):
        kind = data = None
        for line in frame.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if kind:
            frames.append((kind, data or ""))
    return frames


# --- POST /chat/checkout ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_checkout_produces_a_preview_with_no_model_in_the_path(client, merchant_id):
    """The whole point: a button press yields a preview, deterministically.

    No LLM is configured in this suite, so a run that reached one would fail — which is
    exactly the assertion. A preview is a function of the cart, not a favour to ask.
    """
    session = await _session_with_cart(merchant_id)

    response = client.post("/chat/checkout", json={"session_id": session.id})
    assert response.status_code == 200

    kinds = [kind for kind, _ in _events(response)]
    assert kinds == ["tool_start", "preview", "done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_checkout_binds_the_preview_to_the_session(client, merchant_id):
    session = await _session_with_cart(merchant_id)
    client.post("/chat/checkout", json={"session_id": session.id})

    stored = await get_session_store().get(session.id)
    assert stored.active_preview is not None
    assert stored.active_preview.cart_hash == stored.cart.hash()


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_empty_basket_is_a_reason_not_a_silence(client, merchant_id):
    """The regression. This case used to withdraw the tool and show the shopper nothing."""
    session = Session(merchant_id=merchant_id)
    await get_session_store().save(session)

    response = client.post("/chat/checkout", json={"session_id": session.id})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "empty_cart"
    assert response.json()["detail"]["message"]  # something a shopper can read


def test_unknown_session_is_a_reason_too(client):
    response = client.post("/chat/checkout", json={"session_id": "sess_never_existed"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_session"


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_blocking_field_is_reported_verbatim(client, merchant_id, monkeypatch):
    """A merchant's required_before_purchase rule must reach the shopper as words.

    Withdrawing the tool told them nothing; this returns the policy's own sentence.
    """
    session = await _session_with_cart(merchant_id)

    import app.api.chat as chat_module

    monkeypatch.setattr(
        chat_module,
        "can_checkout",
        lambda s, p: (False, "The merchant requires these to be settled: Lens type"),
    )

    response = client.post("/chat/checkout", json={"session_id": session.id})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "checkout_blocked"
    assert "Lens type" in detail["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_checkout_charges_nothing(client, merchant_id):
    """The trust gate is untouched: this endpoint mints no token and moves no money."""
    session = await _session_with_cart(merchant_id)
    client.post("/chat/checkout", json={"session_id": session.id})

    stored = await get_session_store().get(session.id)
    assert stored.confirmation_token is None
    assert not stored.has_valid_confirmation()
    # And the charging tool is still absent from what the model may see.
    from app.agent.policy import tool_names

    assert "confirm_and_pay" not in tool_names(stored)


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_both_paths_produce_the_same_preview_shape(client, merchant_id):
    """The model may still call preview_transaction. Both routes converge on one event."""
    ctx = await _context(merchant_id)
    await build_cart({"action": "add", "id": _in_stock_id(ctx)}, ctx)
    tool_result = await preview_transaction({}, ctx)
    from_tool = next(e for e in tool_result.events if isinstance(e, PreviewEvent))

    session = await _session_with_cart(merchant_id)
    response = client.post("/chat/checkout", json={"session_id": session.id})
    kinds = dict(_events(response))

    import json

    from_http = json.loads(kinds["preview"])
    assert set(from_http) == set(from_tool.model_dump(exclude={"type"}))


# --- stock writeback ----------------------------------------------------------


async def _authorized(merchant_id: str, quantity: int = 1) -> ToolContext:
    """A context standing exactly where POST /chat/confirm leaves one."""
    ctx = await _context(merchant_id)
    product_id = _in_stock_id(ctx, minimum=quantity + 1)
    await build_cart({"action": "add", "id": product_id, "quantity": quantity}, ctx)

    result = await preview_transaction({}, ctx)
    assert not result.error, result.llm_content

    # A REAL authorization from the payment service, exactly as POST /chat/confirm
    # obtains one. A made-up id is rejected at capture, and the charge never happens —
    # which would make every stock assertion below pass for the wrong reason.
    from app.clients.payment import get_payment_client

    preview = ctx.session.active_preview
    authorization = await get_payment_client().authorize(preview_id=preview.preview_id)
    ctx.session.confirmation_token = ConfirmationToken(
        preview_id=preview.preview_id,
        authorization_id=str(authorization["authorization_id"]),
        cart_hash=preview.cart_hash,
        expires_at=preview.expires_at,
    )
    return ctx


async def _stock_of(merchant_id: str, product_id: str, column: str) -> float:
    rows = await get_merchant_client().fetch_by_ids(merchant_id, [product_id])
    return float(rows[product_id][column])


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_sale_comes_out_of_stock(merchant_id):
    ctx = await _authorized(merchant_id)
    column = ctx.profile.roles.stock
    product_id = ctx.session.cart.items[0].id
    before = await _stock_of(merchant_id, product_id, column)

    result = await confirm_and_pay({}, ctx)
    assert not result.error, result.llm_content

    assert await _stock_of(merchant_id, product_id, column) == before - 1


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_quantity_comes_out_of_stock(merchant_id):
    ctx = await _authorized(merchant_id, quantity=2)
    column = ctx.profile.roles.stock
    product_id = ctx.session.cart.items[0].id
    before = await _stock_of(merchant_id, product_id, column)

    await confirm_and_pay({}, ctx)

    assert await _stock_of(merchant_id, product_id, column) == before - 2


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_failed_stock_write_never_loses_the_receipt(merchant_id, monkeypatch):
    """Money has already moved. A bookkeeping failure must not swallow the confirmation."""
    ctx = await _authorized(merchant_id)

    from app.clients.merchant import MerchantClient, MerchantUnavailable

    async def boom(self, *args, **kwargs):
        raise MerchantUnavailable("merchant is down")

    monkeypatch.setattr(MerchantClient, "adjust_stock", boom)

    result = await confirm_and_pay({}, ctx)
    assert not result.error, result.llm_content
    assert any(e.type == "receipt" for e in result.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_stock_never_goes_negative(merchant_id):
    ctx = await _authorized(merchant_id)
    column = ctx.profile.roles.stock
    product_id = ctx.session.cart.items[0].id

    # Sell more than exists by editing the cart line behind the reverification's back.
    ctx.session.cart.items[0].quantity = 9999
    ctx.session.active_preview.items[0].quantity = 9999
    ctx.session.confirmation_token.cart_hash = ctx.session.cart.hash()
    ctx.session.active_preview.cart_hash = ctx.session.cart.hash()

    await confirm_and_pay({}, ctx)

    assert await _stock_of(merchant_id, product_id, column) == 0.0


@pytest.mark.asyncio
async def test_an_unreadable_stock_cell_is_left_alone(monkeypatch):
    """"In stock" and "sold out" are legitimate values in a merchant's own spreadsheet.

    A cell we cannot parse is one we must not overwrite with a number of our own.
    """
    merchant_id = MERCHANTS[0]
    ctx = await _authorized(merchant_id)
    column = ctx.profile.roles.stock

    from app.clients.merchant import MerchantClient

    written: dict = {}

    async def fake_fetch(self, mid, ids):
        return {i: {column: "plenty"} for i in ids}

    async def capture(self, mid, updates):
        written.update(updates)
        return len(updates)

    monkeypatch.setattr(MerchantClient, "fetch_by_ids", fake_fetch)
    monkeypatch.setattr(MerchantClient, "adjust_stock", capture)

    await confirm_and_pay({}, ctx)
    assert written == {}


@pytest.mark.asyncio
async def test_a_catalog_without_stock_writes_nothing(monkeypatch):
    """Not every catalog tracks stock. There is nothing to decrement, and no error."""
    merchant_id = MERCHANTS[0]
    ctx = await _authorized(merchant_id)
    ctx.profile.roles.stock = None

    from app.clients.merchant import MerchantClient

    called = False

    async def capture(self, mid, updates):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(MerchantClient, "adjust_stock", capture)

    result = await confirm_and_pay({}, ctx)
    assert not result.error
    assert called is False


# --- the profile must describe the sheet that is actually stored ---------------


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_profile_naming_absent_columns_is_re_derived(merchant_id):
    """Reindexing must not keep a role mapping that fits a different spreadsheet.

    A profile maps roles onto columns by name. Replace the sheet with a differently
    shaped one and those names can all vanish — and the rebuild used to reuse the stored
    profile anyway, producing an index whose every filter names a column that is not
    there. Search returned nothing for a shop full of stock, and the only symptom was an
    agent saying it had none.
    """
    registry = get_registry()
    index = await registry.get(merchant_id)
    original = index.profile

    # A profile from some other catalog entirely.
    stale = original.model_copy(deep=True)
    stale.roles.id = "column_that_does_not_exist"
    stale.roles.title = "another_absent_column"
    stale.roles.price = "a_third_one"

    assert registry._describes(original, index.rows)
    assert not registry._describes(stale, index.rows)

    registry.evict(merchant_id)
    rebuilt = await registry._build(merchant_id, stale)

    assert rebuilt is not None
    # Re-derived from the sheet, so its roles name columns that actually exist.
    columns = {c for row in rebuilt.rows for c in row}
    assert rebuilt.profile.roles.id in columns
    assert rebuilt.profile.roles.title in columns
    assert any(
        "re-derived automatically" in note for note in rebuilt.profile.notes
    ), "a profile no human approved for this sheet must say so"


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_matching_profile_is_left_alone(merchant_id):
    """The guard must not fire on the ordinary case: a merchant's approved profile,
    a re-upload of the same shape, and a version that stays put."""
    registry = get_registry()
    index = await registry.get(merchant_id)
    before = index.profile.version
    # Counted, not searched for: an earlier test in this module may already have left a
    # re-derivation note on this merchant's profile. What matters is that THIS build
    # adds none.
    notes_before = sum(
        "re-derived automatically" in n for n in index.profile.notes
    )

    registry.evict(merchant_id)
    rebuilt = await registry._build(merchant_id, index.profile)

    assert rebuilt.profile.version == before
    assert sum("re-derived automatically" in n for n in rebuilt.profile.notes) == notes_before


@pytest.mark.asyncio
@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_thinner_catalog_is_not_a_different_one(merchant_id):
    """Losing `stock` or `image` is a thinner catalog, not another spreadsheet.

    Only id, title and price decide — re-deriving on every optional column that comes
    and goes would throw away a merchant's approved layman labels for no reason.
    """
    registry = get_registry()
    index = await registry.get(merchant_id)

    thinner = index.profile.model_copy(deep=True)
    thinner.roles.stock = "a_stock_column_this_sheet_lost"
    thinner.roles.image = "an_image_column_this_sheet_lost"

    assert registry._describes(thinner, index.rows)
