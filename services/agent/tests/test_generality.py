"""Stage I — the product claim, asserted.

> Point this system at a spreadsheet of any product category, in any column layout, and it
> derives its own understanding of that category, asks the questions that matter for it,
> and sells from it — with no code change.

Every test here is parameterized over every file in fixtures/catalogs/ and asserts only
category-agnostic properties. Dropping a new spreadsheet into that directory extends this
suite automatically, which is the point: proving the claim should need no code change
either.

A test that names a specific product attribute is a bug. Domain-specific assertions belong
in a per-fixture golden file, never here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.ingestion.classify import apply_classification, validate_classification
from app.ingestion.coerce import NULL_TOKENS
from app.ingestion.pipeline import analyze_file
from app.ingestion.profile_store import get_profile_store
from app.models.ingestion import ColumnKind
from app.retrieval.registry import get_registry
from app.retrieval.search import search
from app.tools.probe_attributes import rank_probes
from tests.fixtures_helper import CATALOGS, catalog_path, merchant_id_for

MERCHANTS = [merchant_id_for(name) for name in CATALOGS]


def test_the_corpus_is_actually_diverse():
    """A two-fixture suite proves nothing if both fixtures are the same shape."""
    assert len(CATALOGS) >= 2, "the claim needs at least two unrelated catalogues"

    suffixes = {catalog_path(name).suffix.lower() for name in CATALOGS}
    assert len(suffixes) >= 2, "at least two different file formats"

    results = [analyze_file(catalog_path(n), merchant_id=merchant_id_for(n)) for n in CATALOGS]

    # Different column naming conventions. The *dominant* style is what matters — a
    # single auto-generated column_N name is not a convention.
    def dominant_style(columns) -> str:
        underscores = sum(1 for c in columns if "_" in c)
        spaces = sum(1 for c in columns if " " in c)
        return "snake_case" if underscores > spaces else "Title Case"

    styles = {dominant_style(r.frame.columns) for r in results}
    assert len(styles) >= 2, f"fixtures share a column naming convention: {styles}"

    # Different currencies, proving per-column convention detection is doing real work.
    currencies = {
        spec.currency
        for r in results
        for spec in r.profile.fields
        if spec.currency
    }
    price_currencies = set()
    for r in results:
        price_spec = r.profile.field(r.profile.roles.price)
        if price_spec and price_spec.currency:
            price_currencies.add(price_spec.currency)
    assert len(price_currencies) >= 2, f"fixtures share a currency: {price_currencies}"

    # Different special cell shapes: one unit-bearing, one list-valued.
    applied = {
        spec.coercion.applied for r in results for spec in r.profile.fields
    }
    assert "unit_numeric" in applied
    assert "list" in applied


# --- ingestion ---------------------------------------------------------------


@pytest.mark.parametrize("name", CATALOGS)
def test_required_roles_are_detected_with_usable_confidence(name):
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    roles = result.profile.roles

    assert roles.is_sellable, f"missing {roles.missing_required()}"
    for role in ("id", "title", "price"):
        assert roles.confidence[role].confidence >= 0.45


@pytest.mark.parametrize("name", CATALOGS)
def test_no_null_token_ever_becomes_a_canonical_value(name):
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))

    for spec in result.profile.fields:
        for value in spec.canonical_values:
            assert value.strip().casefold() not in NULL_TOKENS, (
                f"{spec.column} exposes the null token {value!r} as a real choice"
            )


@pytest.mark.parametrize("name", CATALOGS)
def test_every_canonical_value_traces_to_data_the_profiler_saw(name):
    """The guard against invented attributes."""
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    observed = {p.name: set(p.value_counts) for p in result.column_profiles}

    for spec in result.profile.fields:
        if spec.kind is ColumnKind.BOOLEAN:
            continue
        for value in spec.canonical_values:
            assert value in observed[spec.column]
        for raw, canonical in spec.aliases.items():
            assert raw in observed[spec.column]
            assert canonical in spec.canonical_values


@pytest.mark.parametrize("name", CATALOGS)
def test_currency_values_are_in_a_sane_order_of_magnitude(name):
    """A misread decimal convention shows up as a 1000x error, not as a crash."""
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    prices = result.frame[result.profile.roles.price].dropna()

    assert len(prices) >= 0.8 * len(result.frame)
    assert prices.min() > 0
    assert prices.max() / max(prices.median(), 1e-9) < 1000


@pytest.mark.parametrize("name", CATALOGS)
def test_no_invented_column_survives_classification(name):
    """The model is given real columns and tries to add one. Validation must drop it."""
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    real = next(spec.column for spec in result.profile.fields)

    payload = {
        "category": "something derived",
        "category_confidence": 0.9,
        "fields": [
            {"column": real, "tier": 1, "layman_name": "Real one"},
            {"column": "a_column_that_never_existed", "tier": 1, "layman_name": "Invented"},
        ],
        "cross_field_rules": [
            {"if": "a_column_that_never_existed > 5", "then": "warn", "message": "nope"}
        ],
    }

    validated = validate_classification(payload, {s.column for s in result.profile.fields})
    applied = apply_classification(result.profile, validated)

    assert "a_column_that_never_existed" not in validated["fields"]
    assert validated["cross_field_rules"] == []
    assert "a_column_that_never_existed" not in {s.column for s in applied.fields}
    assert any("rejected model output" in note for note in applied.notes)


@pytest.mark.parametrize("name", CATALOGS)
def test_classification_cannot_overwrite_measured_facts(name):
    """FieldSpec's deterministic half is measured, not opined. Enforced here."""
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    spec = next(s for s in result.profile.fields if s.canonical_values)
    before = spec.model_dump(
        include={"canonical_values", "numeric_min", "numeric_max", "unit", "currency",
                 "null_rate", "distinct_count", "kind"}
    )

    payload = {
        "category": "x",
        "fields": [
            {
                "column": spec.column,
                "tier": 1,
                "layman_name": "Renamed",
                "canonical_values": ["FABRICATED"],
                "numeric_min": -999,
                "unit": "furlongs",
                "kind": "free_text",
                "suggested_required_before_purchase": True,
            }
        ],
    }
    validated = validate_classification(payload, {s.column for s in result.profile.fields})
    applied = apply_classification(result.profile, validated)
    after = applied.field(spec.column)

    assert after.model_dump(include=set(before)) == before
    assert after.layman_name == "Renamed"  # derived copy is writable
    assert after.tier == 1
    # A suggestion never becomes the merchant's decision.
    assert after.suggested_required_before_purchase is True
    assert after.required_before_purchase is False


