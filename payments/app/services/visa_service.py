import os
import uuid
from datetime import datetime, timezone

from app.models.schemas import Transaction, TransactionPreview, TransactionStatus


def _declined_by_rule(total: float) -> bool:
    # WHY: deterministic decline rule — totals ending in .99 are always
    # declined. This makes the failure path demo-able: a judge can watch a
    # charge fail and see that nothing is persisted and no receipt exists.
    return round(total * 100) % 100 == 99


async def charge_card(preview: TransactionPreview) -> Transaction:
    mode = os.environ.get("VISA_MODE", "mock")
    if mode != "mock":
        # WHY: real Visa sandbox integration is a stretch goal, gated on time
        # and credential access. Fail loudly rather than silently pretend.
        raise NotImplementedError("real Visa sandbox integration not implemented")

    base = dict(
        transaction_id=str(uuid.uuid4()),
        preview_id=preview.preview_id,
        amount=preview.total,
        currency=preview.currency,
        created_at=datetime.now(timezone.utc),
    )
    if _declined_by_rule(preview.total):
        return Transaction(
            **base,
            status=TransactionStatus.FAILED,
            failure_reason="Card declined by issuer (mock rule: total ends in .99)",
        )
    return Transaction(**base, status=TransactionStatus.SUCCESS)
