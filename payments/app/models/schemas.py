from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class CartItem(BaseModel):
    product_id: str
    title: str
    quantity: int = Field(gt=0)
    unit_price: float = Field(ge=0)


class Cart(BaseModel):
    merchant_id: str
    session_id: str
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
