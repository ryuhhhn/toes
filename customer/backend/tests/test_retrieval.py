"""Stage E — index, filters, relaxation, search.

Parameterized over every fixture. The assertions are properties of a working search, not
facts about any product: in-stock only, relaxation instead of emptiness, ranking over the
filtered subset.
"""

from __future__ import annotations

import pytest

from app.ingestion.profile_store import get_profile_store
from app.models.ingestion import ColumnKind
from app.retrieval.filters import (
    EQ,
    GTE,
    IN,
    RANGE,
    Predicate,
    apply_predicates,
    filter_with_relaxation,
    in_stock_predicate,
    matches,
    predicates_from_slots,
)
from app.retrieval.registry import get_registry
from app.retrieval.search import search
from tests.fixtures_helper import CATALOGS, merchant_id_for

MERCHANTS = [merchant_id_for(name) for name in CATALOGS]


async def _index(merchant_id: str):
    index = await get_registry().get(merchant_id)
    assert index is not None, f"no index built for {merchant_id}"
    return index


def _stock_of(index, row) -> float:
    column = index.profile.roles.stock
    try:
        return float(row.get(column))
    except (TypeError, ValueError):
        return 0.0


# --- predicates --------------------------------------------------------------


def test_predicate_matching_by_operator():
    row = {"n": 18.0, "s": "Cordless", "multi": ["organic", "fair-trade"], "b": True}

    assert matches(row, Predicate("n", RANGE, [10, 20]))
    assert not matches(row, Predicate("n", RANGE, [20, 30]))
    assert matches(row, Predicate("n", GTE, 18))
    assert matches(row, Predicate("s", EQ, "cordless"))  # case-insensitive
    assert matches(row, Predicate("multi", IN, ["organic"]))
    assert not matches(row, Predicate("multi", IN, ["decaf"]))
    assert matches(row, Predicate("b", "is", True))


def test_missing_value_never_satisfies_a_filter():
    """A null must not sneak through a constraint the shopper actually stated."""
    assert not matches({"n": None}, Predicate("n", GTE, 1))
    assert not matches({}, Predicate("missing", EQ, "x"))


def test_relaxation_drops_tier_three_before_tier_one():
    rows = [{"a": "x", "b": "y", "c": "z"}, {"a": "x", "b": "no", "c": "no"}]
    predicates = [
        Predicate("a", EQ, "x", tier=1),
        Predicate("b", EQ, "y", tier=2),
        Predicate("c", EQ, "z", tier=3),
    ]

    outcome = filter_with_relaxation(rows, predicates, min_results=2)

    assert len(outcome.indices) == 2
    assert [p.column for p in outcome.relaxed] == ["c", "b"]


def test_hard_predicates_are_never_relaxed():
    rows = [{"stock": 0, "a": "x"}]
    predicates = [
        Predicate("stock", GTE, 1, hard=True, label="in stock"),
        Predicate("a", EQ, "nope", tier=3),
    ]

    outcome = filter_with_relaxation(rows, predicates, min_results=5)

    assert outcome.indices == []
    assert [p.column for p in outcome.relaxed] == ["a"]
    assert any(p.hard for p in outcome.applied)


# --- index -------------------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_index_builds_for_every_catalog(merchant_id):
    index = await _index(merchant_id)

    assert index.size >= 20
    assert len(index.ids) == index.size
    assert len(set(index.ids)) == index.size, "product ids must be unique"
    assert index.profile.roles.is_sellable


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_index_documents_describe_attributes_in_words(merchant_id):
    from app.retrieval.index import build_document

    index = await _index(merchant_id)
    document = build_document(index.rows[0], index.profile)

    assert len(document) > 40
    # A bare "18" is meaningless in embedding space; the field's own name must be present.
    assert any(spec.display_name in document for spec in index.profile.active_fields())


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_index_survives_a_reload_from_disk(merchant_id):
    from app.retrieval.index import load_index

    built = await _index(merchant_id)
    reloaded = load_index(merchant_id, built.profile)

    assert reloaded is not None
    assert reloaded.ids == built.ids
    assert reloaded.has_vectors == built.has_vectors


async def test_index_is_rejected_when_the_embedding_model_changes():
    """A silent dimension change returns nonsense rather than an error, so it must rebuild."""
    from app.retrieval.index import index_dir, load_index

    merchant_id = MERCHANTS[0]
    built = await _index(merchant_id)
    if not built.has_vectors:
        pytest.skip("no embedding backend available")

    import json

    meta_path = index_dir(merchant_id) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["embedding_model"] = "ollama:some-other-model"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert load_index(merchant_id, built.profile) is None


