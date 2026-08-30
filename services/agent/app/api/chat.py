"""POST /chat (SSE), POST /chat/checkout and POST /chat/confirm.

Confirmation is a separate HTTP request by design (invariant 3). It is not a message, not
a tool argument, and not something the model can infer from the shopper typing "yes" — it
is a button press that arrives on its own endpoint, mints a single-use token, and only
then makes the charging tool visible to the model at all.

Checkout is a separate request for a different reason. A preview charges nothing, so it
was never a trust question — it was a reliability one. The button used to send the chat
message "I'm ready to check out" and hope the model called `preview_transaction`, which
made a button press a request for a favour, and left the panel shut with no explanation
whenever the model answered in prose or the tool had been withdrawn by policy. A preview
is a deterministic function of the cart, so /chat/checkout computes one directly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.events import DoneEvent, ErrorEvent, Event, ToolStartEvent
from app.agent.loop import run_turn
from app.agent.policy import can_checkout
from app.audit import record
from app.clients.payment import PaymentError, get_payment_client
from app.retrieval.registry import get_registry
from app.session.models import ConfirmationToken, Session, new_id
from app.session.store import get_session_store
from app.tools.preview_transaction import preview_transaction, reverify_cart
from app.tools.registry import ToolContext

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

#: Proxies close an idle stream; a comment line keeps it open without confusing the client.
HEARTBEAT_SECONDS = 15.0

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # nginx would otherwise buffer the whole stream
}


class ChatRequest(BaseModel):
    message: str
    merchant_id: str
    session_id: str | None = None


class CheckoutRequest(BaseModel):
    session_id: str


class ConfirmRequest(BaseModel):
    session_id: str
    preview_id: str
    #: Demo-only. Forces the charge to fail with this code so the declined-card path
    #: can be shown on purpose. It can only ever turn a success into a failure.
    fail: str | None = None


async def _heartbeat_stream(events: AsyncIterator[Event]) -> AsyncIterator[str]:
    """Forward events, emitting a keepalive comment during quiet stretches."""
    queue: asyncio.Queue = asyncio.Queue()

    async def produce() -> None:
        try:
            async for event in events:
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report inline rather than truncating
            log.exception("stream producer failed")
            await queue.put(ErrorEvent(code="stream_error", message=str(exc)))
        finally:
            await queue.put(None)

    task = asyncio.create_task(produce())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is None:
                break
            yield event.sse()
    finally:
        # Client disconnect lands here; cancel cleanly so session state stays consistent.
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014
            pass


async def _prepare(merchant_id: str, session_id: str | None) -> tuple[Session, object]:
    index = await get_registry().get(merchant_id)
    if index is None:
        raise HTTPException(
            404,
            f"No catalogue is available for merchant {merchant_id!r}. "
            "Upload one, or check the merchant backend is running.",
        )
    session = await get_session_store().get_or_create(session_id, merchant_id)
    return session, index


@router.post("/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    session, index = await _prepare(request.merchant_id, request.session_id)

    async def stream() -> AsyncIterator[str]:
        # The client needs the session id before the first token to send follow-ups.
        yield f'event: session\ndata: {{"session_id": "{session.id}"}}\n\n'
        async for chunk in _heartbeat_stream(
            run_turn(session, index.profile, index, request.message)
        ):
            yield chunk
        await get_session_store().save(session)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/chat/checkout")
async def checkout(request: CheckoutRequest) -> StreamingResponse:
    """The checkout button. A preview, computed — not requested of a model.

    Everything that can refuse does so BEFORE the stream opens, as an HTTP error with a
    code the storefront can render. That is the whole point: the old path could not tell
    "the model chose not to" apart from "the merchant requires a size first", and showed
    the shopper the same nothing for both.

    This does not weaken the trust gate. `confirm_and_pay` is still absent from the
    model's schema until POST /chat/confirm mints a token, and this endpoint mints
    nothing and charges nothing.
    """
    session = await get_session_store().get(request.session_id)
    if session is None:
        raise HTTPException(
            404,
            {
                "code": "unknown_session",
                "message": "This session has expired. Say hello again and I will pick up "
                "where we left off.",
            },
        )

    index = await get_registry().get(session.merchant_id)
    if index is None:
        raise HTTPException(
            503,
            {
                "code": "catalog_unavailable",
                "message": "The catalogue is unavailable, so I cannot price a basket "
                "right now.",
            },
        )

    if not session.cart.items:
        raise HTTPException(
            409,
            {
                "code": "empty_cart",
                "message": "Your basket is empty — add something first and I will total "
                "it up.",
            },
        )

    # The merchant's own required_before_purchase rules, reported rather than silently
    # withdrawing the tool the way the model-driven path did.
    allowed, reason = can_checkout(session, index.profile)
    if not allowed:
        raise HTTPException(409, {"code": "checkout_blocked", "message": reason})

    async def stream() -> AsyncIterator[str]:
        yield ToolStartEvent(
            tool="preview_transaction", summary="Checking live prices and stock"
        ).sse()

        ctx = ToolContext(session=session, profile=index.profile, index=index)
        result = await preview_transaction({}, ctx)

        # A failure after this point (reverification, payments) is a typed error event,
        # exactly as it is on /chat.
        for event in result.events:
            yield event.sse()
        yield DoneEvent(turn_id=new_id("turn")).sse()
        await get_session_store().save(session)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/chat/confirm")
async def confirm(request: ConfirmRequest) -> StreamingResponse:
    """The button press. Validates, authorises, mints a token, re-enters the loop."""
    store = get_session_store()

    session = await store.get(request.session_id)
    if session is None:
        raise HTTPException(404, "Unknown session.")

    preview = session.active_preview
    if preview is None or preview.preview_id != request.preview_id:
        raise HTTPException(409, "That preview is not the current one. Ask for a fresh total.")
    if preview.expired:
        session.invalidate_preview("expired")
        raise HTTPException(410, "That preview has expired. Ask for a fresh total.")
    # Invariant 4: the cart must be exactly what was previewed.
    if preview.cart_hash != session.cart.hash():
        session.invalidate_preview("cart changed")
        raise HTTPException(409, "The basket changed after this preview. Ask for a fresh total.")
    if session.confirmation_token is not None and session.confirmation_token.used:
        raise HTTPException(409, "This confirmation has already been used.")

    # Stock and price are re-checked once more here, because the gap between seeing a
    # preview and pressing confirm is exactly where "it sold out while you were
    # deciding" happens. Abort cleanly rather than charging for something unavailable.
    index_for_check = await get_registry().get(session.merchant_id)
    if index_for_check is not None:
        problems = await reverify_cart(
            ToolContext(
                session=session, profile=index_for_check.profile, index=index_for_check
            )
        )
        if problems:
            session.invalidate_preview("changed between preview and confirm")
            record(
                "authorization_aborted",
                session=session.id,
                merchant_id=session.merchant_id,
                preview_id=preview.preview_id,
                outcome="reverification_failed",
                problems=problems,
            )
            raise HTTPException(
                409, {"code": "verification_failed", "message": " ".join(problems)}
            )

    # Deliberately NOT passed to authorize. Authorize records consent; it is not a
    # card check, so a "declined card" there is meaningless — and it would surface
    # as a bare HTTP 402 on this endpoint, which the agent never gets to speak
    # about. Injected at capture instead, where a real decline happens and where
    # the failure reaches the agent as a tool result it can offer an answer to.
    session.inject_failure = request.fail

    try:
        authorization = await get_payment_client().authorize(
            preview_id=preview.preview_id
        )
    except PaymentError as exc:
        record(
            "authorization_failed",
            session=session.id,
            merchant_id=session.merchant_id,
            preview_id=preview.preview_id,
            outcome=exc.code,
        )
        raise HTTPException(402, {"code": exc.code, "message": exc.message}) from exc

    session.confirmation_token = ConfirmationToken(
        preview_id=preview.preview_id,
        authorization_id=str(authorization.get("authorization_id")),
        cart_hash=preview.cart_hash,
        # The token dies with the preview it authorises. Giving it its own clock
        # would let a token outlive the thing it consents to — and payments would
        # reject the charge anyway, since the ledger expires both together.
        expires_at=preview.expires_at,
    )
    await store.save(session)

    record(
        "authorized",
        session=session.id,
        merchant_id=session.merchant_id,
        preview_id=preview.preview_id,
        authorization_id=session.confirmation_token.authorization_id,
        cart=[i.model_dump() for i in session.cart.items],
        total=preview.total,
        outcome="ok",
    )

    index = await get_registry().get(session.merchant_id)
    if index is None:
        raise HTTPException(503, "The catalogue is unavailable.")

    async def stream() -> AsyncIterator[str]:
        async for chunk in _heartbeat_stream(
            run_turn(
                session,
                index.profile,
                index,
                # Not a user utterance: an instruction describing an authorisation that
                # already happened out of band.
                "[The shopper pressed the confirm button on the transaction preview. "
                "Complete the payment now.]",
                turn_id=new_id("turn"),
            )
        ):
            yield chunk
        await store.save(session)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
