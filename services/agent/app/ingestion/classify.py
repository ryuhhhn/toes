"""The one LLM call in ingestion: category, tiers, layman copy, proposed rules.

The prompt is category-blind. It is told to *derive* what is being sold from the column
profiles it is given, and is never handed a category to confirm — being told the answer is
how a classifier starts agreeing with a wrong premise.

It receives profiles, never the raw table. That keeps the prompt small and, more
importantly, means the model has no rows to generalise from and no way to report a value
the profiler did not measure.

Validation is the point of this module, not the call. Everything the model returns is
checked against deterministic findings before it is allowed near a stored profile, and the
deterministic half of a FieldSpec is never writable from here.
"""

from __future__ import annotations

import logging
from typing import Any

from app.llm.base import LLMClient
from app.models.ingestion import ColumnKind, ColumnProfile
from app.models.profile import AgentProfile, CrossFieldRule, FieldSpec

log = logging.getLogger(__name__)

MAX_SAMPLE_TITLES = 10
MAX_ENUM_VALUES_IN_PROMPT = 15
MAX_COPY_CHARS = 240
VALID_TIERS = {1, 2, 3}
VALID_RULE_ACTIONS = {"warn", "block", "suggest", "info"}

SYSTEM_PROMPT = """You are analysing a merchant's product catalogue so a shopping \
assistant can sell from it without anyone writing code for this category.

You are given a statistical profile of each column, measured from the real data. You are \
NOT given the rows, and you must not invent any.

Work out for yourself what kind of products this catalogue contains, then for each column \
decide how much it should matter to a shopper who does not know the jargon.

For every column you are given, return:
- tier: 1 if a shopper genuinely cannot choose sensibly without knowing this, 2 if getting \
it wrong would be regretted, 3 if it is nice to have.
- layman_name: what a normal person would call this. No jargon.
- why_it_matters: one plain sentence on the practical consequence of this value being \
higher/lower or one thing rather than another.
- how_to_find_out: one sentence helping someone work out what they need, phrased around \
their situation rather than the product.
- probe_question: a natural question you could ask a shopper to establish this. One \
sentence, conversational, no jargon.
- suggested_required_before_purchase: true only if selling the wrong value here would be a \
genuine problem for the buyer.

Also return:
- category: a short, specific noun phrase for what is being sold.
- category_confidence: 0 to 1.
- agent_tone: one sentence describing how an assistant should sound when selling these.
- cross_field_rules: interactions between columns worth warning a shopper about, as \
{"if": "<column> <op> <value> AND <column> <op> <value>", "then": "warn", \
"message": "<one plain sentence>"}. Only propose a rule you are confident is true of this \
kind of product. Return an empty list if none. These are proposals for a human to approve; \
never phrase one as medical, safety or regulatory advice.

Use only the column names given to you, exactly as spelled. Do not invent columns. \
Do not report values that are not in the profile.

Respond with a single JSON object:
{"category": "...", "category_confidence": 0.0, "agent_tone": "...",
 "fields": [{"column": "...", "tier": 1, "layman_name": "...", "why_it_matters": "...",
             "how_to_find_out": "...", "probe_question": "...",
             "suggested_required_before_purchase": false}],
 "cross_field_rules": [{"if": "...", "then": "warn", "message": "..."}]}"""


def _column_digest(profile: ColumnProfile, canonical: list[str]) -> dict[str, Any]:
    """What the model is allowed to see: measurements, never rows."""
    digest: dict[str, Any] = {
        "column": profile.name,
        "kind": profile.kind.value,
        "filled_in": f"{(1 - profile.null_rate) * 100:.0f}% of rows",
        "distinct_values": profile.distinct_count,
    }
    if profile.kind is ColumnKind.NUMERIC:
        digest["range"] = [profile.numeric_min, profile.numeric_max]
        if profile.unit:
            digest["unit"] = profile.unit
        if profile.currency:
            digest["currency"] = profile.currency
    elif canonical:
        digest["values"] = canonical[:MAX_ENUM_VALUES_IN_PROMPT]
        if len(canonical) > MAX_ENUM_VALUES_IN_PROMPT:
            digest["values_truncated"] = True
    elif profile.samples:
        digest["examples"] = [str(s)[:80] for s in profile.samples[:3]]
    return digest


def build_prompt(
    profile: AgentProfile, column_profiles: list[ColumnProfile], sample_titles: list[str]
) -> str:
    by_name = {p.name: p for p in column_profiles}
    columns = [
        _column_digest(by_name[spec.column], spec.canonical_values)
        for spec in profile.active_fields()
        if spec.column in by_name
    ]

    roles = profile.roles
    return (
        f"Product name examples: {sample_titles[:MAX_SAMPLE_TITLES]}\n\n"
        f"Structural columns (already understood, do not describe them): "
        f"id={roles.id!r}, name={roles.title!r}, stock={roles.stock!r}, "
        f"image={roles.image!r}, description={roles.text!r}\n\n"
        f"Columns to analyse ({len(columns)}):\n{columns}"
    )


# --- validation --------------------------------------------------------------


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    return text[:MAX_COPY_CHARS] or None


