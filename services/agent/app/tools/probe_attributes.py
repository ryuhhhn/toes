"""probe_attributes — which question is worth asking, over the live candidate set.

Deterministic and testable. It ranks attributes and supplies the merchant-approved copy;
it does **not** phrase the question. The model phrases it, so the conversation stays fluent
while the selection stays measurable.

    coverage(a) = fraction of the candidate set with a value for a
    H(a)        = normalised Shannon entropy of a's distribution in that set
    tier_w(a)   = {1: 1.0, 2: 0.6, 3: 0.3}[tier]
    score(a)    = tier_w(a) x H(a) x coverage(a)

Entropy is the point: an attribute everything shares tells you nothing, and an attribute
that splits the set evenly tells you the most. Multiplying by coverage stops the agent
asking about a column that is mostly empty, which would filter away real matches.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass

from app.agent.events import ProbeEvent
from app.config import get_settings
from app.models.ingestion import ColumnKind
from app.models.profile import AgentProfile, FieldSpec
from app.tools.registry import ToolContext, ToolResult, object_schema, tool

log = logging.getLogger(__name__)

MIN_COVERAGE = 0.5
MAX_CATEGORICAL_DISTINCT = 12
NUMERIC_BINS = 4
MAX_OPTIONS_SHOWN = 6


@dataclass
class ProbeCandidate:
    spec: FieldSpec
    score: float
    entropy: float
    coverage: float
    options: list[str]
    distinct: int

    def as_dict(self) -> dict:
        return {
            "column": self.spec.column,
            "label": self.spec.display_name,
            "score": round(self.score, 4),
            "entropy": round(self.entropy, 4),
            "coverage": round(self.coverage, 4),
            "tier": self.spec.tier,
            "options": self.options,
        }


def normalised_entropy(counts: list[int]) -> float:
    """0 when one value dominates completely, 1 when the split is perfectly even."""
    total = sum(counts)
    if total <= 0 or len(counts) <= 1:
        return 0.0
    entropy = -sum((c / total) * math.log2(c / total) for c in counts if c > 0)
    return entropy / math.log2(len(counts))


def _values_for(rows: list[dict], column: str) -> list[list[str]]:
    """Per-row lists of values; multi-valued cells are exploded before counting."""
    out: list[list[str]] = []
    for row in rows:
        value = row.get(column)
        if value is None or value == "" or value == []:
            out.append([])
        elif isinstance(value, (list, tuple, set)):
            out.append([str(v) for v in value if str(v).strip()])
        elif isinstance(value, bool):
            out.append(["Yes" if value else "No"])
        else:
            out.append([str(value)])
    return out


def _numeric_bins(rows: list[dict], column: str, bins: int = NUMERIC_BINS):
    """Quantile bins, so entropy reflects the shape of the data rather than its range."""
    numbers: list[float] = []
    for row in rows:
        try:
            numbers.append(float(row[column]))
        except (TypeError, ValueError, KeyError):
            continue
    if not numbers:
        return [], []

    numbers.sort()
    distinct = sorted(set(numbers))
    if len(distinct) <= bins:
        labels = [f"{v:g}" for v in distinct]
        counts = [numbers.count(v) for v in distinct]
        return labels, counts

    edges = [numbers[int(round(i * (len(numbers) - 1) / bins))] for i in range(bins + 1)]
    labels, counts = [], []
    for low, high in zip(edges, edges[1:]):
        if high <= low and labels:
            continue
        in_bin = [n for n in numbers if low <= n <= high]
        labels.append(f"{low:g}–{high:g}")
        counts.append(len(in_bin))
    return labels, counts


def score_field(spec: FieldSpec, rows: list[dict]) -> ProbeCandidate | None:
    """None when the field fails a skip rule."""
    if not rows:
        return None

    if spec.kind is ColumnKind.NUMERIC:
        labels, counts = _numeric_bins(rows, spec.column)
        present = sum(counts)
    else:
        per_row = _values_for(rows, spec.column)
        present = sum(1 for values in per_row if values)
        counter = Counter(v for values in per_row for v in values)
        labels = [label for label, _ in counter.most_common()]
        counts = [count for _, count in counter.most_common()]

    coverage = present / len(rows)
    distinct = len(labels)

    # Skip rules: no discriminating power, too many choices to ask about, or too sparse
    # to filter on without discarding real matches.
    if distinct <= 1:
        return None
    if spec.kind is not ColumnKind.NUMERIC and distinct > MAX_CATEGORICAL_DISTINCT:
        return None
    if coverage < MIN_COVERAGE:
        return None

    entropy = normalised_entropy(counts)
    return ProbeCandidate(
        spec=spec,
        score=spec.tier_weight * entropy * coverage,
        entropy=entropy,
        coverage=coverage,
        options=labels[:MAX_OPTIONS_SHOWN],
        distinct=distinct,
    )


def rank_probes(
    profile: AgentProfile, rows: list[dict], *, exclude: set[str] | None = None
) -> list[ProbeCandidate]:
    """Ranked attributes worth asking about, best first."""
    exclude = exclude or set()
    candidates = []
    for spec in profile.probeable_fields():
        if spec.column in exclude:
            continue
        candidate = score_field(spec, rows)
        if candidate is not None and candidate.score > 0:
            candidates.append(candidate)
    return sorted(candidates, key=lambda c: -c.score)


def candidate_rows(ctx: ToolContext) -> list[dict]:
    """The live candidate set, falling back to the whole catalogue before any search."""
    ids = ctx.session.last_candidate_ids
    if not ids:
        return ctx.index.rows
    rows = ctx.index.rows_by_ids(ids)
    return rows or ctx.index.rows


@tool(
    name="probe_attributes",
    description=(
        "Find out which single question would most narrow down what the shopper needs, "
        "based on the products currently matching. Returns the one attribute worth "
        "asking about with plain-language copy explaining why it matters. You must "
        "phrase the question yourself, in your own words, and only ever alongside "
        "product results — never as a bare interrogation. Takes no arguments."
    ),
    start_summary="Working out what to ask",
    parameters=object_schema({}),
)
async def probe_attributes(args: dict, ctx: ToolContext) -> ToolResult:
    settings = get_settings()
    session, profile = ctx.session, ctx.profile

    # Retrieve before you probe. Refusing here — rather than letting the loop withhold the
    # question afterwards — means the model is told to search and can still ask this turn,
    # instead of asking in prose with nothing on screen.
    if not ctx.products_shown:
        return ToolResult(
            llm_content=(
                "No products have been shown yet this turn. Call search_catalog first with "
                "whatever the shopper has told you, then call probe_attributes again. "
                "Never ask a question without products on screen."
            ),
            summary="search first",
        )

    remaining_budget = settings.max_probes_per_session - session.probe_count
    if remaining_budget <= 0:
        return ToolResult(
            llm_content=(
                "No questions left in this conversation's budget. Recommend from what you "
                "already know instead of asking anything else."
            ),
            summary="probe budget exhausted",
        )

    # One question per turn. The model does not get to choose how many: two questions in
    # one breath reads as a form, and the second is ranked over a candidate set the first
    # answer is about to change anyway.
    limit = min(settings.max_probes_per_turn, remaining_budget)

    rows = candidate_rows(ctx)
    ranked = rank_probes(profile, rows, exclude=session.answered_slots())[:limit]

    if not ranked:
        return ToolResult(
            llm_content=(
                "Nothing left worth asking about — the remaining products no longer "
                "differ in any way the shopper has not already settled. Recommend now."
            ),
            summary="no useful question",
        )

    events = []
    lines = [
        f"Ranked over the {len(rows)} product(s) currently matching. "
        "Ask this in your own words, and keep the products on screen."
    ]

    for candidate in ranked:
        spec = candidate.spec
        events.append(
            ProbeEvent(
                attribute=spec.column,
                # Merchant-approved copy when it exists; the model rephrases regardless.
                question=spec.probe_question or f"What do you need in terms of {spec.display_name}?",
                why_it_matters=spec.why_it_matters,
                how_to_find_out=spec.how_to_find_out,
                options=candidate.options,
            )
        )
        lines.append(
            f"- {spec.display_name} ({spec.column}): choices are "
            f"{', '.join(candidate.options)}. "
            f"Why it matters: {spec.why_it_matters or 'not documented by the merchant'}. "
            f"Suggested angle: {spec.how_to_find_out or 'ask about their situation'}."
        )

    session.record_asked([c.spec.column for c in ranked])

    return ToolResult(
        llm_content="\n".join(lines),
        events=events,
        summary=f"asking about {', '.join(c.spec.display_name for c in ranked)}",
    )
