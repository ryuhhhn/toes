"""Deterministic ingestion artefacts.

Everything here is produced by pure code. The LLM never writes these structures; it only
reads them. That asymmetry is what keeps derived category knowledge honest.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ColumnKind(str, Enum):
    IDENTIFIER = "identifier"
    NUMERIC = "numeric"
    BOOLEAN = "boolean"
    URL = "url"
    CATEGORICAL_ENUM = "categorical_enum"
    CATEGORICAL_MULTI = "categorical_multi"
    CATEGORICAL_HIGH_CARD = "categorical_high_card"
    FREE_TEXT = "free_text"
    UNUSABLE = "unusable"


#: Kinds a shopper can meaningfully be asked about.
PROBEABLE_KINDS = {
    ColumnKind.NUMERIC,
    ColumnKind.BOOLEAN,
    ColumnKind.CATEGORICAL_ENUM,
    ColumnKind.CATEGORICAL_MULTI,
}

#: Kinds that can be used as hard filters.
FILTERABLE_KINDS = PROBEABLE_KINDS | {ColumnKind.CATEGORICAL_HIGH_CARD}


class CoercionReport(BaseModel):
    """What a coercer did to a column, so the merchant can see and correct it."""

    applied: str | None = None  # "currency" | "unit_numeric" | "list" | "boolean" | ...
    detail: str | None = None
    unit: str | None = None
    currency: str | None = None
    decimal_convention: str | None = None  # "us" | "eu"
    list_delimiter: str | None = None
    failed_cells: int = 0
    total_cells: int = 0

    @property
    def failure_rate(self) -> float:
        return self.failed_cells / self.total_cells if self.total_cells else 0.0


class ColumnProfile(BaseModel):
    """Statistical fingerprint of one column. Pure code, no inference."""

    name: str
    raw_dtype: str
    kind: ColumnKind
    null_rate: float
    distinct_count: int
    cardinality_ratio: float
    samples: list[Any] = Field(default_factory=list)
    value_counts: dict[str, int] = Field(default_factory=dict)
    numeric_min: float | None = None
    numeric_max: float | None = None
    mean_token_len: float = 0.0
    coercion: CoercionReport = Field(default_factory=CoercionReport)

    @property
    def unit(self) -> str | None:
        return self.coercion.unit

    @property
    def currency(self) -> str | None:
        return self.coercion.currency

    @property
    def is_probeable(self) -> bool:
        return self.kind in PROBEABLE_KINDS and self.null_rate < 0.95


class LoadNote(BaseModel):
    """Something the loader decided or could not do, surfaced to the merchant."""

    level: str = "info"  # info | warning | error
    message: str


class RoleConfidence(BaseModel):
    column: str | None = None
    confidence: float = 0.0
    reason: str = ""
