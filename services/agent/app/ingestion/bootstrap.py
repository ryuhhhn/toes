"""A complete Agent Profile derived with zero LLM calls.

Three jobs, which is why it is worth building properly rather than as a stub:

1. It unblocks retrieval and the agent loop before the LLM pipeline exists.
2. It is the permanent runtime fallback when a merchant has no approved profile.
3. It is the degradation path when the LLM is unavailable at ingest time.

What it cannot produce is layman copy, tiers or a real category name — those are exactly
the judgements that need a model and then a merchant. Everything it *does* produce is
traceable to something the profiler measured.
"""

from __future__ import annotations

from app.ingestion.canonicalize import canonicalize_profile
from app.models.ingestion import ColumnKind, ColumnProfile, LoadNote
from app.models.profile import AgentProfile, FieldSpec, Roles, SourceInfo

#: Placeholder until the classifier names the category. Deliberately generic: no code
#: path may depend on a category name, so the fallback must be meaningless.
DEFAULT_CATEGORY = "products"

MAX_CANONICAL_VALUES = 25
NUMERIC_BIN_COUNT = 4

#: Kinds that describe the product rather than identify or render it.
ATTRIBUTE_KINDS = {
    ColumnKind.NUMERIC,
    ColumnKind.BOOLEAN,
    ColumnKind.CATEGORICAL_ENUM,
    ColumnKind.CATEGORICAL_MULTI,
    ColumnKind.CATEGORICAL_HIGH_CARD,
}


def _quantile_bins(profile: ColumnProfile, count: int = NUMERIC_BIN_COUNT) -> list[list[float]]:
    """Contiguous ranges over the observed values, for probing and comparison.

    Reconstructed from the profiler's value counts rather than the raw frame, so a profile
    can be rebuilt from stored artefacts alone. Live probing re-bins over the candidate
    set anyway; these are only the static hint.
    """
    values: list[float] = []
    for raw, occurrences in profile.value_counts.items():
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        values.extend([number] * min(occurrences, 50))

    if not values:
        if profile.numeric_min is None or profile.numeric_max is None:
            return []
        return [[profile.numeric_min, profile.numeric_max]]

    values.sort()
    distinct = sorted(set(values))
    if len(distinct) <= count:
        return [[v, v] for v in distinct]

    edges: list[float] = []
    for index in range(count + 1):
        position = int(round(index * (len(values) - 1) / count))
        edges.append(values[position])

    bins: list[list[float]] = []
    for low, high in zip(edges, edges[1:]):
        if high > low or not bins:
            bins.append([low, high])
    return bins


def field_from_profile(profile: ColumnProfile) -> FieldSpec:
    """Deterministic half of a FieldSpec. The LLM may add copy but never touch these."""
    # Clustering runs here rather than only in the LLM path, so variant spellings are
    # collapsed even when no model is available.
    canonical, aliases = canonicalize_profile(profile)
    canonical = canonical[:MAX_CANONICAL_VALUES]
    if profile.kind is ColumnKind.BOOLEAN:
        canonical, aliases = ["true", "false"], {}

    return FieldSpec(
        column=profile.name,
        kind=profile.kind,
        tier=2,  # no prior without a model; the merchant or the classifier sets the real one
        canonical_values=canonical,
        aliases=aliases,
        numeric_min=profile.numeric_min,
        numeric_max=profile.numeric_max,
        bins=_quantile_bins(profile) if profile.kind is ColumnKind.NUMERIC else [],
        unit=profile.unit,
        currency=profile.currency,
        null_rate=profile.null_rate,
        distinct_count=profile.distinct_count,
        coercion=profile.coercion,
    )


def attribute_profiles(profiles: list[ColumnProfile], roles: Roles) -> list[ColumnProfile]:
    """Columns that describe the product, excluding the ones that identify or render it."""
    structural = {roles.id, roles.title, roles.image, *roles.text} - {None}
    return [
        profile
        for profile in profiles
        if profile.name not in structural
        and profile.kind in ATTRIBUTE_KINDS
        and profile.kind is not ColumnKind.UNUSABLE
    ]


def bootstrap_profile(
    profiles: list[ColumnProfile],
    roles: Roles,
    merchant_id: str,
    *,
    source: SourceInfo | None = None,
    notes: list[LoadNote] | None = None,
    version: int = 1,
) -> AgentProfile:
    fields = [field_from_profile(p) for p in attribute_profiles(profiles, roles)]

    # Stock is plumbing. It is kept as a FieldSpec because the stock filter reads its kind,
    # but hidden so it is never probed, compared, embedded or listed to the model —
    # "how much stock would you like?" is not a question anyone asks a shopper.
    for spec in fields:
        if spec.column == roles.stock:
            spec.hidden = True

    messages = [f"{n.level}: {n.message}" for n in (notes or [])]
    missing = roles.missing_required()
    if missing:
        messages.append(
            f"error: no column identified for {', '.join(missing)}. "
            "Checkout is disabled until this is corrected."
        )
    if not roles.stock:
        messages.append(
            "warning: no stock column identified. Every item will be treated as in stock."
        )
    if not roles.image:
        messages.append("warning: no image column identified. Product cards will have no image.")

    unusable = [p.name for p in profiles if p.kind is ColumnKind.UNUSABLE]
    if unusable:
        messages.append(f"info: ignored mostly-empty column(s): {', '.join(unusable)}")

    return AgentProfile(
        merchant_id=merchant_id,
        version=version,
        status="draft",
        category=DEFAULT_CATEGORY,
        category_confidence=0.0,
        source=source or SourceInfo(),
        roles=roles,
        fields=fields,
        cross_field_rules=[],
        notes=messages,
        derived_by="bootstrap",
    )
