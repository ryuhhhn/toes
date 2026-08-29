"""Stages F and G — loop, policy, probe ranking, comparison, cards, prompt.

The probe tests assert the *selection*, not the phrasing: the tool decides which question
is worth asking and the model words it. That split is what makes the intelligence testable.
"""

from __future__ import annotations

import pytest

from app.agent.events import ProbeEvent, ProductsEvent, TokenEvent, ToolStartEvent, product_card
from app.agent.loop import run_turn
from app.agent.policy import available_tools, tool_names
from app.agent.prompt import build_system_prompt, catalogue_summary
from app.config import get_settings
from app.models.ingestion import ColumnKind
from app.retrieval.registry import get_registry
from app.session.models import Session
from app.tools.compare_products import compare_products, select_axes
from app.tools.probe_attributes import normalised_entropy, rank_probes, score_field
from app.tools.registry import ToolContext
from app.tools.search_catalog import resolve_filters, resolve_value
from tests.fixtures_helper import CATALOGS, merchant_id_for
from tests.scripted_llm import ScriptedLLM, calls, says

MERCHANTS = [merchant_id_for(name) for name in CATALOGS]


async def _ctx(merchant_id: str) -> ToolContext:
    index = await get_registry().get(merchant_id)
    return ToolContext(
        session=Session(merchant_id=merchant_id), profile=index.profile, index=index
    )


# --- entropy -----------------------------------------------------------------


def test_entropy_is_zero_when_one_value_dominates_and_one_when_even():
    assert normalised_entropy([10]) == 0.0
    assert normalised_entropy([5, 5]) == pytest.approx(1.0)
    assert normalised_entropy([1, 1, 1, 1]) == pytest.approx(1.0)
    assert 0 < normalised_entropy([9, 1]) < 0.5


def test_a_column_with_one_value_is_never_worth_asking_about():
    from app.models.profile import FieldSpec

    spec = FieldSpec(column="c", kind=ColumnKind.CATEGORICAL_ENUM, tier=1)
    rows = [{"c": "same"} for _ in range(20)]

    assert score_field(spec, rows) is None


def test_a_mostly_empty_column_is_never_worth_asking_about():
    """Filtering on a sparse column throws away real matches."""
    from app.models.profile import FieldSpec

    spec = FieldSpec(column="c", kind=ColumnKind.CATEGORICAL_ENUM, tier=1)
    rows = [{"c": "a"}, {"c": "b"}] + [{"c": None} for _ in range(18)]

    assert score_field(spec, rows) is None


def test_too_many_choices_is_never_worth_asking_about():
    from app.models.profile import FieldSpec

    spec = FieldSpec(column="c", kind=ColumnKind.CATEGORICAL_ENUM, tier=1)
    rows = [{"c": f"value{i}"} for i in range(30)]

    assert score_field(spec, rows) is None


def test_tier_one_outranks_tier_three_at_equal_information():
    from app.models.profile import FieldSpec

    rows = [{"a": x, "b": x} for x in ["p", "q"] * 10]
    high = score_field(FieldSpec(column="a", kind=ColumnKind.CATEGORICAL_ENUM, tier=1), rows)
    low = score_field(FieldSpec(column="b", kind=ColumnKind.CATEGORICAL_ENUM, tier=3), rows)

    assert high.score > low.score


