"""Stage B — loader and coercion.

Deliberately adversarial. Every case here is one that silently corrupts a real catalog:
a European price read as US, a comma inside prose read as a list delimiter, a legitimate
"None" level read as missing, an integer SKU stringified to "1001.0".
"""

from __future__ import annotations

import io

import numpy as np
import pandas as pd
import pytest

from app.ingestion.coerce import (
    NULL_TOKENS,
    coerce_column,
    coerce_frame,
    detect_decimal_convention,
    normalise_nulls,
    parse_number,
)
from app.ingestion.loader import (
    UnreadableFile,
    clean_column_names,
    detect_format,
    detect_header_row,
    load_table,
    sniff_delimiter,
)
from tests.fixtures_helper import CATALOGS, catalog_path


def _s(values) -> pd.Series:
    return pd.Series(values, dtype=object)


# --- loader: format and encoding ---------------------------------------------


def test_detect_format_prefers_content_over_extension():
    """A misnamed upload is common: .csv holding a real xlsx must still load."""
    assert detect_format(b"PK\x03\x04rest", "catalog.csv") == "excel"
    assert detect_format(b"\xd0\xcf\x11\xe0", "catalog.xlsx") == "excel_legacy"
    assert detect_format(b"a,b,c\n1,2,3", "catalog.xlsx") == "text"
    assert detect_format(b"a,b,c\n1,2,3", "catalog.csv") == "text"


@pytest.mark.parametrize(
    "raw,expected_delimiter",
    [
        (b"a,b,c\r\n1,2,3\r\n4,5,6\r\n", ","),
        (b"a\tb\tc\n1\t2\t3\n4\t5\t6\n", "\t"),
        (b"a;b;c\n1;2;3\n4;5;6\n", ";"),
        (b"a|b|c\n1|2|3\n4|5|6\n", "|"),
    ],
)
def test_loads_every_text_delimiter(raw, expected_delimiter):
    table = load_table(raw, filename="catalog.csv")
    assert table.detected_delimiter == expected_delimiter
    assert list(table.df.columns) == ["a", "b", "c"]
    assert len(table.df) == 2


def test_utf8_bom_does_not_corrupt_the_first_column_name():
    table = load_table("sku,name\r\n1,Widget\r\n".encode("utf-8-sig"), filename="c.csv")
    assert table.detected_encoding == "utf-8-sig"
    assert list(table.df.columns) == ["sku", "name"]


def test_cp1252_falls_back_without_raising():
    table = load_table("name,note\r\nCaf\xe9,fa\xe7ade\r\n".encode("cp1252"), filename="c.csv")
    assert table.detected_encoding in ("cp1252", "latin-1")
    assert len(table.df) == 1


def test_empty_file_is_an_explicit_error():
    with pytest.raises(UnreadableFile):
        load_table(b"", filename="c.csv")


# --- loader: header detection and column hygiene -----------------------------


def test_header_row_detected_below_junk_rows():
    raw = pd.DataFrame(
        [
            ["Acme Wholesale Catalogue", None, None],
            [None, None, None],
            ["sku", "name", "price"],
            ["A1", "Widget", "10"],
        ]
    )
    assert detect_header_row(raw) == 2


def test_header_row_zero_when_the_file_is_already_clean():
    raw = pd.DataFrame([["sku", "name"], ["A1", "Widget"], ["A2", "Gadget"]])
    assert detect_header_row(raw) == 0


def test_column_names_trimmed_deduped_and_named():
    names, notes = clean_column_names(["  Item   Code ", "price", "price", None, "Unnamed: 4"])
    assert names == ["Item Code", "price", "price_2", "column_4", "column_5"]
    assert len(notes) == 3


def test_nan_header_cell_does_not_become_a_column_called_nan():
    names, _ = clean_column_names(["sku", np.nan])
    assert names == ["sku", "column_2"]


def test_fully_empty_rows_and_columns_dropped_with_a_note():
    raw = b"sku,name,junk\r\nA1,Widget,\r\n,,\r\nA2,Gadget,\r\n"
    table = load_table(raw, filename="c.csv")
    assert len(table.df) == 2
    assert "junk" not in table.df.columns
    assert any("empty column" in n.message for n in table.notes)