# --- search ------------------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_vague_query_returns_in_stock_results(merchant_id):
    index = await _index(merchant_id)

    result = await search(index, query="something good for everyday use")

    assert result.items, "a vague opening query must still return cards"
    assert all(_stock_of(index, row) > 0 for row in result.items)
    assert result.total_candidates > 0
    assert result.candidate_ids


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_out_of_stock_items_are_never_recommended(merchant_id):
    index = await _index(merchant_id)
    out_of_stock = [
        index.ids[i] for i, row in enumerate(index.rows) if _stock_of(index, row) <= 0
    ]
    assert out_of_stock, "fixture must contain an out-of-stock row to make this meaningful"

    result = await search(index, query="", k=100)

    assert not set(result.ids) & set(out_of_stock)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_over_constrained_query_relaxes_rather_than_returning_empty(merchant_id):
    index = await _index(merchant_id)
    profile = index.profile

    # Build a deliberately contradictory set of slots from real canonical values.
    enums = [
        f for f in profile.active_fields()
        if f.kind in (ColumnKind.CATEGORICAL_ENUM, ColumnKind.CATEGORICAL_MULTI)
        and len(f.canonical_values) >= 2
    ]
    if len(enums) < 2:
        pytest.skip("catalog has too few enum fields to over-constrain")

    slots = {
        enums[0].column: enums[0].canonical_values[0],
        enums[1].column: enums[1].canonical_values[-1],
    }
    for spec in enums[2:4]:
        slots[spec.column] = spec.canonical_values[-1]

    result = await search(index, query="anything", slots=slots)

    assert result.items or result.filters_relaxed, "a dead end kills the conversation"
    if result.filters_relaxed:
        assert all("description" in f for f in result.filters_relaxed)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_known_slots_actually_narrow_the_candidate_set(merchant_id):
    index = await _index(merchant_id)
    enum = next(
        (
            f
            for f in index.profile.active_fields()
            if f.kind is ColumnKind.CATEGORICAL_ENUM and len(f.canonical_values) >= 2
        ),
        None,
    )
    if enum is None:
        pytest.skip("catalog has no enum field")

    wide = await search(index, query="", k=100)
    narrow = await search(index, query="", slots={enum.column: enum.canonical_values[0]}, k=100)

    assert narrow.total_candidates <= wide.total_candidates
    assert narrow.total_candidates > 0


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_price_ceiling_is_a_hard_filter(merchant_id):
    index = await _index(merchant_id)
    price_column = index.profile.roles.price
    prices = sorted(
        float(r[price_column]) for r in index.rows if r.get(price_column) is not None
    )
    ceiling = prices[len(prices) // 4]

    result = await search(
        index,
        query="anything at all",
        extra_predicates=[Predicate(price_column, "lte", ceiling, hard=True, label="budget")],
        k=100,
    )

    for row in result.items:
        assert float(row[price_column]) <= ceiling
    assert not any(p["column"] == price_column for p in result.filters_relaxed)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_search_ranks_over_the_filtered_subset_only(merchant_id):
    index = await _index(merchant_id)
    if not index.has_vectors:
        pytest.skip("no embedding backend available")

    enum = next(
        (
            f
            for f in index.profile.active_fields()
            if f.kind is ColumnKind.CATEGORICAL_ENUM and f.canonical_values
        ),
        None,
    )
    if enum is None:
        pytest.skip("catalog has no enum field")

    value = enum.canonical_values[0]
    result = await search(index, query="the best one you have", slots={enum.column: value}, k=50)

    for row in result.items:
        cell = row.get(enum.column)
        rendered = cell if isinstance(cell, list) else [cell]
        assert any(str(v).casefold() == value.casefold() for v in rendered)


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_search_degrades_instead_of_hanging_when_embeddings_fail(merchant_id, monkeypatch):
    """A dead embedding backend must produce results and a caveat, never a stalled turn."""
    import app.retrieval.search as search_module

    index = await _index(merchant_id)

    async def dead(*_args, **_kwargs):
        return None

    monkeypatch.setattr(search_module, "embed_query", dead)

    result = await search(index, query="something for everyday use")

    assert result.items
    assert result.ranked_by == "price"
    assert result.degraded


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_filters_applied_are_reported_for_transparency(merchant_id):
    index = await _index(merchant_id)
    result = await search(index, query="anything")

    assert any(f.get("hard") for f in result.filters_applied), "stock filter must be reported"
    assert all("description" in f for f in result.filters_applied)


# --- slots -> predicates -----------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_unknown_slot_columns_are_ignored_not_guessed(merchant_id):
    """The path a hallucinated filter would otherwise take into the catalog."""
    index = await _index(merchant_id)

    predicates = predicates_from_slots(
        {"a_column_that_does_not_exist": "value"}, index.profile
    )

    assert predicates == []


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_dont_care_is_an_answered_slot_with_no_constraint(merchant_id):
    index = await _index(merchant_id)
    spec = index.profile.active_fields()[0]

    assert predicates_from_slots({spec.column: "any"}, index.profile) == []


async def test_registry_holds_two_unrelated_catalogs_at_once():
    """Required for the two-niche demo: switching catalogs must not mean a restart."""
    registry = get_registry()
    for merchant_id in MERCHANTS:
        await registry.get(merchant_id)

    assert len(registry.loaded()) >= 2
    categories = {registry.peek(m).profile.merchant_id for m in MERCHANTS}
    assert len(categories) == len(MERCHANTS)


async def test_cold_start_creates_a_profile_when_none_was_approved():
    """A shopper who arrives before the merchant approves anything must still be served."""
    merchant_id = MERCHANTS[0]
    store = get_profile_store()

    index = await get_registry().get(merchant_id)

    assert index is not None
    assert store.load(merchant_id) is not None
    assert any("not yet approved" in n for n in index.profile.notes)
