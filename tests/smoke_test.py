"""Cross-service smoke test — the seams, against the three REAL services.

    docker compose up -d db
    # start merchant :8001, agent :8002, payments :8003
    python -m pytest tests/smoke_test.py -q

Every per-service suite passes while the services disagree with each other, because
each one tests against its own idea of the other. That is exactly how this repo ended
up with an agent that sent `id` where payments wanted `product_id`, read `total` from a
response carrying `amount`, and showed a 5-minute expiry the ledger enforced as 15.

**Every finding in the integration review would have been caught here on the day it was
introduced.** That is the only reason this file exists, so keep it pointed at seams —
the shapes that cross a process boundary — and not at business logic, which the
per-service suites already own.

Skips cleanly when the services are not up, so it can sit in CI before CI has them.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

MERCHANT = os.environ.get("SMOKE_MERCHANT_URL", "http://localhost:8001")
AGENT = os.environ.get("SMOKE_AGENT_URL", "http://localhost:8002")
PAYMENTS = os.environ.get("SMOKE_PAYMENTS_URL", "http://localhost:8003")

FIXTURE = "services/agent/fixtures/catalogs/power_tools.csv"

#: A merchant id unique to this run, so a smoke test never mutates demo data.
MERCHANT_ID = f"smoke_{uuid.uuid4().hex[:8]}"

pytestmark = pytest.mark.asyncio


def _up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


requires_stack = pytest.mark.skipif(
    not all(_up(u) for u in (MERCHANT, AGENT, PAYMENTS)),
    reason=(
        "needs all three services up — "
        "docker compose up -d db, then merchant :8001, agent :8002, payments :8003"
    ),
)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(timeout=120.0) as c:
        yield c


# --- the merchant → agent seam ----------------------------------------------


@requires_stack
async def test_upload_stores_raw_rows_untouched(client):
    """The agent's whole premise is that column layout is arbitrary and derived.
    A normalized row cannot be un-normalized, so the merchant must serve originals."""
    with open(FIXTURE, "rb") as fh:
        original = fh.read().decode("utf-8-sig")
    with open(FIXTURE, "rb") as fh:
        r = client.post(
            f"{MERCHANT}/catalog/upload",
            files={"file": ("power_tools.csv", fh, "text/csv")},
            data={"merchant_id": MERCHANT_ID},
        )
    assert r.status_code in (200, 422), r.text

    raw = client.get(f"{MERCHANT}/catalog/raw", params={"merchant_id": MERCHANT_ID})
    assert raw.status_code == 200, raw.text
    body = raw.json()

    header = original.splitlines()[0].split(",")
    assert list(body["rows"][0].keys()) == header, "column names or order changed"
    assert body["id_column"] == "sku"
    # Every value a string or null: the agent's coerce step needs the original text
    # to find currency and units. A parsed float has already lost "$149.00".
    for value in body["rows"][0].values():
        assert value is None or isinstance(value, str), f"{value!r} was coerced"


@requires_stack
async def test_normalizer_rejection_does_not_lose_the_catalog(client):
    """This fixture has `price_usd`, not `price`, so the merchant's own normalizer
    refuses it. The raw rows must survive anyway — the console's view failing is not
    the agent's problem."""
    raw = client.get(f"{MERCHANT}/catalog/raw", params={"merchant_id": MERCHANT_ID})
    assert raw.json()["row_count"] == 26


@requires_stack
async def test_ids_filter_returns_only_those_rows(client):
    """Without server-side `ids=`, pre-charge reverification silently degrades to
    scanning the whole catalog — invariant 5 weakens without ever raising."""
    r = client.get(
        f"{MERCHANT}/catalog/raw",
        params={"merchant_id": MERCHANT_ID, "ids": "PT-1001,PT-1003"},
    )
    body = r.json()
    assert body["row_count"] == 2
    assert {row["sku"] for row in body["rows"]} == {"PT-1001", "PT-1003"}


@requires_stack
async def test_agent_derives_roles_from_arbitrary_columns(client):
    r = client.post(
        f"{AGENT}/ingest/analyze",
        json={"merchant_id": MERCHANT_ID, "use_llm": False},
    )
    assert r.status_code == 200, r.text
    roles = r.json()["profile"]["roles"]
    # Names the agent has never been told about.
    assert roles["id"] == "sku"
    assert roles["title"] == "product_name"
    assert roles["price"] == "price_usd"
    assert roles["stock"] == "qty_on_hand"


@requires_stack
async def test_index_builds_off_the_real_merchant(client):
    r = client.post(f"{AGENT}/catalog/sync/{MERCHANT_ID}")
    body = r.json()
    assert body["ok"] and body["built"], body
    assert body["rows"] == 26


# --- the agent → payments seam ----------------------------------------------


def _cart():
    return {
        "merchant_id": MERCHANT_ID,
        "session_id": "smoke",
        "currency": "USD",
        "items": [
            {
                "product_id": "PT-1001",
                "title": "Compact Drill Driver 18V",
                "quantity": 1,
                "unit_price": 149.0,
            }
        ],
    }