# --- coercion: nulls ---------------------------------------------------------


def test_null_tokens_become_missing():
    coerced = normalise_nulls(_s(["10", "N/A", "-", "", "?", "TBD", "null", "20"]))
    assert int(coerced.isna().sum()) == 6
    assert coerced.dropna().tolist() == ["10", "20"]


def test_none_is_not_treated_as_missing():
    """"None" is a real level in real enums; destroying it is a correctness bug."""
    assert "none" not in NULL_TOKENS
    coerced = normalise_nulls(_s(["High", "None", "Low"]))
    assert coerced.isna().sum() == 0


# --- coercion: currency ------------------------------------------------------


@pytest.mark.parametrize(
    "values,convention",
    [
        (["$1,299.00", "$149.00", "$89.99"], "us"),
        (["1.299,00 €", "12,50 €", "8,90 €"], "eu"),
        (["45.00", "1299.50"], "us"),
        (["45,00", "1299,50"], "eu"),
    ],
)
def test_decimal_convention_detected_per_column(values, convention):
    assert detect_decimal_convention(values) == convention


def test_european_prices_are_not_read_as_thousands():
    """The 1000x error. "12,50 €" must be 12.50, never 1250."""
    coerced, report = coerce_column(_s(["12,50 €", "1.299,00 €", "8,90 €"]))
    assert report.applied == "currency"
    assert report.currency == "EUR"
    assert report.decimal_convention == "eu"
    assert coerced.tolist() == [12.5, 1299.0, 8.9]


def test_us_prices_keep_their_magnitude():
    coerced, report = coerce_column(_s(["$1,299.00", "$149.00", "$89.99"]))
    assert report.currency == "USD"
    assert coerced.tolist() == [1299.0, 149.0, 89.99]


def test_unparseable_price_is_reported_not_hidden():
    coerced, report = coerce_column(_s(["$10.00", "$20.00", "call for quote", "$30.00"]))
    assert report.applied == "currency"
    assert report.failed_cells == 1
    assert bool(pd.isna(coerced.iloc[2]))


# --- coercion: units ---------------------------------------------------------


@pytest.mark.parametrize(
    "values,unit,expected",
    [
        (["18 V", "230 V", "12V"], "V", [18.0, 230.0, 12.0]),
        (["1.2 kg", "3.4kg", "10.2 kg"], "kg", [1.2, 3.4, 10.2]),
        (["13mm", "8 mm", "12 mm"], "mm", [13.0, 8.0, 12.0]),
        (["100 g", "250g", "30 g"], "g", [100.0, 250.0, 30.0]),
        (["6.1-inch", "5.4-inch", "6.7 inch"], "inch", [6.1, 5.4, 6.7]),
    ],
)
def test_unit_bearing_numerics(values, unit, expected):
    coerced, report = coerce_column(_s(values))
    assert report.applied == "unit_numeric"
    assert report.unit == unit
    assert coerced.tolist() == expected


def test_mixed_units_are_left_as_text_rather_than_silently_flattened():
    coerced, report = coerce_column(_s(["10 kg", "4 mm", "7 V", "9 ml", "3 cm"]))
    assert report.applied != "unit_numeric"


# --- coercion: lists ---------------------------------------------------------


def test_semicolon_list_detected_even_when_half_the_rows_are_single_valued():
    coerced, report = coerce_column(
        _s(["organic; fair-trade", "organic", "organic; caffeine-free", "single-estate"])
    )
    assert report.applied == "list"
    assert report.list_delimiter == ";"
    assert coerced.iloc[0] == ["organic", "fair-trade"]
    assert coerced.iloc[1] == ["organic"]


def test_pipe_list_with_high_element_cardinality_still_detected():
    values = [f"note{i} | note{i + 1} | note{i + 2}" for i in range(20)]
    _coerced, report = coerce_column(_s(values))
    assert report.applied == "list"
    assert report.list_delimiter == "|"