# --- probe ranking over real catalogs ----------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_probe_ranking_returns_a_sensible_non_empty_order(merchant_id):
    ctx = await _ctx(merchant_id)

    ranked = rank_probes(ctx.profile, ctx.index.rows)

    assert ranked, "nothing worth asking about in this catalogue"
    assert all(c.score > 0 for c in ranked)
    assert ranked == sorted(ranked, key=lambda c: -c.score)
    for candidate in ranked:
        assert candidate.coverage >= 0.5
        assert candidate.distinct > 1
        assert candidate.options


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_probing_never_repeats_a_question(merchant_id):
    from app.tools.probe_attributes import probe_attributes

    ctx = await _ctx(merchant_id)
    ctx.products_shown = True  # the tool refuses to ask into an empty screen

    first = await probe_attributes({"limit": 2}, ctx)
    asked = [e.attribute for e in first.events if isinstance(e, ProbeEvent)]
    assert asked

    second = await probe_attributes({"limit": 2}, ctx)
    asked_again = [e.attribute for e in second.events if isinstance(e, ProbeEvent)]

    assert not set(asked) & set(asked_again)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_probing_stops_at_the_session_budget(merchant_id):
    from app.tools.probe_attributes import probe_attributes

    ctx = await _ctx(merchant_id)
    ctx.products_shown = True
    budget = get_settings().max_probes_per_session

    ctx.session.probe_count = budget
    result = await probe_attributes({"limit": 2}, ctx)

    assert not [e for e in result.events if isinstance(e, ProbeEvent)]
    assert "budget" in result.llm_content.lower()
    assert "probe_attributes" not in tool_names(ctx.session)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_probing_ranks_over_the_live_candidate_set(merchant_id):
    """Asking about the whole catalogue when six products remain is the wrong question."""
    from app.tools.probe_attributes import candidate_rows

    ctx = await _ctx(merchant_id)
    ctx.session.last_candidate_ids = ctx.index.ids[:5]

    rows = candidate_rows(ctx)

    assert len(rows) == 5


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_declined_slots_are_not_asked_again(merchant_id):
    ctx = await _ctx(merchant_id)
    first = rank_probes(ctx.profile, ctx.index.rows)[0]

    ctx.session.declined_slots.append(first.spec.column)
    after = rank_probes(ctx.profile, ctx.index.rows, exclude=ctx.session.answered_slots())

    assert first.spec.column not in [c.spec.column for c in after]


# --- paraphrase resolution ---------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_paraphrase_resolves_onto_canonical_values(merchant_id):
    ctx = await _ctx(merchant_id)
    spec = next(
        f
        for f in ctx.profile.active_fields()
        if f.kind is ColumnKind.CATEGORICAL_ENUM and f.canonical_values
    )
    canonical = spec.canonical_values[0]

    assert resolve_value(spec, canonical.upper()) == canonical
    assert resolve_value(spec, f" {canonical.lower()} ") == canonical


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_unresolvable_filter_is_dropped_with_a_note(merchant_id):
    """An unresolvable filter silently returns zero results and reads as a broken agent."""
    ctx = await _ctx(merchant_id)
    spec = next(
        f
        for f in ctx.profile.active_fields()
        if f.kind is ColumnKind.CATEGORICAL_ENUM and f.canonical_values
    )

    resolved, notes = resolve_filters({spec.column: "zzzz-not-a-real-value-zzzz"}, ctx.profile)

    assert resolved == {}
    assert notes and spec.column in notes[0]


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_alias_spelling_resolves_to_its_canonical_value(merchant_id):
    ctx = await _ctx(merchant_id)
    spec = next(
        (f for f in ctx.profile.active_fields() if f.aliases), None
    )
    if spec is None:
        pytest.skip("no variant spellings were collapsed in this catalogue")

    raw, canonical = next(iter(spec.aliases.items()))

    assert resolve_value(spec, raw) == canonical


# --- comparison --------------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_comparison_axes_lead_with_what_the_shopper_mentioned(merchant_id):
    ctx = await _ctx(merchant_id)
    spec = next(
        f
        for f in ctx.profile.active_fields()
        if f.kind is ColumnKind.CATEGORICAL_ENUM and f.canonical_values
    )
    rows = ctx.index.rows[:4]

    axes = select_axes(ctx.profile, rows, {spec.column: spec.canonical_values[0]})

    assert axes[0].column == spec.column
    assert len(axes) <= 6


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_comparison_returns_a_structured_table_not_prose(merchant_id):
    ctx = await _ctx(merchant_id)
    ids = ctx.index.ids[:3]

    result = await compare_products({"ids": ids}, ctx)

    assert not result.error
    event = result.events[0]
    assert len(event.rows) == 3
    assert event.axes[0]["column"] == "__price__"
    for row in event.rows:
        assert set(row["values"]) == {a["column"] for a in event.axes}


async def test_comparison_needs_at_least_two_products():
    ctx = await _ctx(MERCHANTS[0])
    assert (await compare_products({"ids": [ctx.index.ids[0]]}, ctx)).error


async def test_comparison_reports_unknown_ids_rather_than_silently_dropping_them():
    ctx = await _ctx(MERCHANTS[0])
    result = await compare_products({"ids": ["nope-1", "nope-2"]}, ctx)
    assert result.error


