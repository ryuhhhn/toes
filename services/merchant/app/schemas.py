from typing import Any, Optional

from pydantic import BaseModel, Field


class Product(BaseModel):
    id: str
    merchant_id: str
    title: str
    description: str = ""
    price: float
    category: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    image_url: Optional[str] = None
    stock: int = 1


class RowIssueOut(BaseModel):
    row_index: int
    reason: str


class UploadReport(BaseModel):
    merchant_id: str
    ok: bool
    rows_in: int
    rows_out: int
    rows_skipped: int
    missing_required_columns: list[str]
    unmapped_columns: list[str]
    aliased_columns: dict[str, str]
    warnings: list[str] = Field(default_factory=list)
    skipped_row_details: list[RowIssueOut]


class UploadResponse(BaseModel):
    report: UploadReport
    products: list[Product]


class ProductUpdate(BaseModel):
    """PATCH /catalog/:id — all fields optional, only provided ones change."""
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    category: Optional[str] = None
    attributes: Optional[dict[str, Any]] = None
    image_url: Optional[str] = None
    stock: Optional[int] = None