def test_comma_inside_prose_is_not_a_list_delimiter():
    """The failure that produces one bogus enum value per unique sentence."""
    values = [
        "Demolition saw for timber, nail-embedded stock and pipe.",
        "A brisk cup that stands up to milk, without turning thin.",
        "Fine finishing sander with dust extraction, and a soft grip.",
        "Sheet goods and framing saw, with a cast base and riving knife.",
    ]
    _coerced, report = coerce_column(_s(values))
    assert report.applied != "list"


# --- coercion: booleans, percentages, numbers --------------------------------


@pytest.mark.parametrize(
    "values",
    [["Yes", "No", "Yes"], ["Y", "N", "Y"], ["true", "false", "true"], ["1", "0", "1"]],
)
def test_boolean_vocabularies(values):
    coerced, report = coerce_column(_s(values))
    assert report.applied == "boolean"
    assert coerced.tolist() == [True, False, True]


def test_percentages_stored_as_fractions():
    coerced, report = coerce_column(_s(["15%", "7.5%", "100%"]))
    assert report.applied == "percentage"
    assert coerced.tolist() == [0.15, 0.075, 1.0]


def test_single_valued_column_is_not_forced_into_a_boolean():
    _coerced, report = coerce_column(_s(["Yes", "Yes", "Yes"]))
    assert report.applied != "boolean"


def test_parse_number_returns_none_rather_than_raising():
    assert parse_number("not a number") is None
    assert parse_number("") is None


# --- fixtures: both catalogs, the same code path -----------------------------


@pytest.mark.parametrize("name", CATALOGS)
def test_every_fixture_loads(name):
    table = load_table(catalog_path(name))
    assert len(table.df) >= 20
    assert len(table.df.columns) >= 8
    assert not any(str(c).lower().startswith(("unnamed", "nan")) for c in table.df.columns)
    assert table.notes


@pytest.mark.parametrize("name", CATALOGS)
def test_every_fixture_coerces_without_losing_rows(name):
    table = load_table(catalog_path(name))
    frame, reports = coerce_frame(table.df)

    assert len(frame) == len(table.df)
    assert set(reports) == set(table.df.columns)

    # Exactly one currency column per catalog, in a sane order of magnitude.
    currency_columns = [c for c, r in reports.items() if r.applied == "currency"]
    assert len(currency_columns) == 1
    prices = frame[currency_columns[0]].dropna()
    assert prices.min() > 0
    assert prices.max() < 100_000  # a 1000x convention error would blow past this

    # Every catalog has at least one out-of-stock row for the never-recommend rule.
    numeric_columns = [c for c, r in reports.items() if r.applied == "numeric"]
    assert any((frame[c].dropna() == 0).any() for c in numeric_columns)


def test_csv_fixture_shape():
    table = load_table(catalog_path("power_tools.csv"))
    assert table.detected_encoding == "utf-8-sig"
    assert table.detected_delimiter == ","
    assert table.header_row == 0
    assert table.sheet is None


def test_xlsx_fixture_picks_the_data_sheet_and_skips_junk_rows():
    table = load_table(catalog_path("tea_and_infusions.xlsx"))
    assert table.sheet == "Catalogue"
    assert table.header_row == 2
    assert any("sheets" in n.message for n in table.notes)
    assert "column_13" in table.df.columns  # the unnamed column, named not dropped


def test_xlsx_reader_keeps_a_literal_none_level():
    table = load_table(catalog_path("tea_and_infusions.xlsx"))
    frame, _reports = coerce_frame(table.df)
    caffeine = [c for c in frame.columns if frame[c].dropna().isin(["None"]).any()]
    assert caffeine, "a legitimate 'None' value was destroyed by NA handling"


def test_upload_from_bytes_matches_upload_from_path():
    path = catalog_path("power_tools.csv")
    from_path = load_table(path)
    from_bytes = load_table(path.read_bytes(), filename=path.name)
    pd.testing.assert_frame_equal(from_path.df, from_bytes.df)