# --- product cards -----------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_product_card_is_built_from_roles_with_generic_attributes(merchant_id):
    ctx = await _ctx(merchant_id)
    card = product_card(ctx.index.rows[0], ctx.profile)

    assert card.id and card.title
    assert card.price is not None
    assert card.image
    assert card.attributes
    for attribute in card.attributes:
        assert attribute.label and attribute.display
        assert ctx.profile.field(attribute.column) is not None


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_card_attributes_lead_with_what_the_shopper_talked_about(merchant_id):
    ctx = await _ctx(merchant_id)
    spec = next(f for f in ctx.profile.active_fields() if f.kind is ColumnKind.CATEGORICAL_ENUM)
    row = next(r for r in ctx.index.rows if r.get(spec.column) not in (None, "", []))

    card = product_card(row, ctx.profile, prefer=[spec.column])

    assert card.attributes[0].column == spec.column


# --- prompt ------------------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_prompt_is_assembled_from_the_profile(merchant_id):
    ctx = await _ctx(merchant_id)
    prompt = build_system_prompt(ctx.profile, ctx.session)

    assert ctx.profile.category in prompt
    summary = catalogue_summary(ctx.profile)
    for spec in list(ctx.profile.active_fields())[:3]:
        assert spec.column in summary


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_unapproved_cross_field_rules_never_reach_the_prompt(merchant_id):
    """Invariant 7: no domain authority the merchant did not sign off on."""
    from app.models.profile import CrossFieldRule

    ctx = await _ctx(merchant_id)
    ctx.profile.cross_field_rules = [
        CrossFieldRule(
            **{"if": "a > 1"},
            message="UNAPPROVED CLAIM THAT MUST NOT APPEAR",
            approved_by_merchant=False,
        ),
        CrossFieldRule(
            **{"if": "b > 1"}, message="APPROVED GUIDANCE", approved_by_merchant=True
        ),
    ]

    prompt = build_system_prompt(ctx.profile, ctx.session)

    assert "UNAPPROVED CLAIM" not in prompt
    assert "APPROVED GUIDANCE" in prompt


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_prompt_tells_the_model_it_cannot_confirm_for_the_shopper(merchant_id):
    ctx = await _ctx(merchant_id)
    prompt = build_system_prompt(ctx.profile, ctx.session)
    assert "confirm button" in prompt


# --- the loop ----------------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_loop_streams_tokens_and_tool_events(merchant_id, monkeypatch):
    import app.agent.loop as loop_module

    ctx = await _ctx(merchant_id)
    scripted = ScriptedLLM(
        [
            calls("search_catalog", {"query": "something for everyday use"}, text="Let me look."),
            says("Here are a few options."),
        ]
    )
    monkeypatch.setattr(loop_module, "get_llm", lambda: scripted)

    events = [
        e async for e in run_turn(ctx.session, ctx.profile, ctx.index, "I need something")
    ]

    assert any(isinstance(e, TokenEvent) for e in events)
    assert any(isinstance(e, ToolStartEvent) for e in events)
    assert any(isinstance(e, ProductsEvent) for e in events)
    assert events[-1].type == "done"
    assert ctx.session.last_candidate_ids


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_probing_without_products_is_refused_and_redirected_to_search(merchant_id,
                                                                            monkeypatch):
    """Probe-first reads as a form; retrieve-then-probe reads as a salesperson.

    The tool refuses rather than the loop withholding afterwards, so the model is told to
    search and can still ask this turn instead of asking into an empty screen.
    """
    import app.agent.loop as loop_module

    ctx = await _ctx(merchant_id)
    scripted = ScriptedLLM([calls("probe_attributes", {"limit": 1}), says("So, tell me?")])
    monkeypatch.setattr(loop_module, "get_llm", lambda: scripted)

    events = [e async for e in run_turn(ctx.session, ctx.profile, ctx.index, "hi")]

    assert not any(isinstance(e, ProbeEvent) for e in events)
    assert ctx.session.probe_count == 0, "a refused probe must not burn the budget"
    tool_result = next(
        m for m in ctx.session.messages if m.get("name") == "probe_attributes"
    )
    assert "search_catalog first" in tool_result["content"]


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_the_loop_still_withholds_a_probe_that_arrives_without_products(merchant_id):
    """Backstop for the same rule, one level down, in case a future tool emits one."""
    from app.agent.events import ProbeEvent as PE
    from app.agent.loop import _rollback_probes

    ctx = await _ctx(merchant_id)
    ctx.session.probe_count = 2
    ctx.session.asked_slots = ["a", "b"]

    _rollback_probes(ctx.session, [PE(attribute="a", question="?"), PE(attribute="b", question="?")])

    assert ctx.session.probe_count == 0
    assert ctx.session.asked_slots == []


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_probe_is_allowed_alongside_products(merchant_id, monkeypatch):
    import app.agent.loop as loop_module

    ctx = await _ctx(merchant_id)
    scripted = ScriptedLLM(
        [
            calls("search_catalog", {"query": "anything"}),
            calls("probe_attributes", {"limit": 1}),
            says("Which of those suits you?"),
        ]
    )
    monkeypatch.setattr(loop_module, "get_llm", lambda: scripted)

    events = [e async for e in run_turn(ctx.session, ctx.profile, ctx.index, "hi")]

    assert any(isinstance(e, ProductsEvent) for e in events)
    assert any(isinstance(e, ProbeEvent) for e in events)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_tool_error_becomes_a_recoverable_message(merchant_id, monkeypatch):
    """A failing tool must not kill the stream — the agent needs to talk its way out."""
    import app.agent.loop as loop_module

    ctx = await _ctx(merchant_id)
    scripted = ScriptedLLM(
        [
            calls("get_product_details", {"id": "definitely-not-a-real-id"}),
            says("I could not find that one, but here are others."),
        ]
    )
    monkeypatch.setattr(loop_module, "get_llm", lambda: scripted)

    events = [e async for e in run_turn(ctx.session, ctx.profile, ctx.index, "tell me about X")]

    assert any(e.type == "error" for e in events)
    assert events[-1].type == "done"
    assert any(isinstance(e, TokenEvent) for e in events)
    assert ctx.session.messages[-1]["role"] == "assistant"