# --- probing -----------------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_probe_ranking_is_non_empty_and_sensible(merchant_id):
    index = await get_registry().get(merchant_id)
    ranked = rank_probes(index.profile, index.rows)

    assert ranked
    assert ranked == sorted(ranked, key=lambda c: -c.score)
    for candidate in ranked:
        assert candidate.distinct > 1, "a single-valued column discriminates nothing"
        assert candidate.coverage >= 0.5, "a mostly-empty column filters away real matches"


# --- search ------------------------------------------------------------------


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_search_returns_only_in_stock_items(merchant_id):
    index = await get_registry().get(merchant_id)
    stock_column = index.profile.roles.stock

    result = await search(index, query="something for everyday use", k=100)

    assert result.items
    for row in result.items:
        assert float(row[stock_column]) > 0


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_over_constraining_relaxes_rather_than_dead_ends(merchant_id):
    index = await get_registry().get(merchant_id)
    enums = [
        f
        for f in index.profile.active_fields()
        if f.kind in (ColumnKind.CATEGORICAL_ENUM, ColumnKind.CATEGORICAL_MULTI)
        and len(f.canonical_values) >= 2
    ]
    slots = {spec.column: spec.canonical_values[-1] for spec in enums[:4]}

    result = await search(index, query="anything", slots=slots)

    assert result.items or result.filters_relaxed


