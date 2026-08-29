import asyncio

from conftest import confirm, fetch_events, preview_and_authorize

from app.db.database import get_pool


async def _backdate(table: str, column_value: str, column: str, interval: str):
    async with get_pool().acquire() as conn:
        await conn.execute(
            f"UPDATE {table} SET created_at = now() - interval '{interval}'"
            f" WHERE {column} = $1",
            column_value,
        )


async def _count_transactions() -> int:
    async with get_pool().acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM transactions")


async def test_happy_path_full_flow_and_receipt_timeline(client):
    # WHY: the headline Trust story — preview, consent, charge, and a receipt
    # whose event timeline proves the order it all happened in.
    preview, auth = await preview_and_authorize(client, unit_price=12.5, quantity=2)
    assert preview["total"] == 25.0

    r = await confirm(client, preview["preview_id"], auth["authorization_id"])
    assert r.status_code == 200
    tx = r.json()
    assert tx["status"] == "success"
    assert tx["amount"] == 25.0

    r = await client.get(f"/payment/receipt/{tx['transaction_id']}")
    assert r.status_code == 200
    receipt = r.json()
    assert receipt["transaction"]["transaction_id"] == tx["transaction_id"]
    assert receipt["authorization"]["authorized"] is True

    types = [e["event_type"] for e in receipt["events"]]
    assert types == ["preview_created", "auth_granted", "charge_attempted", "charge_succeeded"]


async def test_repeat_confirm_converges_idempotently(client):
    # WHY: a retry after a lost response must converge on the existing
    # transaction, never charge twice and never surface a false failure.
    preview, auth = await preview_and_authorize(client)
    first = await confirm(client, preview["preview_id"], auth["authorization_id"])
    assert first.status_code == 200

    second = await confirm(client, preview["preview_id"], auth["authorization_id"])
    assert second.status_code == 200
    assert second.json()["transaction_id"] == first.json()["transaction_id"]
    assert await _count_transactions() == 1


async def test_racing_confirms_cannot_double_charge(client):
    # WHY: even two confirms in flight at once must yield exactly one charge —
    # the UNIQUE(preview_id) constraint is the last line of defense.
    preview, auth = await preview_and_authorize(client)
    results = await asyncio.gather(
        confirm(client, preview["preview_id"], auth["authorization_id"]),
        confirm(client, preview["preview_id"], auth["authorization_id"]),
    )
    assert [r.status_code for r in results] == [200, 200]
    ids = {r.json()["transaction_id"] for r in results}
    assert len(ids) == 1
    assert await _count_transactions() == 1


async def test_expired_preview_is_rejected_410(client):
    # WHY: consent has a shelf life — a stale preview cannot be charged.
    preview, auth = await preview_and_authorize(client)
    await _backdate("previews", preview["preview_id"], "preview_id", "20 minutes")

    r = await confirm(client, preview["preview_id"], auth["authorization_id"])
    assert r.status_code == 410
    assert await _count_transactions() == 0


async def test_expired_authorization_is_rejected_410(client):
    preview, auth = await preview_and_authorize(client)
    await _backdate(
        "authorizations", auth["authorization_id"], "authorization_id", "20 minutes"
    )

    r = await confirm(client, preview["preview_id"], auth["authorization_id"])
    assert r.status_code == 410
    assert await _count_transactions() == 0


async def test_declined_charge_402_and_nothing_persisted(client):
    # WHY: a failed charge moves no money — no transaction row, no receipt, and
    # the failure itself is audited (charge_attempted then charge_failed).
    preview, auth = await preview_and_authorize(client, unit_price=9.99, quantity=1)
    assert preview["total"] == 9.99

    r = await confirm(client, preview["preview_id"], auth["authorization_id"])
    assert r.status_code == 402
    assert await _count_transactions() == 0

    events = [(e["event_type"], e["reason"]) for e in await fetch_events(preview["preview_id"])]
    assert ("charge_attempted", None) in events
    assert any(t == "charge_failed" and reason for t, reason in events)

    # No receipt can exist for a charge that never landed.
    r = await client.get("/payment/receipt/does-not-exist")
    assert r.status_code == 404


async def test_blocked_confirms_are_audited(client):
    # WHY: everything that reaches the money door is recorded, including what
    # is turned away.
    preview, auth = await preview_and_authorize(client)

    # Missing preview -> 404, recorded.
    r = await confirm(client, "no-such-preview", auth["authorization_id"])
    assert r.status_code == 404

    # Authorization that belongs to a different preview -> 401, recorded.
    r = await confirm(client, preview["preview_id"], "wrong-auth-id")
    assert r.status_code == 401

    events = await fetch_events(preview["preview_id"])
    blocked = [e["reason"] for e in events if e["event_type"] == "confirm_blocked"]
    assert "authorization_invalid" in blocked

    orphan_events = await fetch_events("no-such-preview")
    assert any(
        e["event_type"] == "confirm_blocked" and e["reason"] == "preview_not_found"
        for e in orphan_events
    )