async def test_loop_stops_at_the_tool_round_cap(monkeypatch):
    import app.agent.loop as loop_module

    ctx = await _ctx(MERCHANTS[0])
    cap = get_settings().max_tool_rounds
    scripted = ScriptedLLM([calls("search_catalog", {"query": "again"}) for _ in range(cap + 5)])
    monkeypatch.setattr(loop_module, "get_llm", lambda: scripted)

    events = [e async for e in run_turn(ctx.session, ctx.profile, ctx.index, "loop forever")]

    assert len(scripted.offered_tools) <= cap
    assert events[-1].type == "done"


async def test_model_errors_are_reported_without_killing_the_stream(monkeypatch):
    import app.agent.loop as loop_module

    ctx = await _ctx(MERCHANTS[0])

    class Exploding:
        model = "boom"

        async def stream_with_tools(self, **_kwargs):
            raise RuntimeError("rate limited")
            yield  # pragma: no cover

        async def complete_json(self, **_kwargs):
            return {}

    monkeypatch.setattr(loop_module, "get_llm", lambda: Exploding())

    events = [e async for e in run_turn(ctx.session, ctx.profile, ctx.index, "hello")]

    assert any(e.type == "error" and "rate limited" in e.message for e in events)
    assert events[-1].type == "done"


async def test_search_merges_known_slots_so_a_forgotten_filter_cannot_widen_the_search():
    from app.tools.search_catalog import search_catalog

    ctx = await _ctx(MERCHANTS[0])
    spec = next(
        f
        for f in ctx.profile.active_fields()
        if f.kind is ColumnKind.CATEGORICAL_ENUM and len(f.canonical_values) >= 2
    )
    ctx.session.known_slots[spec.column] = spec.canonical_values[0]

    result = await search_catalog({"query": "anything", "filters": {}}, ctx)
    applied = {f["column"] for f in result.events[0].filters_applied}

    assert spec.column in applied


# --- policy ------------------------------------------------------------------


async def test_preview_is_unavailable_with_an_empty_basket():
    ctx = await _ctx(MERCHANTS[0])
    assert "preview_transaction" not in tool_names(ctx.session)


async def test_discovery_tools_are_never_gated():
    ctx = await _ctx(MERCHANTS[0])
    names = tool_names(ctx.session)
    for expected in ("search_catalog", "get_product_details", "compare_products", "build_cart"):
        assert expected in names


async def test_every_offered_tool_emits_a_valid_schema_for_both_providers():
    ctx = await _ctx(MERCHANTS[0])
    for tool in available_tools(ctx.session):
        openai_schema = tool.to_openai()
        anthropic_schema = tool.to_anthropic()

        assert openai_schema["function"]["name"] == anthropic_schema["name"]
        assert openai_schema["function"]["parameters"] == anthropic_schema["input_schema"]
        assert anthropic_schema["input_schema"]["type"] == "object"
        assert tool.description