# --- the full purchase path, end to end, over HTTP ---------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[7:].strip()
        elif line.startswith("data: ") and name:
            try:
                events.append((name, json.loads(line[6:])))
            except json.JSONDecodeError:
                pass
    return events


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_the_full_purchase_path_completes(merchant_id, monkeypatch):
    """Discover, decide and pay, over the real HTTP surface, for every catalogue.

    The model is scripted so the test is deterministic and free; everything else — policy,
    tools, re-verification, the confirm endpoint, the payment service — is the real path.
    """
    import app.agent.loop as loop_module
    from app.main import app as main_app
    from tests.scripted_llm import ScriptedLLM, calls, says

    index = await get_registry().get(merchant_id)
    stock_column = index.profile.roles.stock
    product_id = next(
        pid
        for pid, row in zip(index.ids, index.rows)
        if float(row.get(stock_column) or 0) > 0
    )

    scripted = ScriptedLLM(
        [
            calls("search_catalog", {"query": "something for everyday use"}),
            calls("probe_attributes", {}),
            says("Here are some options — which suits you?"),
            calls("build_cart", {"action": "add", "id": product_id, "quantity": 1}),
            calls("preview_transaction", {}),
            says("That is your total. Press confirm when you are ready."),
            calls("confirm_and_pay", {}),
            says("All done, thank you."),
        ]
    )
    monkeypatch.setattr(loop_module, "get_llm", lambda: scripted)

    transport = httpx.ASGITransport(app=main_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        first = await client.post(
            "/chat", json={"message": "I need something", "merchant_id": merchant_id}
        )
        assert first.status_code == 200
        events = _parse_sse(first.text)
        session_id = next(d["session_id"] for n, d in events if n == "session")

        kinds = [n for n, _ in events]
        assert "products" in kinds
        assert "probe" in kinds, "a question must arrive alongside the products"
        assert kinds.index("products") < kinds.index("probe")

        second = await client.post(
            "/chat",
            json={
                "message": "the first one please",
                "merchant_id": merchant_id,
                "session_id": session_id,
            },
        )
        events = _parse_sse(second.text)
        preview = next((d for n, d in events if n == "preview"), None)
        assert preview is not None, "no preview card was produced"
        assert preview["total"] > 0
        assert not any(n == "receipt" for n, _ in events), "nothing may be charged yet"

        confirmed = await client.post(
            "/chat/confirm",
            json={"session_id": session_id, "preview_id": preview["preview_id"]},
        )
        assert confirmed.status_code == 200
        receipt = next((d for n, d in _parse_sse(confirmed.text) if n == "receipt"), None)

        assert receipt is not None, "the confirmed purchase produced no receipt"
        assert receipt["transaction_id"]
        assert receipt["total"] == pytest.approx(preview["total"])

        state = (await client.get(f"/session/{session_id}")).json()
        assert state["cart"]["items"] == []
        assert state["authorised"] is False, "the token must be burnt after use"


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_cold_catalogue_can_be_sold_from_without_approval(merchant_id):
    """Permanent fallback: no approved profile must never mean no sale."""
    store = get_profile_store()
    index = await get_registry().get(merchant_id)

    assert index is not None
    assert store.load(merchant_id) is not None
    assert index.profile.roles.is_sellable

    result = await search(index, query="anything at all")
    assert result.items


@pytest.mark.parametrize("merchant_id", MERCHANTS)
async def test_a_product_with_no_usable_price_is_never_recommended(merchant_id):
    """Real exports contain "call for quote" cells. Showing one is a guaranteed dead end."""
    index = await get_registry().get(merchant_id)
    price_column = index.profile.roles.price

    unpriced = [
        index.ids[i] for i, row in enumerate(index.rows) if row.get(price_column) is None
    ]
    assert unpriced, "fixture must contain an unparseable price to make this meaningful"

    result = await search(index, query="anything at all", k=100)

    assert not set(result.ids) & set(unpriced)
    assert all(row.get(price_column) is not None for row in result.items)
