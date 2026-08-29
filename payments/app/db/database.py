import os

import asyncpg

_pool: asyncpg.Pool | None = None

# WHY: transactions.preview_id is UNIQUE — this is the last line of defense
# against double-charging. Two racing confirms may both pass the application
# checks, but only one insert can ever succeed; the loser converges on the
# winner's transaction instead of charging twice.
SCHEMA = """
CREATE TABLE IF NOT EXISTS previews (
    preview_id  TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    subtotal    DOUBLE PRECISION NOT NULL,
    total       DOUBLE PRECISION NOT NULL,
    currency    TEXT NOT NULL,
    items       JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    preview_id       TEXT NOT NULL REFERENCES previews(preview_id),
    method           TEXT NOT NULL,
    authorized       BOOLEAN NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id   TEXT PRIMARY KEY,
    preview_id       TEXT NOT NULL UNIQUE REFERENCES previews(preview_id),
    authorization_id TEXT NOT NULL REFERENCES authorizations(authorization_id),
    amount           DOUBLE PRECISION NOT NULL,
    currency         TEXT NOT NULL,
    status           TEXT NOT NULL,
    failure_reason   TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ledger_events (
    event_id       BIGSERIAL PRIMARY KEY,
    preview_id     TEXT NOT NULL,
    transaction_id TEXT,
    event_type     TEXT NOT NULL,
    reason         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _dsn() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/payments"
    )


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(_dsn(), min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def ping_db() -> bool:
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialized — call init_pool() first")
    return _pool
