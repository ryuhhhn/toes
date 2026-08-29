import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.models.schemas import (
    Cart,
    ConfirmRequest,
    ReceiptView,
    Transaction,
    TransactionPreview,
    TransactionStatus,
    UserAuthorizationRequest,
    UserAuthorizationResult,
)
from app.services import ledger_service
from app.services.user_auth_service import authorize_user
from app.services.visa_service import charge_card

router = APIRouter(prefix="/payment", tags=["payment"])


# NOTE: preview -> authorize -> confirm is deliberately three separate endpoints.
# That separation is the Trust, Consent & Transparency design: the user sees the
# exact charge (preview), explicitly consents to it (authorize), and only then is
# money moved (confirm). Do NOT collapse these into one call.


@router.post("/preview", response_model=TransactionPreview)
async def preview_payment(cart: Cart) -> TransactionPreview:
    # WHY: the user must see the exact amount BEFORE any auth or charge happens.
    # This is the "show the receipt before asking for consent" step.
    subtotal = sum(item.unit_price * item.quantity for item in cart.items)

    # ASSUMPTION: no tax/shipping/discounts in scope, so total == subtotal and
    # currency is fixed to USD.
    preview = TransactionPreview(
        preview_id=str(uuid.uuid4()),
        merchant_id=cart.merchant_id,
        session_id=cart.session_id,
        subtotal=subtotal,
        total=subtotal,
        currency="USD",
        items=cart.items,
        created_at=datetime.now(timezone.utc),
    )

    # WHY: persist the preview (with a preview_created audit event) so
    # authorize/confirm reference a stable, auditable record of exactly what
    # the user was shown.
    await ledger_service.save_preview(preview)
    return preview


@router.post("/authorize", response_model=UserAuthorizationResult)
async def authorize_payment(request: UserAuthorizationRequest) -> UserAuthorizationResult:
    # WHY: explicit user consent (OTP / biometric / confirm) is captured and
    # verified BEFORE any money moves. Consent is a first-class, recorded step.
    result = await authorize_user(request)

    # WHY: record the consent decision (with an auth_granted audit event) so
    # there is an immutable trail of what the user agreed to.
    await ledger_service.save_authorization(result)

    if not result.authorized:
        # WHY: never proceed to a charge without a positive authorization.
        # NOTE: unreachable while user_auth_service is a rubber stamp; kept for
        # the real verifier integration.
        raise HTTPException(status_code=401, detail="User authorization failed")

    return result


@router.post("/confirm", response_model=Transaction)
async def confirm_payment(request: ConfirmRequest) -> Transaction:
    # WHY: money only moves once ALL of these hold, checked in this exact order.
    # Each check maps to a distinct failure mode so the caller can react
    # precisely — and every refusal is itself recorded (confirm_blocked event),
    # because everything that reaches the money door gets logged, including
    # what is turned away.

    # (a) The preview must exist — you cannot charge for something never shown.
    preview = await ledger_service.get_preview(request.preview_id)
    if preview is None:
        await _block(request.preview_id, 404, "preview_not_found", "Preview not found")

    # (b) Consent has a shelf life — an expired preview cannot be charged.
    if ledger_service.is_expired(preview.created_at):
        await _block(request.preview_id, 410, "preview_expired", "Preview has expired")

    # (c) The authorization must exist AND be tied to THIS preview — consent
    # for one cart cannot be replayed against a different one.
    authorization = await ledger_service.get_authorization(request.authorization_id)
    if authorization is None or authorization.preview_id != request.preview_id:
        await _block(
            request.preview_id,
            401,
            "authorization_invalid",
            "Authorization missing or does not match this preview",
        )

    # (d) The authorization must actually be approved.
    if not authorization.authorized:
        await _block(
            request.preview_id,
            401,
            "authorization_not_approved",
            "Authorization was not approved",
        )

    # (e) Consent has a shelf life — an expired authorization cannot charge.
    if ledger_service.is_expired(authorization.created_at):
        await _block(
            request.preview_id,
            410,
            "authorization_expired",
            "Authorization has expired",
        )

    # (f) Idempotent convergence: if this preview was already charged, return
    # the existing transaction instead of erroring. A retry after a lost
    # response must converge on the truth, never surface a false failure —
    # and never charge twice. (Replaces the former 409 by design.)
    existing = await ledger_service.get_transaction_for_preview(request.preview_id)
    if existing is not None:
        return existing

    # WHY: record the INTENT before touching the card network. If anything dies
    # between a successful charge and its persistence, the attempt is still in
    # the immutable log — no charge can ever become invisible.
    await ledger_service.append_event(request.preview_id, None, "charge_attempted")

    transaction = await charge_card(preview)

    if transaction.status != TransactionStatus.SUCCESS:
        # WHY: a declined charge is audited (charge_failed) and reported as 402
        # Payment Required; nothing is persisted, so no receipt can exist for it.
        await ledger_service.append_event(
            request.preview_id,
            transaction.transaction_id,
            "charge_failed",
            reason=transaction.failure_reason,
        )
        raise HTTPException(
            status_code=402,
            detail=transaction.failure_reason or "Charge failed",
        )

    try:
        # WHY: dual-write — transaction row + charge_succeeded event land in one
        # database transaction, and UNIQUE(preview_id) forbids a second charge.
        await ledger_service.save_transaction(transaction, request.authorization_id)
    except ledger_service.TransactionAlreadyExists:
        # WHY: we lost a race — another confirm charged first. Converge on the
        # winner's transaction; double-charge is impossible at the DB level.
        winner = await ledger_service.get_transaction_for_preview(request.preview_id)
        if winner is not None:
            return winner
        raise

    return transaction


@router.get("/receipt/{transaction_id}", response_model=ReceiptView)
async def get_receipt(transaction_id: str) -> ReceiptView:
    # WHY: the receipt is the user-facing proof of purchase — the charge, the
    # consent that allowed it, and the full event timeline that got it there.
    # A failed charge persists nothing, so its receipt lookup 404s: no receipt
    # exists because no money moved.
    receipt = await ledger_service.get_receipt(transaction_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return receipt


async def _block(preview_id: str, status_code: int, reason: str, detail: str) -> None:
    # WHY: a refused confirm is itself an auditable fact — record it, then
    # refuse. (Raises; never returns.)
    await ledger_service.append_event(preview_id, None, "confirm_blocked", reason=reason)
    raise HTTPException(status_code=status_code, detail=detail)
