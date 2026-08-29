"""Payment/Auth client.

Failures here are conversational events, not exceptions: a declined card should leave the
cart intact and let the agent offer an alternative. So every method returns a structured
outcome and PaymentError carries a code the agent can speak about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.clients.http import get_http_client
from app.config import get_settings
from app.session.models import CartItem

log = logging.getLogger(__name__)


@dataclass
class PaymentError(Exception):
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class PaymentClient:
    def __init__(self, base_url: str | None = None, client: httpx.AsyncClient | None = None):
        self._base_url = (base_url or get_settings().payment_base_url).rstrip("/")
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client or get_http_client()

    async def _post(self, path: str, payload: dict, params: dict | None = None) -> dict:
        url = f"{self._base_url}{path}"
        try:
            response = await self.client.post(url, json=payload, params=params or {})
        except httpx.HTTPError as exc:
            raise PaymentError("network_error", f"The payment service is unreachable: {exc}")

        if response.status_code >= 400:
            code, message = _decode_error(response)
            raise PaymentError(code, message)

        try:
            return response.json()
        except ValueError as exc:
            raise PaymentError("bad_response", f"Payment service returned invalid JSON: {exc}")

    async def preview(
        self,
        *,
        session_id: str,
        merchant_id: str,
        items: list[CartItem],
        currency: str,
        fail: str | None = None,
    ) -> dict:
        return await self._post(
            "/payment/preview",
            {
                "session_id": session_id,
                "merchant_id": merchant_id,
                "currency": currency,
                "items": [
                    {
                        # `product_id` is what payments' CartItem is named. Sending
                        # `id` here 422'd against the real service on every checkout.
                        "product_id": i.id,
                        "title": i.title,
                        "quantity": i.quantity,
                        "unit_price": i.unit_price,
                    }
                    for i in items
                ],
            },
            {"fail": fail} if fail else None,
        )

    async def authorize(self, *, preview_id: str, fail: str | None = None) -> dict:
        """Record the shopper's consent for this preview.

        The body is exactly what payments' UserAuthorizationRequest takes. It carries
        no session_id and no user_id: `explicit_confirm` already says everything the
        ledger records — a human pressed the button on this specific preview.
        """
        return await self._post(
            "/payment/authorize",
            {"preview_id": preview_id, "method": "explicit_confirm", "proof": True},
            {"fail": fail} if fail else None,
        )

    async def confirm(
        self, *, preview_id: str, authorization_id: str, fail: str | None = None
    ) -> dict:
        """Move the money. Returns a Transaction: `amount`, `currency`, `created_at`.

        It carries no line items — the caller holds those in its own session, which
        is the authoritative record of what the shopper actually saw and agreed to.
        """
        return await self._post(
            "/payment/confirm",
            {"preview_id": preview_id, "authorization_id": authorization_id},
            {"fail": fail} if fail else None,
        )

    async def receipt(self, transaction_id: str) -> dict:
        """Fetch a ReceiptView: {transaction, authorization, events}.

        Unwrapped to the transaction, since that is what every caller wants; the
        consent record and the event timeline stay reachable under their own keys.
        """
        url = f"{self._base_url}/payment/receipt/{transaction_id}"
        try:
            response = await self.client.get(url)
        except httpx.HTTPError as exc:
            raise PaymentError("network_error", str(exc))
        if response.status_code >= 400:
            raise PaymentError(*_decode_error(response))
        body = response.json()
        transaction = body.get("transaction")
        if not isinstance(transaction, dict):
            raise PaymentError(
                "bad_response", "Receipt response carried no transaction."
            )
        return {
            **transaction,
            "authorization": body.get("authorization"),
            "events": body.get("events", []),
        }


def _decode_error(response: httpx.Response) -> tuple[str, str]:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None

    if isinstance(detail, dict):
        return str(detail.get("code", "payment_failed")), str(
            detail.get("message", "The payment could not be completed.")
        )
    if isinstance(detail, str):
        return "payment_failed", detail
    return "payment_failed", f"Payment service returned HTTP {response.status_code}."


def get_payment_client() -> PaymentClient:
    return PaymentClient()
