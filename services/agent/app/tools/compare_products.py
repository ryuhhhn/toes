"""compare_products — a structured table, never prose.

Axis selection is the small thing that reads as intelligence: what *this* shopper said they
care about comes first, then the merchant's tiers. A generic spec dump is what every other
comparison table does, and it makes the shopper do the work of finding the difference.
"""

from __future__ import annotations

from typing import Any

from app.agent.events import ComparisonEvent, _display
from app.models.profile import AgentProfile, FieldSpec
from app.tools.registry import ToolContext, ToolResult, object_schema, tool

MAX_AXES = 6
MAX_PRODUCTS = 4


def select_axes(profile: AgentProfile, rows: list[dict], known_slots: dict) -> list[FieldSpec]:
    """Slots the shopper has spoken about first, then tier order.

    Axes on which every product is identical are dropped: a column of the same value four
    times is noise in a table whose entire job is showing the difference.
    """
    def differs(spec: FieldSpec) -> bool:
        seen = {
            str(row.get(spec.column)) for row in rows if row.get(spec.column) not in (None, "")
        }
        return len(seen) > 1

    # Price has its own column in the table; listing it again duplicates it.
    fields = [f for f in profile.active_fields() if f.column != profile.roles.price]
    spoken = [f for f in fields if f.column in known_slots]
    rest = sorted(
        (f for f in fields if f.column not in known_slots),
        key=lambda f: (f.tier, f.column),
    )

    ordered = spoken + rest
    discriminating = [f for f in ordered if differs(f)]
    # If nothing differs, showing the shared attributes is still better than an empty table.
    return (discriminating or ordered)[:MAX_AXES]


@tool(
    name="compare_products",
    description=(
        "Put two to four products side by side on the attributes that matter to this "
        "shopper. Use this when they are choosing between specific items."
    ),
    start_summary="Building a comparison",
    parameters=object_schema(
        {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Two to four product ids to compare.",
            }
        },
        required=["ids"],
    ),
)
async def compare_products(args: dict, ctx: ToolContext) -> ToolResult:
    ids = [str(i) for i in (args.get("ids") or [])][:MAX_PRODUCTS]
    if len(ids) < 2:
        return ToolResult.failure("Comparing needs at least two product ids.", code="bad_request")

    rows = ctx.index.rows_by_ids(ids)
    if len(rows) < 2:
        missing = [i for i in ids if ctx.index.row_by_id(i) is None]
        return ToolResult.failure(
            f"These ids are not in the catalogue: {', '.join(missing)}", code="unknown_product"
        )

    profile = ctx.profile
    roles = profile.roles
    specs = select_axes(profile, rows, ctx.session.known_slots)

    axes: list[dict[str, Any]] = [{"column": "__price__", "label": "Price", "unit": None}]
    axes += [
        {"column": s.column, "label": s.display_name, "unit": s.unit, "tier": s.tier}
        for s in specs
    ]

    table: list[dict[str, Any]] = []
    for row in rows:
        values: dict[str, Any] = {}
        price = row.get(roles.price)
        price_spec = profile.field(roles.price) if roles.price else None
        currency = price_spec.currency if price_spec and price_spec.currency else ""
        values["__price__"] = (
            f"{float(price):g} {currency}".strip() if price is not None else "—"
        )
        for spec in specs:
            cell = row.get(spec.column)
            values[spec.column] = _display(cell, spec.unit) if cell not in (None, "", []) else "—"

        table.append(
            {
                "id": str(row.get(roles.id)),
                "title": str(row.get(roles.title)) if roles.title else "",
                "values": values,
            }
        )

    header = " | ".join(a["label"] for a in axes)
    lines = [f"Comparison on: {header}"]
    for entry in table:
        cells = " | ".join(str(entry["values"][a["column"]]) for a in axes)
        lines.append(f"[{entry['id']}] {entry['title']}: {cells}")
    lines.append(
        "Summarise the real trade-off between these in a sentence. Do not read the table out."
    )

    return ToolResult(
        llm_content="\n".join(lines),
        events=[ComparisonEvent(axes=axes, rows=table)],
        summary=f"compared {len(table)} products",
    )
