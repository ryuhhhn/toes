import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

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


# WHY every error carries {code, message} rather than a bare string: the agent
# turns a failure into something it says out loud, and "declined card" and
# "expired preview" need different sentences. A plain string detail collapses
# every failure to one generic code and the agent loses the distinction.
def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


# WHY failure injection lives in the real service and not only in the stub: a
# declined card is a scripted demo beat. FastAPI ignores unknown query params,
# so `?fail=card_declined` against the real service used to succeed silently —
# the demo would show a successful charge while narrating a decline.
FAILURE_MESSAGES = {
    "insufficient_funds": "The card was declined for insufficient funds.",
    "card_declined": "The card was declined by the issuer.",
    "network_error": "The payment network could not be reached.",
    "expired_preview": "This transaction preview has expired.",
}


def _inject_failure(code: str | None) -> None:
    if not code:
        return
    raise _error(402, code, FAILURE_MESSAGES.get(code, "The payment could not be completed."))


# NOTE: preview -> authorize -> confirm is deliberately three separate endpoints.
# That separation is the Trust, Consent & Transparency design: the user sees the
# exact charge (preview), explicitly consents to it (authorize), and only then is
# money moved (confirm). Do NOT collapse these into one call.


@router.post("/preview", response_model=TransactionPreview)
async def preview_payment(
    cart: Cart, fail: str | None = Query(None)
) -> TransactionPreview:
    _inject_failure(fail)

    # WHY: the user must see the exact amount BEFORE any auth or charge happens.
    # This is the "show the receipt before asking for consent" step.
    subtotal = sum(item.unit_price * item.quantity for item in cart.items)

    # ASSUMPTION: no tax/shipping/discounts in scope, so total == subtotal.
    # Currency comes from the cart — the caller decides it, we never assume it.
    preview = TransactionPreview(
        preview_id=str(uuid.uuid4()),
        merchant_id=cart.merchant_id,
        session_id=cart.session_id,
        subtotal=subtotal,
        total=subtotal,
        currency=cart.currency,
        items=cart.items,
        created_at=datetime.now(timezone.utc),
    )

    # WHY: persist the preview (with a preview_created audit event) so
    # authorize/confirm reference a stable, auditable record of exactly what
    # the user was shown.
    await ledger_service.save_preview(preview)
    return preview


@router.post("/authorize", response_model=UserAuthorizationResult)
async def authorize_payment(
    request: UserAuthorizationRequest, fail: str | None = Query(None)
) -> UserAuthorizationResult:
    _inject_failure(fail)

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
        raise _error(401, "authorization_failed", "User authorization failed.")

    return result


@router.post("/confirm", response_model=Transaction)
async def confirm_payment(
    request: ConfirmRequest, fail: str | None = Query(None)
) -> Transaction:
    # NOTE: unlike preview and authorize, confirm does NOT short-circuit here. The
    # injected failure is handed to the charge itself (below) so the attempt and
    # its failure are both recorded, exactly as a real decline would be.

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

    transaction = await charge_card(
        preview, fail_reason=FAILURE_MESSAGES.get(fail, fail) if fail else None
    )

    if transaction.status != TransactionStatus.SUCCESS:
        # WHY: a declined charge is audited (charge_failed) and reported as 402
        # Payment Required; nothing is persisted, so no receipt can exist for it.
        await ledger_service.append_event(
            request.preview_id,
            transaction.transaction_id,
            "charge_failed",
            reason=transaction.failure_reason,
        )
        raise _error(
            402,
            fail or "card_declined",
            transaction.failure_reason or "The charge was declined.",
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
        raise _error(404, "unknown_transaction", "No receipt exists for that transaction.")
    return receipt


async def _block(preview_id: str, status_code: int, reason: str, detail: str) -> None:
    # WHY: a refused confirm is itself an auditable fact — record it, then
    # refuse. (Raises; never returns.)
    await ledger_service.append_event(preview_id, None, "confirm_blocked", reason=reason)
    # `reason` is already the machine code the ledger records — reuse it as the
    # wire code so the audit trail and the agent agree on what went wrong.
    raise _error(status_code, reason, detail)
