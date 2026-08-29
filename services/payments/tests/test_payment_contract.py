"""The wire contract the agent depends on — docs/CONTRACTS.md §2.

Each test here corresponds to a mismatch that was live between this service and the
agent's client, and each one would have caught it on the day it was introduced.
"""

import pytest

from tests.conftest import preview_and_authorize


async def _preview(client, **overrides):
    cart = {
        "merchant_id": "m-1",
        "session_id": "s-1",
        "items": [
            {"product_id": "p-1", "title": "Widget", "quantity": 2, "unit_price": 12.5}
        ],
        **overrides,
    }
    return await client.post("/payment/preview", json=cart)


class TestCurrency:
    async def test_cart_currency_is_used_not_a_hardcoded_usd(self, client):
        # Was: Cart had no `currency` field, so pydantic dropped the caller's value
        # and the response hardcoded "USD" — a EUR cart priced in USD, silently.
        r = await _preview(client, currency="EUR")
        assert r.status_code == 200
        assert r.json()["currency"] == "EUR"

    async def test_currency_defaults_to_usd_when_absent(self, client):
        r = await _preview(client)
        assert r.json()["currency"] == "USD"


class TestExpiry:
    async def test_preview_carries_expires_at(self, client):
        # Was: no expiry on the wire at all, so the agent minted its own from a
        # 300s TTL while this service enforced 15 minutes. Two clocks, one lie.
        r = await _preview(client)
        assert "expires_at" in r.json()

    async def test_expires_at_matches_the_ttl_the_ledger_enforces(self, client):
        from datetime import datetime, timedelta

        from app.config import get_settings
        from app.services import ledger_service

        body = (await _preview(client)).json()
        created = datetime.fromisoformat(body["created_at"])
        expires = datetime.fromisoformat(body["expires_at"])

        assert expires - created == timedelta(minutes=get_settings().payment_ttl_minutes)
        # The clock the wire advertises and the clock confirm() enforces are the
        # same clock — not merely two values that happen to agree today.
        assert not ledger_service.is_expired(created)


class TestErrorEnvelope:
    """Every error is {code, message}. A bare string collapses a declined card and
    an expired preview into one indistinguishable failure the agent cannot speak to."""

    async def test_unknown_preview_returns_a_coded_envelope(self, client):
        r = await client.post(
            "/payment/confirm",
            json={"preview_id": "nope", "authorization_id": "nope"},
        )
        assert r.status_code == 404
        detail = r.json()["detail"]
        assert isinstance(detail, dict)
        assert detail["code"] == "preview_not_found"
        assert detail["message"]

    async def test_missing_receipt_returns_a_coded_envelope(self, client):
        r = await client.get("/payment/receipt/does-not-exist")
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "unknown_transaction"

    async def test_declined_charge_is_coded_card_declined(self, client):
        # The mock decline rule: a total ending in .99.
        preview, auth = await preview_and_authorize(client, unit_price=9.99, quantity=1)
        r = await client.post(
            "/payment/confirm",
            json={
                "preview_id": preview["preview_id"],
                "authorization_id": auth["authorization_id"],
            },
        )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == "card_declined"

    async def test_block_reason_and_wire_code_are_the_same_string(self, client):
        """The ledger's audit reason and the code the agent sees must not drift."""
        from tests.conftest import fetch_events

        r = await client.post(
            "/payment/confirm",
            json={"preview_id": "ghost", "authorization_id": "ghost"},
        )
        events = await fetch_events("ghost")
        assert [e["event_type"] for e in events] == ["confirm_blocked"]
        assert events[0]["reason"] == r.json()["detail"]["code"]


class TestFailureInjection:
    """`?fail=` was a silent no-op here: FastAPI ignores unknown query params, so a
    scripted 'declined card' demo beat simply succeeded against the real service."""

    @pytest.mark.parametrize(
        "code",
        ["insufficient_funds", "card_declined", "network_error", "expired_preview"],
    )
    async def test_preview_honours_fail(self, client, code):
        r = await _preview(client)
        assert r.status_code == 200  # control
        r = await client.post(
            "/payment/preview",
            params={"fail": code},
            json={
                "merchant_id": "m-1",
                "session_id": "s-1",
                "items": [
                    {
                        "product_id": "p-1",
                        "title": "Widget",
                        "quantity": 1,
                        "unit_price": 1.0,
                    }
                ],
            },
        )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == code

    async def test_authorize_honours_fail(self, client):
        preview = (await _preview(client)).json()
        r = await client.post(
            "/payment/authorize",
            params={"fail": "card_declined"},
            json={
                "preview_id": preview["preview_id"],
                "method": "explicit_confirm",
                "proof": True,
            },
        )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == "card_declined"

    async def test_confirm_injection_is_audited_like_a_real_decline(self, client):
        """A declined charge must leave the same trail as a real one. Injecting at
        the router door produced a 'decline' the ledger had never heard of."""
        from tests.conftest import fetch_events

        preview, auth = await preview_and_authorize(client)
        r = await client.post(
            "/payment/confirm",
            params={"fail": "card_declined"},
            json={
                "preview_id": preview["preview_id"],
                "authorization_id": auth["authorization_id"],
            },
        )
        assert r.status_code == 402
        events = [e["event_type"] for e in await fetch_events(preview["preview_id"])]
        assert events == [
            "preview_created",
            "auth_granted",
            "charge_attempted",
            "charge_failed",
        ]

    async def test_confirm_honours_fail_and_charges_nothing(self, client):
        preview, auth = await preview_and_authorize(client)
        r = await client.post(
            "/payment/confirm",
            params={"fail": "insufficient_funds"},
            json={
                "preview_id": preview["preview_id"],
                "authorization_id": auth["authorization_id"],
            },
        )
        assert r.status_code == 402
        assert r.json()["detail"]["code"] == "insufficient_funds"

        # An injected failure must be a real refusal, not a cosmetic one: the
        # preview stays chargeable afterwards precisely because nothing happened.
        ok = await client.post(
            "/payment/confirm",
            json={
                "preview_id": preview["preview_id"],
                "authorization_id": auth["authorization_id"],
            },
        )
        assert ok.status_code == 200

    async def test_no_fail_param_is_the_normal_path(self, client):
        assert (await _preview(client)).status_code == 200


class TestAuthorizeBody:
    async def test_explicit_confirm_is_the_agent_flow(self, client):
        """The agent sends exactly this: no session_id, no user_id. Consent is the
        method — a human pressed a button on this specific preview."""
        preview = (await _preview(client)).json()
        r = await client.post(
            "/payment/authorize",
            json={
                "preview_id": preview["preview_id"],
                "method": "explicit_confirm",
                "proof": True,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["authorized"] is True
        assert body["method"] == "explicit_confirm"
        assert body["preview_id"] == preview["preview_id"]

    async def test_item_key_is_product_id(self, client):
        """The agent used to send `id`, which 422'd here on every checkout."""
        r = await client.post(
            "/payment/preview",
            json={
                "merchant_id": "m-1",
                "session_id": "s-1",
                "items": [
                    {"id": "p-1", "title": "W", "quantity": 1, "unit_price": 1.0}
                ],
            },
        )
        assert r.status_code == 422
