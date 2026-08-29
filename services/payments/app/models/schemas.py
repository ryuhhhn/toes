from datetime import datetime, timedelta, timezone
from enum import Enum

from pydantic import BaseModel, Field, computed_field

from app.config import get_settings


class CartItem(BaseModel):
    product_id: str
    title: str
    quantity: int = Field(gt=0)
    unit_price: float = Field(ge=0)


class Cart(BaseModel):
    merchant_id: str
    session_id: str
    # WHY a field rather than a hardcoded literal: the caller was already sending
    # `currency` and pydantic was silently dropping it, so a EUR cart came back
    # priced in USD with no error anywhere. A wrong-currency bug that fails
    # quietly is worse than one that 422s.
    currency: str = "USD"
    items: list[CartItem] = Field(min_length=1)


class TransactionPreview(BaseModel):
    preview_id: str
    merchant_id: str
    session_id: str
    subtotal: float
    total: float
    currency: str
    items: list[CartItem]
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expires_at(self) -> datetime:
        """When consent for this preview lapses.

        WHY derived rather than stored: there used to be two clocks — the agent
        minted its own expiry from PREVIEW_TTL_SECONDS (300s) while the ledger
        enforced PAYMENT_TTL_MINUTES (15m), so the shopper was shown a deadline
        the server did not honour. Payments is authoritative, and deriving this
        from the same `created_at` and the same TTL that `is_expired` reads means
        the two can never drift. It needs no column: it is a view of created_at.
        """
        created = self.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created + timedelta(minutes=get_settings().payment_ttl_minutes)


class AuthMethod(str, Enum):
    MOCK_OTP = "mock_otp"
    MOCK_BIOMETRIC = "mock_biometric"
    EXPLICIT_CONFIRM = "explicit_confirm"


class UserAuthorizationRequest(BaseModel):
    preview_id: str
    method: AuthMethod
    proof: str | bool


class UserAuthorizationResult(BaseModel):
    authorized: bool
    authorization_id: str
    method: AuthMethod
    preview_id: str


class StoredAuthorization(BaseModel):
    authorization_id: str
    preview_id: str
    method: AuthMethod
    authorized: bool
    created_at: datetime


class ConfirmRequest(BaseModel):
    preview_id: str
    authorization_id: str


class TransactionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class Transaction(BaseModel):
    transaction_id: str
    preview_id: str
    amount: float
    currency: str
    status: TransactionStatus
    failure_reason: str | None = None
    created_at: datetime


class LedgerEvent(BaseModel):
    event_type: str
    reason: str | None = None
    created_at: datetime


class ReceiptView(BaseModel):
    transaction: Transaction
    authorization: StoredAuthorization
    events: list[LedgerEvent]
