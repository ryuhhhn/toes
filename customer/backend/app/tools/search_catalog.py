"""search_catalog — retrieve before you probe.

Every turn that asks the shopper a question must also put products on screen, so this is
the tool that runs first and most often.

Free-text answers are resolved onto canonical values here, deterministically: aliases from
canonicalization first, then a fuzzy match against the values the profiler actually saw.
A value that resolves to nothing is dropped rather than passed through, because an
unresolvable filter silently returns zero results and reads as a broken assistant.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from rapidfuzz import fuzz, process

from app.agent.events import ProductsEvent, product_card
from app.config import get_settings
from app.models.ingestion import ColumnKind
from app.ingestion.coerce import parse_boolean, parse_number
from app.models.profile import AgentProfile
from app.retrieval.search import search
from app.tools.registry import ToolContext, ToolResult, object_schema, tool

log = logging.getLogger(__name__)

#: Below this a free-text value is treated as unrelated to any canonical value.
RESOLVE_THRESHOLD = 78

MAX_LLM_ITEMS = 8


#: "0.8–1.3", "100 - 250", "18 to 36". probe_attributes offers numeric choices as bin
#: labels, so an answer to one arrives in exactly this shape and must become a range.
_RANGE_TEXT = re.compile(
    r"^\s*([+-]?[\d.,]+)\s*(?:[-–—]|to)\s*([+-]?[\d.,]+)\s*$", re.IGNORECASE
)


def resolve_numeric(value: Any) -> Any:
    """Turn whatever the model sent into something the numeric predicates understand."""
    if isinstance(value, (int, float)) or isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value

    text = str(value).strip()
    match = _RANGE_TEXT.match(text)
    if match:
        low, high = parse_number(match.group(1)), parse_number(match.group(2))
        if low is not None and high is not None:
            return {"min": min(low, high), "max": max(low, high)}

    number = parse_number(text)
    # Dropped rather than passed through: a numeric filter that is not a number silently
    # matches nothing, which reads as the agent ignoring what the shopper just said.
    return number


def resolve_value(spec, value: Any) -> Any:
    """Map a paraphrase onto a canonical value. Returns None when nothing matches."""
    if spec.kind is ColumnKind.NUMERIC:
        return resolve_numeric(value)
    if spec.kind is ColumnKind.BOOLEAN:
        return parse_boolean(value)
    if not spec.canonical_values:
        return value

    if isinstance(value, (list, tuple, set)):
        resolved = [resolve_value(spec, v) for v in value]
        return [v for v in resolved if v is not None] or None

    text = str(value).strip()
    if not text:
        return None

    lowered = text.casefold()
    for canonical in spec.canonical_values:
        if canonical.casefold() == lowered:
            return canonical
    for raw, canonical in spec.aliases.items():
        if str(raw).casefold() == lowered:
            return canonical

    match = process.extractOne(text, spec.canonical_values, scorer=fuzz.WRatio)
    if match and match[1] >= RESOLVE_THRESHOLD:
        return match[0]
    return None


def resolve_filters(
    filters: dict[str, Any], profile: AgentProfile
) -> tuple[dict[str, Any], list[str]]:
    """Returns (usable slots, notes about anything dropped)."""
    resolved: dict[str, Any] = {}
    notes: list[str] = []

    for column, value in (filters or {}).items():
        spec = profile.field(column)
        if spec is None:
            notes.append(f"ignored unknown filter {column!r}")
            continue
        mapped = resolve_value(spec, value)
        if mapped is None:
            notes.append(
                f"ignored {column}={value!r}: not one of the values this catalogue uses"
            )
            continue
        resolved[column] = mapped

    return resolved, notes


def record_price_bounds(args: dict, profile: AgentProfile, session) -> str | None:
    """A stated budget is an answer, so it is stored as a slot like any other.

    Keeping it only as a one-off predicate meant the next search silently dropped it —
    the same class of bug as the model forgetting to re-send a filter. Returned column
    name is marked hard so relaxation can never quietly raise someone's budget.
    """
    column = profile.roles.price
    if not column:
        return None

    maximum, minimum = args.get("max_price"), args.get("min_price")
    if maximum is None and minimum is None:
        return column if isinstance(session.known_slots.get(column), dict) else None

    existing = session.known_slots.get(column)
    bounds = dict(existing) if isinstance(existing, dict) else {}
    if maximum is not None:
        bounds["max"] = float(maximum)
    if minimum is not None:
        bounds["min"] = float(minimum)

    session.known_slots[column] = bounds
    if column not in session.asked_slots:
        session.asked_slots.append(column)
    return column


def summarise_for_model(cards, profile: AgentProfile, result) -> str:
    """What the model reads. Compact, and never the whole row dump."""
    lines = [f"{result.total_candidates} product(s) match. Showing {len(cards)}."]
    if result.filters_relaxed:
        dropped = ", ".join(f["description"] for f in result.filters_relaxed)
        lines.append(
            f"NOTE: no exact match, so these constraints were relaxed: {dropped}. "
            "Tell the shopper this in your reply."
        )
    if result.degraded:
        lines.append(f"NOTE: {result.degraded}.")

    for card in cards[:MAX_LLM_ITEMS]:
        attributes = "; ".join(f"{a.label}: {a.display}" for a in card.attributes[:4])
        price = f"{card.price:g} {card.currency}" if card.price is not None else "price unknown"
        lines.append(f"- [{card.id}] {card.title} — {price} — {attributes}")

    return "\n".join(lines)


@tool(
    name="search_catalog",
    description=(
        "Search the merchant's catalogue and show product cards to the shopper. "
        "Call this before asking any question, and again after every answer, so there "
        "are always products on screen. Pass a natural-language query describing what "
        "the shopper wants, plus any attribute filters you are confident about."
    ),
    start_summary="Searching the catalogue",
    parameters=object_schema(
        {
            "query": {
                "type": "string",
                "description": (
                    "What the shopper is looking for, in their own words. Include the "
                    "situation they described, not just product terms."
                ),
            },
            "filters": {
                "type": "object",
                "description": (
                    "Attribute filters as {column: value}, using column names and values "
                    "from the catalogue summary in your instructions. Omit anything the "
                    "shopper has not actually told you."
                ),
                "additionalProperties": True,
            },
            "max_price": {"type": "number", "description": "Budget ceiling, if stated."},
            "min_price": {"type": "number", "description": "Lower bound, if stated."},
        },
        required=["query"],
    ),
)
async def search_catalog(args: dict, ctx: ToolContext) -> ToolResult:
    settings = get_settings()
    profile, session = ctx.profile, ctx.session

    filters, notes = resolve_filters(args.get("filters") or {}, profile)

    # An answered slot stays answered: merge what we already know underneath this call's
    # filters, so the model cannot accidentally widen the search by forgetting a slot.
    slots = {**session.known_slots, **filters}
    for column, value in filters.items():
        session.known_slots[column] = value
        if column not in session.asked_slots:
            session.asked_slots.append(column)

    price_column = record_price_bounds(args, profile, session)
    if price_column:
        slots[price_column] = session.known_slots[price_column]

    result = await search(
        ctx.index,
        query=str(args.get("query") or ""),
        slots=slots,
        hard_columns={price_column} if price_column else None,
        k=int(args.get("max_results") or settings.search_top_k),
    )

    cards = [
        product_card(row, profile, score=score, prefer=list(session.known_slots))
        for row, score in zip(result.items, result.scores or [None] * len(result.items))
    ]

    # The live candidate set is what probing ranks over — not the whole catalogue.
    session.last_candidate_ids = result.candidate_ids
    session.last_shown_ids = result.ids
    session.touch()
    if cards:
        ctx.products_shown = True

    event = ProductsEvent(
        items=cards,
        filters_applied=result.filters_applied,
        filters_relaxed=result.filters_relaxed,
        total_candidates=result.total_candidates,
        note="; ".join(notes) or None,
    )

    content = summarise_for_model(cards, profile, result)
    if notes:
        content += "\n" + "\n".join(f"NOTE: {n}" for n in notes)
    if not cards:
        content += (
            "\nNothing matched even after relaxing the soft filters. Suggest what to "
            "change rather than repeating the search."
        )

    return ToolResult(
        llm_content=content,
        events=[event],
        summary=f"{result.total_candidates} match(es)",
    )