async def test_a_stated_budget_is_remembered_across_searches():
    """A budget dropped on the next search is the same bug as a forgotten filter."""
    from app.tools.search_catalog import search_catalog

    ctx = await _ctx(MERCHANTS[0])
    price_column = ctx.profile.roles.price

    await search_catalog({"query": "anything", "max_price": 200}, ctx)
    assert ctx.session.known_slots[price_column] == {"max": 200.0}

    # A later search that does not repeat the budget must still respect it.
    result = await search_catalog({"query": "something else"}, ctx)

    applied = {f["column"]: f for f in result.events[0].filters_applied}
    assert price_column in applied
    assert applied[price_column]["hard"] is True
    for card in result.events[0].items:
        assert card.price <= 200


async def test_a_budget_is_never_relaxed_away():
    from app.tools.search_catalog import search_catalog

    ctx = await _ctx(MERCHANTS[0])
    price_column = ctx.profile.roles.price

    # A budget so low that relaxation would be tempting.
    result = await search_catalog({"query": "anything at all", "max_price": 1}, ctx)

    assert not any(f["column"] == price_column for f in result.events[0].filters_relaxed)
    assert result.events[0].items == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0.8–1.3", {"min": 0.8, "max": 1.3}),   # en dash, as probe options emit
        ("100 - 250", {"min": 100.0, "max": 250.0}),
        ("18 to 36", {"min": 18.0, "max": 36.0}),
        ("1.3—2", {"min": 1.3, "max": 2.0}),     # em dash
        ("250", 250.0),
        ("not a number", None),
    ],
)
def test_numeric_answers_including_probe_bin_labels_resolve_to_ranges(text, expected):
    """probe_attributes offers numeric choices as bins, so answers arrive in that shape.

    Stored as a bare string, such an answer produces no predicate at all and the shopper
    watches the result count not move after they answered.
    """
    from app.tools.search_catalog import resolve_numeric

    assert resolve_numeric(text) == expected


async def test_answering_a_numeric_probe_actually_narrows_the_results():
    from app.tools.probe_attributes import probe_attributes
    from app.tools.search_catalog import search_catalog

    ctx = await _ctx(MERCHANTS[0])
    await search_catalog({"query": "anything"}, ctx)
    wide = ctx.session.last_candidate_ids

    probe = await probe_attributes({"limit": 4}, ctx)
    numeric = next(
        (
            e
            for e in probe.events
            if isinstance(e, ProbeEvent)
            and ctx.profile.field(e.attribute).kind is ColumnKind.NUMERIC
        ),
        None,
    )
    if numeric is None:
        pytest.skip("no numeric field was ranked worth asking about")

    result = await search_catalog(
        {"query": "anything", "filters": {numeric.attribute: numeric.options[0]}}, ctx
    )

    assert isinstance(ctx.session.known_slots[numeric.attribute], (dict, float))
    assert result.events[0].total_candidates < len(wide)


@pytest.mark.parametrize(
    "answer,expected",
    [("No", False), ("no", False), ("N", False), ("false", False), ("0", False),
     ("Yes", True), ("yes", True), ("Y", True), ("true", True), ("1", True),
     ("maybe", None)],
)
def test_a_boolean_answer_is_parsed_not_truthiness_tested(answer, expected):
    """bool("No") is True, so a shopper saying no would silently get the yes results."""
    from app.ingestion.coerce import parse_boolean

    assert parse_boolean(answer) is expected


async def test_answering_no_to_a_boolean_returns_only_no_products():
    """The end-to-end version of the same bug, through the real filter path."""
    from app.tools.search_catalog import search_catalog

    ctx = await _ctx(MERCHANTS[0])
    boolean = next(
        (f for f in ctx.profile.active_fields() if f.kind is ColumnKind.BOOLEAN), None
    )
    if boolean is None:
        pytest.skip("catalog has no boolean field")

    result = await search_catalog(
        {"query": "anything", "filters": {boolean.column: "No"}}, ctx
    )

    assert ctx.session.known_slots[boolean.column] is False
    assert result.events[0].items
    for row in ctx.index.rows_by_ids([c.id for c in result.events[0].items]):
        assert row[boolean.column] is False


async def test_an_unparseable_boolean_answer_is_dropped_rather_than_inverted():
    from app.tools.search_catalog import resolve_filters

    ctx = await _ctx(MERCHANTS[0])
    boolean = next(
        (f for f in ctx.profile.active_fields() if f.kind is ColumnKind.BOOLEAN), None
    )
    if boolean is None:
        pytest.skip("catalog has no boolean field")

    resolved, notes = resolve_filters({boolean.column: "not sure really"}, ctx.profile)

    assert resolved == {}
    assert notes
