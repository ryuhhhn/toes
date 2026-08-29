"""The Agent Profile — the only place category-specific knowledge is allowed to live.

Generated at ingest, approved by the merchant, injected into the agent at runtime.
Nothing under app/ may encode category knowledge outside of this structure.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.models.ingestion import ColumnKind, CoercionReport, RoleConfidence

PROFILE_SCHEMA_VERSION = 1

TIER_WEIGHTS: dict[int, float] = {1: 1.0, 2: 0.6, 3: 0.3}


class Roles(BaseModel):
    """Canonical roles mapped onto whatever the merchant's columns happen to be called."""

    id: str | None = None
    title: str | None = None
    price: str | None = None
    stock: str | None = None
    image: str | None = None
    text: list[str] = Field(default_factory=list)

    confidence: dict[str, RoleConfidence] = Field(default_factory=dict)

    @property
    def is_sellable(self) -> bool:
        """Checkout is impossible without an id, a name, and a price."""
        return bool(self.id and self.title and self.price)

    def missing_required(self) -> list[str]:
        return [r for r in ("id", "title", "price") if not getattr(self, r)]


class FieldSpec(BaseModel):
    """One catalog column, as the agent understands it."""

    column: str
    kind: ColumnKind
    tier: int = 2

    # Deterministic facts (from the profiler — the LLM may not overwrite these)
    canonical_values: list[str] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
    numeric_min: float | None = None
    numeric_max: float | None = None
    bins: list[list[float]] = Field(default_factory=list)
    unit: str | None = None
    currency: str | None = None
    null_rate: float = 0.0
    distinct_count: int = 0
    coercion: CoercionReport = Field(default_factory=CoercionReport)

    # Derived copy (from the LLM, merchant-editable)
    layman_name: str | None = None
    why_it_matters: str | None = None
    how_to_find_out: str | None = None
    probe_question: str | None = None

    # The classifier's opinion, kept separate from the merchant's decision below so the
    # two can never be confused for one another.
    suggested_required_before_purchase: bool = False

    # Merchant-controlled
    required_before_purchase: bool = False
    hidden: bool = False
    stale: bool = False

    @property
    def display_name(self) -> str:
        return self.layman_name or self.column.replace("_", " ")

    @property
    def tier_weight(self) -> float:
        return TIER_WEIGHTS.get(self.tier, 0.3)


class CrossFieldRule(BaseModel):
    """A field *interaction*. Proposed by the LLM, inert until a merchant approves it.

    This is the one part of the pipeline that cannot be validated against data, which is
    exactly why it requires a human signature before the agent may say it out loud.
    """

    if_: str = Field(alias="if")
    then: str = "warn"
    message: str
    approved_by_merchant: bool = False
    columns: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class SourceInfo(BaseModel):
    filename: str | None = None
    sheet: str | None = None
    row_count: int = 0
    column_count: int = 0
    row_hash: str | None = None


class AgentProfile(BaseModel):
    schema_version: int = PROFILE_SCHEMA_VERSION
    merchant_id: str
    version: int = 1
    status: str = "draft"  # draft | approved

    category: str = "products"
    category_confidence: float = 0.0
    agent_tone: str = "Warm, concise and knowledgeable. Never pushy."

    source: SourceInfo = Field(default_factory=SourceInfo)
    roles: Roles = Field(default_factory=Roles)
    fields: list[FieldSpec] = Field(default_factory=list)
    cross_field_rules: list[CrossFieldRule] = Field(default_factory=list)

    notes: list[str] = Field(default_factory=list)
    edited_fields: list[str] = Field(default_factory=list)
    derived_by: str = "bootstrap"  # bootstrap | llm
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ---- lookups -----------------------------------------------------------

    def field(self, column: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.column == column), None)

    def active_fields(self) -> list[FieldSpec]:
        return [f for f in self.fields if not f.hidden and not f.stale]

    def probeable_fields(self) -> list[FieldSpec]:
        from app.models.ingestion import PROBEABLE_KINDS

        return [f for f in self.active_fields() if f.kind in PROBEABLE_KINDS]

    def blocking_fields(self) -> list[FieldSpec]:
        return [f for f in self.active_fields() if f.required_before_purchase]

    def approved_rules(self) -> list[CrossFieldRule]:
        return [r for r in self.cross_field_rules if r.approved_by_merchant]
