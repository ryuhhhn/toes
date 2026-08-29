import asyncio
import os

import asyncpg
import httpx
import pytest

from app.db.database import close_pool, get_pool, init_pool
from app.main import app

ADMIN_DSN = "postgresql://postgres:postgres@localhost:5432/postgres"
TEST_DSN = "postgresql://postgres:postgres@localhost:5432/payments_test"


async def _ensure_test_db() -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'payments_test'"
        )
        if not exists:
            await conn.execute("CREATE DATABASE payments_test")
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    asyncio.run(_ensure_test_db())
    os.environ["DATABASE_URL"] = TEST_DSN


@pytest.fixture
async def client():
    await init_pool()
    async with get_pool().acquire() as conn:
        await conn.execute(
            "TRUNCATE transactions, authorizations, previews, ledger_events"
            " RESTART IDENTITY CASCADE"
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await close_pool()


async def preview_and_authorize(client, unit_price=12.5, quantity=2):
    cart = {
        "merchant_id": "m-1",
        "session_id": "s-1",
        "items": [
            {
                "product_id": "p-1",
                "title": "Widget",
                "quantity": quantity,
                "unit_price": unit_price,
            }
        ],
    }
    r = await client.post("/payment/preview", json=cart)
    assert r.status_code == 200
    preview = r.json()
    r = await client.post(
        "/payment/authorize",
        json={
            "preview_id": preview["preview_id"],
            "method": "explicit_confirm",
            "proof": True,
        },
    )
    assert r.status_code == 200
    return preview, r.json()


async def confirm(client, preview_id, authorization_id):
    return await client.post(
        "/payment/confirm",
        json={"preview_id": preview_id, "authorization_id": authorization_id},
    )


async def fetch_events(preview_id):
    async with get_pool().acquire() as conn:
        return await conn.fetch(
            "SELECT event_type, reason FROM ledger_events"
            " WHERE preview_id = $1 ORDER BY event_id",
            preview_id,
        )