def _referenced_columns(expression: str, known: set[str]) -> list[str]:
    lowered = str(expression).casefold()
    return sorted(c for c in known if c.casefold() in lowered)


def validate_classification(payload: dict, known_columns: set[str]) -> dict:
    """Strip everything untraceable before it can reach a stored profile.

    Returns a normalised payload plus a `rejected` list, which the approval screen shows
    so a merchant can see what the model tried to claim and we refused.
    """
    rejected: list[str] = []

    category = _clean_text(payload.get("category")) or ""
    try:
        confidence = float(payload.get("category_confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    fields: dict[str, dict] = {}
    for raw in payload.get("fields") or []:
        if not isinstance(raw, dict):
            continue
        column = raw.get("column")
        if column not in known_columns:
            rejected.append(f"unknown column {column!r}")
            continue
        if column in fields:
            rejected.append(f"duplicate entry for column {column!r}")
            continue

        try:
            tier = int(raw.get("tier", 2))
        except (TypeError, ValueError):
            tier = 2
        if tier not in VALID_TIERS:
            rejected.append(f"invalid tier for {column!r}")
            tier = 2

        fields[column] = {
            "tier": tier,
            "layman_name": _clean_text(raw.get("layman_name")),
            "why_it_matters": _clean_text(raw.get("why_it_matters")),
            "how_to_find_out": _clean_text(raw.get("how_to_find_out")),
            "probe_question": _clean_text(raw.get("probe_question")),
            "suggested_required_before_purchase": bool(
                raw.get("suggested_required_before_purchase", False)
            ),
        }

    rules: list[CrossFieldRule] = []
    for raw in payload.get("cross_field_rules") or []:
        if not isinstance(raw, dict):
            continue
        expression = _clean_text(raw.get("if"))
        message = _clean_text(raw.get("message"))
        if not expression or not message:
            continue
        columns = _referenced_columns(expression, known_columns)
        if not columns:
            rejected.append(f"rule references no known column: {expression!r}")
            continue
        action = str(raw.get("then", "warn")).strip().casefold()
        if action not in VALID_RULE_ACTIONS:
            action = "warn"
        rules.append(
            CrossFieldRule(
                **{"if": expression},
                then=action,
                message=message,
                columns=columns,
                # Inert until a human signs it. The model never approves its own claim.
                approved_by_merchant=False,
            )
        )

    return {
        "category": category,
        "category_confidence": confidence,
        "agent_tone": _clean_text(payload.get("agent_tone")),
        "fields": fields,
        "cross_field_rules": rules,
        "rejected": rejected,
    }


def apply_classification(profile: AgentProfile, validated: dict) -> AgentProfile:
    """Write **only** the derived-copy half of each FieldSpec.

    The deterministic half — canonical_values, ranges, unit, currency, null_rate,
    distinct_count, coercion, kind — is measured, not opined, and is not writable here.
    This function is the enforcement point for that split.
    """
    updated = profile.model_copy(deep=True)

    if validated.get("category"):
        updated.category = validated["category"]
        updated.category_confidence = validated["category_confidence"]
    if validated.get("agent_tone"):
        updated.agent_tone = validated["agent_tone"]

    for spec in updated.fields:
        derived = validated["fields"].get(spec.column)
        if not derived:
            continue
        spec.tier = derived["tier"]
        spec.layman_name = derived["layman_name"]
        spec.why_it_matters = derived["why_it_matters"]
        spec.how_to_find_out = derived["how_to_find_out"]
        spec.probe_question = derived["probe_question"]
        # A suggestion only. required_before_purchase stays merchant-controlled.
        spec.suggested_required_before_purchase = derived[
            "suggested_required_before_purchase"
        ]

    updated.cross_field_rules = validated["cross_field_rules"]
    updated.derived_by = "llm"

    for note in validated.get("rejected", []):
        updated.notes.append(f"warning: rejected model output — {note}")

    return updated


async def classify_profile(
    profile: AgentProfile,
    column_profiles: list[ColumnProfile],
    sample_titles: list[str],
    *,
    llm: LLMClient,
) -> AgentProfile:
    """Enrich a bootstrap profile with derived copy. Returns it unchanged on any failure."""
    known = {spec.column for spec in profile.fields}
    if not known:
        return profile

    try:
        payload = await llm.complete_json(
            system=SYSTEM_PROMPT,
            user=build_prompt(profile, column_profiles, sample_titles),
        )
    except Exception as exc:  # noqa: BLE001 - degrade to the deterministic profile
        log.warning("classification failed for %s: %s", profile.merchant_id, exc)
        result = profile.model_copy(deep=True)
        result.notes.append(f"warning: category analysis unavailable ({exc})")
        return result

    validated = validate_classification(payload, known)
    return apply_classification(profile, validated)


def field_summary(spec: FieldSpec) -> str:
    """One line per field for the system prompt and the approval screen."""
    parts = [f"{spec.display_name} ({spec.column})"]
    if spec.unit:
        parts.append(f"in {spec.unit}")
    if spec.canonical_values:
        parts.append(f"one of: {', '.join(spec.canonical_values[:8])}")
    elif spec.numeric_min is not None:
        parts.append(f"from {spec.numeric_min:g} to {spec.numeric_max:g}")
    return " — ".join(parts)