@requires_stack
async def test_preview_accepts_the_shape_the_agent_actually_sends(client):
    """`product_id`, not `id`. This was a hard 422 on every checkout."""
    r = client.post(f"{PAYMENTS}/payment/preview", json=_cart())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["currency"] == "USD"
    assert body["expires_at"], "no expiry on the wire means the agent invents one"


@requires_stack
async def test_currency_is_not_silently_replaced(client):
    cart = _cart() | {"currency": "EUR"}
    body = client.post(f"{PAYMENTS}/payment/preview", json=cart).json()
    assert body["currency"] == "EUR", "a EUR cart priced in USD, silently"


@requires_stack
async def test_one_expiry_clock(client):
    """Payments enforces the expiry, so payments owns the clock. Two clocks means the
    shopper is shown a deadline nobody honours."""
    from datetime import datetime

    body = client.post(f"{PAYMENTS}/payment/preview", json=_cart()).json()
    created = datetime.fromisoformat(body["created_at"])
    expires = datetime.fromisoformat(body["expires_at"])
    assert (expires - created).total_seconds() >= 600, "not the ledger's TTL"


@requires_stack
async def test_full_money_path_and_receipt(client):
    preview = client.post(f"{PAYMENTS}/payment/preview", json=_cart()).json()

    auth = client.post(
        f"{PAYMENTS}/payment/authorize",
        json={
            "preview_id": preview["preview_id"],
            "method": "explicit_confirm",
            "proof": True,
        },
    )
    assert auth.status_code == 200, auth.text

    txn = client.post(
        f"{PAYMENTS}/payment/confirm",
        json={
            "preview_id": preview["preview_id"],
            "authorization_id": auth.json()["authorization_id"],
        },
    )
    assert txn.status_code == 200, txn.text
    body = txn.json()
    # `amount` and `created_at` — the agent used to read `total` and `timestamp`,
    # which is why receipts showed 0.0 and a blank date.
    assert body["amount"] == 149.0
    assert body["created_at"]
    assert "total" not in body and "timestamp" not in body

    receipt = client.get(f"{PAYMENTS}/payment/receipt/{body['transaction_id']}").json()
    assert {"transaction", "authorization", "events"} <= receipt.keys()
    assert "charge_succeeded" in [e["event_type"] for e in receipt["events"]]


@requires_stack
async def test_errors_carry_a_code_the_agent_can_speak_about(client):
    """A plain-string detail collapses every failure to one generic code, and a
    declined card and an expired preview need different sentences."""
    r = client.post(
        f"{PAYMENTS}/payment/confirm",
        json={"preview_id": "nope", "authorization_id": "nope"},
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert isinstance(detail, dict), "bare string detail"
    assert detail["code"] == "preview_not_found"


@requires_stack
async def test_failure_injection_is_not_a_silent_no_op(client):
    """FastAPI ignores unknown query params, so `?fail=` used to pass straight
    through and a scripted 'declined card' demo simply succeeded."""
    r = client.post(
        f"{PAYMENTS}/payment/preview", params={"fail": "card_declined"}, json=_cart()
    )
    assert r.status_code == 402
    assert r.json()["detail"]["code"] == "card_declined"


# --- the agent → frontend seam ----------------------------------------------


def _sse(response):
    """Parse an SSE body into [(event, data)], the way the storefront does."""
    import json

    out, etype, data = [], None, []
    for line in response.text.split("\n"):
        if line.startswith("event:"):
            etype = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line.strip() and etype and data:
            out.append((etype, json.loads("\n".join(data))))
            etype, data = None, []
    return out


@requires_stack
async def test_chat_streams_typed_events(client):
    r = client.post(
        f"{AGENT}/chat",
        json={"message": "what do you have?", "merchant_id": MERCHANT_ID},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = _sse(r)
    types = [t for t, _ in events]
    assert "session" in types, "the frontend cannot continue a conversation without it"
    assert "done" in types, "an unterminated stream leaves the UI spinning"

    # Structure must arrive as typed events, never scraped out of prose.
    for etype, payload in events:
        if etype == "products":
            for card in payload["items"]:
                assert {"id", "title"} <= card.keys()
                for attr in card.get("attributes", []):
                    # Generic by construction: the column is data, not a field name
                    # the frontend knows.
                    assert {"column", "label", "display"} <= attr.keys()


@requires_stack
async def test_confirm_is_its_own_endpoint(client):
    """Chat text can never charge. Confirmation is a separate POST that mints the
    token, and only then does the charging tool exist in the model's schema."""
    r = client.post(
        f"{AGENT}/chat/confirm",
        json={"session_id": "does-not-exist", "preview_id": "nope"},
    )
    assert r.status_code == 404


@requires_stack
async def test_agent_health_reaches_both_services(client):
    checks = client.get(f"{AGENT}/health").json()["checks"]
    assert checks["merchant"]["ok"], "the agent cannot see the merchant"
    assert checks["payment"]["ok"], "the agent cannot see payments"


@requires_stack
async def test_merchant_reports_which_store_it_is_using(client):
    """Debugging a store you are not actually using costs hours. `memory` here means
    the catalog dies on restart."""
    assert client.get(f"{MERCHANT}/health").json()["storage"] in ("postgres", "memory")
