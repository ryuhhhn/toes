import json
import os
from datetime import datetime, timedelta, timezone

import asyncpg

from app.db.database import get_pool
from app.models.schemas import (
    LedgerEvent,
    ReceiptView,
    StoredAuthorization,
    Transaction,
    TransactionPreview,
    TransactionStatus,
    UserAuthorizationResult,
)


class TransactionAlreadyExists(Exception):
    pass


def _ttl() -> timedelta:
    return timedelta(minutes=int(os.environ.get("PAYMENT_TTL_MINUTES", "15")))


def is_expired(created_at: datetime) -> bool:
    # WHY: consent has a shelf life. A preview (and the consent tied to it)
    # cannot be charged after the TTL — this kills stale-price charges, stale
    # consent, and orphaned previews from a crashed Consumer Backend at once.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created_at > _ttl()


async def append_event(
    preview_id: str,
    transaction_id: str | None,
    event_type: str,
    reason: str | None = None,
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ledger_events (preview_id, transaction_id, event_type, reason)"
            " VALUES ($1, $2, $3, $4)",
            preview_id,
            transaction_id,
            event_type,
            reason,
        )


async def save_preview(preview: TransactionPreview) -> None:
    # WHY: dual-write — the entity row and the preview_created event land in
    # one database transaction, so the audit log can never lag behind reality.
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO previews (preview_id, merchant_id, session_id, subtotal,"
                " total, currency, items, created_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                preview.preview_id,
                preview.merchant_id,
                preview.session_id,
                preview.subtotal,
                preview.total,
                preview.currency,
                json.dumps([item.model_dump() for item in preview.items]),
                preview.created_at,
            )
            await conn.execute(
                "INSERT INTO ledger_events (preview_id, event_type)"
                " VALUES ($1, 'preview_created')",
                preview.preview_id,
            )


async def get_preview(preview_id: str) -> TransactionPreview | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT preview_id, merchant_id, session_id, subtotal, total, currency,"
            " items, created_at FROM previews WHERE preview_id = $1",
            preview_id,
        )
    if row is None:
        return None
    return TransactionPreview(
        preview_id=row["preview_id"],
        merchant_id=row["merchant_id"],
        session_id=row["session_id"],
        subtotal=row["subtotal"],
        total=row["total"],
        currency=row["currency"],
        items=json.loads(row["items"]),
        created_at=row["created_at"],
    )


async def save_authorization(result: UserAuthorizationResult) -> None:
    # WHY: dual-write — consent decision and its audit event in one transaction.
    # Even decisions we'd rather not have made are recorded: the ledger is the
    # module's memory of what the user agreed to.
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO authorizations (authorization_id, preview_id, method,"
                " authorized) VALUES ($1, $2, $3, $4)",
                result.authorization_id,
                result.preview_id,
                result.method.value,
                result.authorized,
            )
            if result.authorized:
                await conn.execute(
                    "INSERT INTO ledger_events (preview_id, event_type)"
                    " VALUES ($1, 'auth_granted')",
                    result.preview_id,
                )


async def get_authorization(authorization_id: str) -> StoredAuthorization | None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT authorization_id, preview_id, method, authorized, created_at"
            " FROM authorizations WHERE authorization_id = $1",
            authorization_id,
        )
    if row is None:
        return None
    return StoredAuthorization(
        authorization_id=row["authorization_id"],
        preview_id=row["preview_id"],
        method=row["method"],
        authorized=row["authorized"],
        created_at=row["created_at"],
    )


async def get_transaction_for_preview(preview_id: str) -> Transaction | None:
    # WHY: convergence lookup — a repeat or racing confirm needs to find the
    # existing transaction and return it, not charge again.
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT transaction_id, preview_id, amount, currency, status,"
            " failure_reason, created_at FROM transactions WHERE preview_id = $1",
            preview_id,
        )
    return _row_to_transaction(row) if row is not None else None


async def save_transaction(transaction: Transaction, authorization_id: str) -> None:
    # WHY: dual-write — the transaction and charge_succeeded land together, and
    # the UNIQUE(preview_id) constraint makes double-charge impossible at the
    # database level. A loser of the race gets TransactionAlreadyExists and
    # converges on the winner's transaction.
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO transactions (transaction_id, preview_id,"
                    " authorization_id, amount, currency, status, failure_reason,"
                    " created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                    transaction.transaction_id,
                    transaction.preview_id,
                    authorization_id,
                    transaction.amount,
                    transaction.currency,
                    transaction.status.value,
                    transaction.failure_reason,
                    transaction.created_at,
                )
                await conn.execute(
                    "INSERT INTO ledger_events (preview_id, transaction_id, event_type)"
                    " VALUES ($1, $2, 'charge_succeeded')",
                    transaction.preview_id,
                    transaction.transaction_id,
                )
    except asyncpg.UniqueViolationError:
        raise TransactionAlreadyExists(transaction.preview_id) from None


async def get_receipt(transaction_id: str) -> ReceiptView | None:
    # WHY: the receipt is the user-facing proof of purchase — the transaction,
    # the consent that allowed it, and the full event timeline that got it there.
    pool = get_pool()
    async with pool.acquire() as conn:
        tx_row = await conn.fetchrow(
            "SELECT transaction_id, preview_id, amount, currency, status,"
            " failure_reason, created_at, authorization_id"
            " FROM transactions WHERE transaction_id = $1",
            transaction_id,
        )
        if tx_row is None:
            return None
        auth_row = await conn.fetchrow(
            "SELECT authorization_id, preview_id, method, authorized, created_at"
            " FROM authorizations WHERE authorization_id = $1",
            tx_row["authorization_id"],
        )
        event_rows = await conn.fetch(
            "SELECT event_type, reason, created_at FROM ledger_events"
            " WHERE preview_id = $1 ORDER BY event_id",
            tx_row["preview_id"],
        )
    return ReceiptView(
        transaction=_row_to_transaction(tx_row),
        authorization=StoredAuthorization(
            authorization_id=auth_row["authorization_id"],
            preview_id=auth_row["preview_id"],
            method=auth_row["method"],
            authorized=auth_row["authorized"],
            created_at=auth_row["created_at"],
        ),
        events=[
            LedgerEvent(
                event_type=row["event_type"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in event_rows
        ],
    )


def _row_to_transaction(row) -> Transaction:
    return Transaction(
        transaction_id=row["transaction_id"],
        preview_id=row["preview_id"],
        amount=row["amount"],
        currency=row["currency"],
        status=TransactionStatus(row["status"]),
        failure_reason=row["failure_reason"],
        created_at=row["created_at"],
    )
