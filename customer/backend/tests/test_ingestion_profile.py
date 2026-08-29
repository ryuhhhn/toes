"""Stage C — profiler, role mapping, bootstrap.

Every assertion here is category-agnostic. Where a test needs to know something about a
specific catalog, it asserts a *property* (an enum column exists, an id was found) rather
than naming an attribute.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.ingestion.bootstrap import bootstrap_profile
from app.ingestion.coerce import coerce_column, coerce_frame
from app.ingestion.pipeline import analyze_file, analyze_rows, frame_to_rows, row_hash
from app.ingestion.profiler import profile_column, profile_frame
from app.ingestion.schema_map import map_roles, name_score, normalise_name
from app.models.ingestion import ColumnKind
from tests.fixtures_helper import CATALOGS, catalog_path, merchant_id_for


def _profile(values, name="col"):
    series, report = coerce_column(pd.Series(values, dtype=object))
    return profile_column(series, name=name, report=report)


# --- column kinds ------------------------------------------------------------


def test_url_column_is_a_url_not_an_identifier():
    """URLs are always all-unique; uniqueness alone must not make them the id."""
    values = [f"https://img.example.com/{i}.jpg" for i in range(20)]
    assert _profile(values).kind is ColumnKind.URL


def test_prose_column_is_free_text_not_an_identifier():
    values = [
        f"A distinctive product built for job number {i} and everyday working use."
        for i in range(20)
    ]
    assert _profile(values).kind is ColumnKind.FREE_TEXT


def test_code_shaped_column_is_an_identifier():
    assert _profile([f"AB-{1000 + i}" for i in range(20)]).kind is ColumnKind.IDENTIFIER


def test_near_unique_labels_are_not_identifiers():
    """A grade or variant column can be almost all-distinct without being an id."""
    values = [
        "TGFOP", "Deep Steamed", "Shaded", "Hand Rolled", "Whole Flower",
        "Cut Leaf", "Stone Ground", "High Mountain", "Roasted", "Superior",
    ]
    assert _profile(values).kind is not ColumnKind.IDENTIFIER


def test_small_distinct_set_is_an_enum_even_on_a_short_catalog():
    values = ["alpha", "beta", "gamma"] * 3 + ["delta"]
    assert _profile(values).kind is ColumnKind.CATEGORICAL_ENUM


def test_mostly_null_column_is_unusable_and_unprobeable():
    profile = _profile(["value"] + [None] * 30)
    assert profile.kind is ColumnKind.UNUSABLE
    assert profile.is_probeable is False


def test_multi_valued_column_counts_elements_not_combinations():
    """Counting combinations invents one bogus enum value per unique pairing."""
    profile = _profile(
        ["a; b", "b; c", "a; c", "a", "b; c", "a; b"] * 3
    )
    assert profile.kind is ColumnKind.CATEGORICAL_MULTI
    assert set(profile.value_counts) == {"a", "b", "c"}


def test_numeric_range_recorded():
    profile = _profile(["10", "20", "30", "40"])
    assert profile.kind is ColumnKind.NUMERIC
    assert (profile.numeric_min, profile.numeric_max) == (10.0, 40.0)


# --- role mapping ------------------------------------------------------------


def test_normalise_name_handles_separator_styles():
    assert normalise_name("qty_on_hand") == "qty on hand"
    assert normalise_name("Units In Stock") == "units in stock"
    assert normalise_name("Item-Code") == "item code"


def test_name_score_exact_beats_fuzzy():
    assert name_score("price", ("price",)) == 1.0
    assert 0 < name_score("retail_price_gbp", ("retail price",)) < 1.0
    assert name_score("colour", ("price",)) < 0.5


def test_shape_disqualifies_a_convincing_name():
    """A column called "price" holding prose is not the price column."""
    profiles = [
        _profile([f"Some descriptive sentence number {i} about the item." for i in range(20)],
                 name="price"),
        _profile([f"{10 + i}.00" for i in range(20)], name="amount_charged"),
    ]
    roles = map_roles(profiles)
    assert roles.price != "price"


def test_role_overrides_win_outright():
    profiles = [
        _profile([f"AB-{i}" for i in range(20)], name="sku"),
        _profile([f"XY-{i}" for i in range(20)], name="legacy_code"),
    ]
    roles = map_roles(profiles, {"id": "legacy_code"})
    assert roles.id == "legacy_code"
    assert roles.confidence["id"].confidence == 1.0


def test_one_column_is_never_given_two_roles():
    result = analyze_file(catalog_path(CATALOGS[0]), merchant_id="m")
    roles = result.profile.roles
    assigned = [r for r in (roles.id, roles.title, roles.price, roles.stock, roles.image) if r]
    assert len(assigned) == len(set(assigned))
    assert not set(assigned) & set(roles.text)


def test_missing_price_blocks_checkout_structurally():
    profiles = [
        _profile([f"AB-{i}" for i in range(20)], name="sku"),
        _profile([f"Product {i}" for i in range(20)], name="product_name"),
    ]
    roles = map_roles(profiles)
    assert roles.is_sellable is False
    assert "price" in roles.missing_required()


# --- bootstrap ---------------------------------------------------------------


@pytest.mark.parametrize("name", CATALOGS)
def test_bootstrap_produces_a_sellable_profile_with_no_llm(name):
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    profile = result.profile

    assert profile.derived_by == "bootstrap"
    assert profile.status == "draft"
    assert profile.roles.is_sellable
    assert profile.roles.image
    assert profile.roles.text
    assert profile.source.row_count >= 20
    assert profile.source.row_hash


@pytest.mark.parametrize("name", CATALOGS)
def test_bootstrap_fields_are_attributes_not_plumbing(name):
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    profile = result.profile
    roles = profile.roles

    columns = {f.column for f in profile.fields}
    assert roles.id not in columns
    assert roles.title not in columns
    assert roles.image not in columns
    assert not columns & set(roles.text)

    assert profile.probeable_fields(), "nothing to ask the shopper about"
    assert all(f.tier == 2 for f in profile.fields)  # no priors without a model
    assert all(f.layman_name is None for f in profile.fields)


@pytest.mark.parametrize("name", CATALOGS)
def test_canonical_values_trace_back_to_real_data(name):
    """The guard against invented attributes, asserted at the bootstrap stage."""
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    observed = {p.name: set(p.value_counts) for p in result.column_profiles}

    for spec in result.profile.fields:
        if spec.kind is ColumnKind.BOOLEAN:
            continue
        for value in spec.canonical_values:
            assert value in observed[spec.column], f"{spec.column}={value!r} was never seen"


@pytest.mark.parametrize("name", CATALOGS)
def test_numeric_fields_get_usable_bins(name):
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    numeric = [f for f in result.profile.fields if f.kind is ColumnKind.NUMERIC]
    assert numeric
    for spec in numeric:
        assert spec.bins
        for low, high in spec.bins:
            assert low <= high


@pytest.mark.parametrize("name", CATALOGS)
def test_ingest_notes_report_what_happened(name):
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    assert result.profile.notes
    assert any("rows" in note for note in result.profile.notes)


def test_mostly_empty_columns_are_reported_and_excluded():
    result = analyze_file(catalog_path("tea_and_infusions.xlsx"), merchant_id="tea")
    assert any("mostly-empty" in note for note in result.profile.notes)
    assert "column_13" not in {f.column for f in result.profile.fields}


# --- rows in, rows out -------------------------------------------------------


@pytest.mark.parametrize("name", CATALOGS)
def test_rows_are_json_safe(name):
    result = analyze_file(catalog_path(name), merchant_id=merchant_id_for(name))
    import json

    json.dumps(result.rows)  # must not raise
    assert len(result.rows) == result.profile.source.row_count


def test_row_hash_is_order_independent():
    rows = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    assert row_hash(rows) == row_hash(list(reversed(rows)))
    assert row_hash(rows) != row_hash([{"id": "a", "v": 1}, {"id": "b", "v": 3}])


def test_analyze_rows_matches_analyze_file():
    """The merchant API path and the upload path must produce the same understanding."""
    path = catalog_path("power_tools.csv")
    from_file = analyze_file(path, merchant_id="m")

    raw = frame_to_rows(from_file.table.df)  # pre-coercion rows, as the merchant would serve
    from_rows = analyze_rows(raw, merchant_id="m")

    assert from_rows.profile.roles.model_dump(exclude={"confidence"}) == (
        from_file.profile.roles.model_dump(exclude={"confidence"})
    )
    assert {f.column for f in from_rows.profile.fields} == {
        f.column for f in from_file.profile.fields
    }
