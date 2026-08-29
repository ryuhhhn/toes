"""Predicates and the relaxation ladder.

Filter first, then rank. A hard constraint cannot be ranked around: an out-of-stock item
at rank 1 is worse than no result, and a shopper who said "under 200" does not want 340
shown to them because it scored well.

The ladder exists because a dead-end "no results" kills a conversation, while a relaxed
result with an honest caveat does not. Every drop is recorded so the agent can say out
loud which constraint it had to let go of.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.ingestion.coerce import parse_boolean
from app.models.profile import AgentProfile

log = logging.getLogger(__name__)

EQ, IN, RANGE, GTE, LTE, CONTAINS, IS = "eq", "in", "range", "gte", "lte", "contains", "is"
NOTNULL = "notnull"


@dataclass
class Predicate:
    column: str
    op: str
    value: Any
    tier: int = 2
    hard: bool = False
    label: str = ""

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.op == RANGE:
            low, high = self.value
            return f"{self.column} between {low} and {high}"
        if self.op == IN:
            return f"{self.column} one of {', '.join(map(str, self.value))}"
        if self.op == GTE:
            return f"{self.column} at least {self.value}"
        if self.op == LTE:
            return f"{self.column} at most {self.value}"
        return f"{self.column} = {self.value}"

    def as_dict(self) -> dict:
        return {
            "column": self.column,
            "op": self.op,
            "value": self.value,
            "tier": self.tier,
            "hard": self.hard,
            "description": self.describe(),
        }


def _as_number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise(value: Any) -> str:
    return str(value).strip().casefold()


def _cell_values(cell: Any) -> list[str]:
    """One representation for single- and multi-valued cells."""
    if cell is None:
        return []
    if isinstance(cell, (list, tuple, set)):
        return [_normalise(v) for v in cell]
    if isinstance(cell, bool):
        return ["true" if cell else "false"]
    return [_normalise(cell)]


def matches(row: dict, predicate: Predicate) -> bool:
    cell = row.get(predicate.column)

    if predicate.op == NOTNULL:
        return cell is not None and cell != "" and cell != []

    if predicate.op == RANGE:
        number = _as_number(cell)
        low, high = predicate.value
        if number is None:
            return False
        if low is not None and number < float(low):
            return False
        return not (high is not None and number > float(high))

    if predicate.op == GTE:
        number = _as_number(cell)
        return number is not None and number >= float(predicate.value)

    if predicate.op == LTE:
        number = _as_number(cell)
        return number is not None and number <= float(predicate.value)

    if predicate.op == IS:
        if cell is None:
            return False
        return bool(cell) is bool(predicate.value)

    values = _cell_values(cell)
    if not values:
        return False

    if predicate.op == EQ:
        return _normalise(predicate.value) in values
    if predicate.op in (IN, CONTAINS):
        wanted = predicate.value if isinstance(predicate.value, (list, tuple, set)) else [
            predicate.value
        ]
        wanted_set = {_normalise(v) for v in wanted}
        if predicate.op == IN:
            return bool(wanted_set & set(values))
        return wanted_set <= set(values)

    return False


def apply_predicates(rows: list[dict], predicates: list[Predicate]) -> list[int]:
    """Indices of rows satisfying every predicate."""
    return [
        index
        for index, row in enumerate(rows)
        if all(matches(row, predicate) for predicate in predicates)
    ]


def sellable_predicate(profile: AgentProfile) -> Predicate | None:
    """A product with no usable price cannot be bought, so it must not be recommended.

    build_cart already refuses one; showing it in results anyway just walks the shopper
    into a dead end. Unparseable price cells are common in real exports ("call for
    quote"), which is exactly why this is a hard filter rather than a ranking penalty.
    """
    column = profile.roles.price
    if not column:
        return None
    return Predicate(column, NOTNULL, True, hard=True, label="has a price")


def in_stock_predicate(profile: AgentProfile) -> Predicate | None:
    """Invariant 6: never recommend an out-of-stock item, unless explicitly asked."""
    column = profile.roles.stock
    if not column:
        return None
    spec = profile.field(column)
    if spec is not None and spec.kind.value == "boolean":
        return Predicate(column, IS, True, hard=True, label="in stock")
    return Predicate(column, GTE, 1, hard=True, label="in stock")


@dataclass
class FilterOutcome:
    indices: list[int]
    applied: list[Predicate] = field(default_factory=list)
    relaxed: list[Predicate] = field(default_factory=list)

    @property
    def relaxed_descriptions(self) -> list[str]:
        return [p.describe() for p in self.relaxed]


def filter_with_relaxation(
    rows: list[dict], predicates: list[Predicate], *, min_results: int
) -> FilterOutcome:
    """Apply everything, then drop soft filters — Tier 3 first — until enough remains.

    Hard predicates (stock, an explicit price ceiling the shopper stated) are never
    dropped: relaxing those would answer a different question than the one asked.
    """
    indices = apply_predicates(rows, predicates)
    if len(indices) >= min_results or not predicates:
        return FilterOutcome(indices=indices, applied=list(predicates))

    active = list(predicates)
    relaxed: list[Predicate] = []

    # Tier 3 is the cheapest thing to give up; a Tier 1 constraint is given up last.
    droppable = sorted(
        [p for p in active if not p.hard], key=lambda p: (-p.tier, p.column), reverse=False
    )

    for predicate in droppable:
        if len(indices) >= min_results:
            break
        active.remove(predicate)
        relaxed.append(predicate)
        indices = apply_predicates(rows, active)

    return FilterOutcome(indices=indices, applied=active, relaxed=relaxed)


def predicates_from_slots(
    slots: dict[str, Any], profile: AgentProfile, *, hard_columns: set[str] | None = None
) -> list[Predicate]:
    """Turn answered questions into predicates, using the field's own kind and tier.

    A slot whose column is unknown to the profile is ignored rather than guessed at —
    that is how a hallucinated filter would otherwise reach the catalog.
    """
    hard_columns = hard_columns or set()
    predicates: list[Predicate] = []

    for column, value in slots.items():
        spec = profile.field(column)
        if spec is None or value is None:
            continue
        # "Don't care" is an answered slot with no constraint attached.
        if isinstance(value, str) and value.strip().casefold() in {"any", "don't care", "dont care"}:
            continue

        kind = spec.kind.value
        hard = column in hard_columns

        if kind == "numeric":
            if isinstance(value, (list, tuple)) and len(value) == 2:
                predicates.append(
                    Predicate(column, RANGE, list(value), tier=spec.tier, hard=hard)
                )
            elif isinstance(value, dict):
                low, high = value.get("min"), value.get("max")
                predicates.append(Predicate(column, RANGE, [low, high], tier=spec.tier, hard=hard))
            else:
                number = _as_number(value)
                if number is not None:
                    predicates.append(Predicate(column, EQ, number, tier=spec.tier, hard=hard))
        elif kind == "boolean":
            # bool("No") is True. Parse the answer rather than testing its truthiness.
            flag = parse_boolean(value)
            if flag is not None:
                predicates.append(Predicate(column, IS, flag, tier=spec.tier, hard=hard))
        elif isinstance(value, (list, tuple, set)):
            predicates.append(
                Predicate(column, IN, [str(v) for v in value], tier=spec.tier, hard=hard)
            )
        else:
            predicates.append(Predicate(column, EQ, str(value), tier=spec.tier, hard=hard))

    return predicates


def resolve_alias(spec_aliases: dict[str, str], value: str) -> str:
    """Map a raw spelling onto its canonical value, if the profile knows one."""
    for raw, canonical in spec_aliases.items():
        if _normalise(raw) == _normalise(value):
            return canonical
    return value
