"""Column coercion.

Every coercer is applied to a **whole column or not at all**, after winning a confidence
check across that column. Per-cell guessing is what produces silent 1000x price errors and
enum values that are really one-off typos.

Each coercer returns a CoercionReport so the approval screen can say "we read Retail Price
as currency (EUR, European decimal convention), 1 cell unparseable" — cheap transparency
that buys a lot of merchant trust.

Nothing here knows what is being sold.

Note on ``none``: it is deliberately **not** a null token. Blank, ``-`` and ``N/A`` are
unambiguously missing, but "None" is frequently a real level in a real enum — a rating
scale, a list of included extras, a cover period. Leaking a rare null token into an enum
is cosmetic; destroying a legitimate value is a correctness bug, so the ambiguous tokens
are left alone.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

import numpy as np
import pandas as pd

from app.models.ingestion import CoercionReport

log = logging.getLogger(__name__)

NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        "?",
        "??",
        ".",
        "n/a",
        "n.a.",
        "n.a",
        "na",
        "#n/a",
        "#na",
        "null",
        "nan",
        "nil",
        "tbd",
        "tba",
        "not available",
        "not applicable",
    }
)

TRUE_TOKENS = frozenset({"yes", "y", "true", "t", "1", "✓", "✔", "x", "in stock", "available"})
FALSE_TOKENS = frozenset({"no", "n", "false", "f", "0", "✗", "✘", "out of stock", "unavailable"})

CURRENCY_SYMBOLS = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "₩": "KRW",
    "R$": "BRL",
    "kr": "SEK",
    "zł": "PLN",
}
CURRENCY_CODES = {
    "USD", "EUR", "GBP", "JPY", "INR", "CAD", "AUD", "CHF", "CNY", "SEK", "NZD",
    "MXN", "SGD", "HKD", "NOK", "KRW", "TRY", "BRL", "ZAR", "PLN", "DKK",
}

#: ; and | practically never occur inside prose, so they need only a light guard.
#: , and / do, so they need a strict one.
UNAMBIGUOUS_DELIMITERS = (";", "|")
AMBIGUOUS_DELIMITERS = (",", "/")

_NUMBER_UNIT = re.compile(
    r"^\s*([+-]?\d{1,3}(?:[ ,.]\d{3})*(?:[.,]\d+)?|[+-]?\d+(?:[.,]\d+)?)"
    r"\s*[-–]?\s*"
    r"([A-Za-z°µΩ\"'][A-Za-z0-9°µΩ/\.\"']{0,11})\s*$"
)
_BARE_NUMBER = re.compile(r"^[+-]?\d{1,3}(?:[ ,.]\d{3})*(?:[.,]\d+)?$|^[+-]?\d+(?:[.,]\d+)?$")
_PERCENT = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*%\s*$")

#: Shared with the profiler so "what counts as a URL" is defined exactly once.
URL_RE = re.compile(r"^(https?://|www\.)", re.IGNORECASE)


# --- nulls -------------------------------------------------------------------


def is_null_token(value: Any) -> bool:
    if value is None:
        return True
    # Lists are legitimate cell values post-coercion; pd.isna would return an array.
    if not isinstance(value, (list, tuple, set)) and pd.isna(value):
        return True
    return str(value).strip().casefold() in NULL_TOKENS


def parse_boolean(value: Any) -> bool | None:
    """Text to a real bool, or None when it is neither.

    Never use bool() on a shopper's answer: bool("No") is True, so a shopper who says no
    silently gets the yes results. Returning None lets the caller drop an unusable filter
    rather than invert it.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().casefold()
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    return None


def normalise_nulls(series: pd.Series) -> pd.Series:
    """Runs first. Every later coercer assumes missing values are already NaN."""
    return series.map(lambda v: np.nan if is_null_token(v) else str(v).strip())


def _non_null(series: pd.Series) -> pd.Series:
    return series.dropna()


# --- number parsing ----------------------------------------------------------


def _decimal_votes(text: str) -> tuple[str | None, float]:
    """Which character is the decimal separator in this cell, and how sure are we?"""
    digits = re.sub(r"[^\d.,]", "", text)
    has_dot, has_comma = "." in digits, "," in digits

    if has_dot and has_comma:
        # 1,299.00 vs 1.299,00 — whichever comes last is the decimal point.
        return (".", 1.0) if digits.rfind(".") > digits.rfind(",") else (",", 1.0)

    for char, other in ((".", ","), (",", ".")):
        if char not in digits:
            continue
        occurrences = digits.count(char)
        if occurrences > 1:
            return other, 1.0  # repeated separator can only be grouping
        tail = digits.split(char)[-1]
        if len(tail) == 3:
            return other, 0.5  # 1,299 / 1.299 — probably grouping, but weakly
        return char, 1.0

    return None, 0.0


def detect_decimal_convention(values: Iterable[str]) -> str:
    """"us" (1,299.00) or "eu" (1.299,00), decided per column and never per cell."""
    scores = {".": 0.0, ",": 0.0}
    for value in values:
        char, weight = _decimal_votes(str(value))
        if char:
            scores[char] += weight
    return "eu" if scores[","] > scores["."] else "us"


def parse_number(text: str, convention: str = "us") -> float | None:
    cleaned = re.sub(r"[^\d.,+-]", "", str(text))
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    if convention == "eu":
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# --- coercers ----------------------------------------------------------------
# Each returns (series, report) if it wins its confidence check for the whole column,
# or None to pass.


def try_boolean(series: pd.Series) -> tuple[pd.Series, CoercionReport] | None:
    values = _non_null(series)
    if values.empty:
        return None

    tokens = {str(v).strip().casefold() for v in values}
    if len(tokens) > 2 or not tokens <= (TRUE_TOKENS | FALSE_TOKENS):
        return None
    # A single-valued column is not evidence of a boolean; leave it to the enum path.
    if len(tokens) < 2:
        return None

    coerced = series.map(
        lambda v: np.nan if pd.isna(v) else (str(v).strip().casefold() in TRUE_TOKENS)
    )
    return coerced, CoercionReport(
        applied="boolean",
        detail=f"read {sorted(tokens)} as true/false",
        total_cells=len(values),
    )


def _currency_of(text: str) -> str | None:
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    upper = text.upper()
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", upper):
            return code
    return None


def try_currency(series: pd.Series) -> tuple[pd.Series, CoercionReport] | None:
    values = _non_null(series)
    if values.empty:
        return None

    marked = [(v, _currency_of(str(v))) for v in values]
    with_currency = [code for _, code in marked if code]
    if len(with_currency) / len(values) < 0.6:
        return None

    currency = max(set(with_currency), key=with_currency.count)
    convention = detect_decimal_convention(str(v) for v, code in marked if code)

    coerced = series.map(
        lambda v: np.nan if pd.isna(v) else _or_nan(parse_number(str(v), convention))
    )
    failed = int(coerced.isna().sum() - series.isna().sum())

    return coerced, CoercionReport(
        applied="currency",
        detail=f"parsed as {currency} using {convention} decimal convention",
        currency=currency,
        decimal_convention=convention,
        failed_cells=max(failed, 0),
        total_cells=len(values),
    )


def try_percentage(series: pd.Series) -> tuple[pd.Series, CoercionReport] | None:
    values = _non_null(series)
    if values.empty:
        return None

    matches = [v for v in values if _PERCENT.match(str(v))]
    if len(matches) / len(values) < 0.8:
        return None

    def convert(value: Any) -> float:
        if pd.isna(value):
            return np.nan
        match = _PERCENT.match(str(value))
        if not match:
            return np.nan
        number = parse_number(match.group(1))
        return np.nan if number is None else number / 100.0

    coerced = series.map(convert)
    failed = int(coerced.isna().sum() - series.isna().sum())
    return coerced, CoercionReport(
        applied="percentage",
        detail="parsed as a percentage and stored as a fraction",
        unit="%",
        failed_cells=max(failed, 0),
        total_cells=len(values),
    )


def try_unit_numeric(series: pd.Series) -> tuple[pd.Series, CoercionReport] | None:
    """"18 V", "1.2 kg", "13mm", "6.1-inch" -> value + a unit stored on the field spec."""
    values = _non_null(series)
    if values.empty:
        return None

    parsed: list[tuple[float, str]] = []
    for value in values:
        match = _NUMBER_UNIT.match(str(value))
        if not match:
            continue
        number = parse_number(match.group(1))
        if number is not None:
            parsed.append((number, match.group(2)))

    if len(parsed) / len(values) < 0.8:
        return None

    units = [unit.casefold() for _, unit in parsed]
    dominant = max(set(units), key=units.count)
    if units.count(dominant) / len(units) < 0.8:
        return None  # mixed units in one column: leave as text rather than lie about it

    display_unit = next(unit for _, unit in parsed if unit.casefold() == dominant)

    def convert(value: Any) -> float:
        if pd.isna(value):
            return np.nan
        match = _NUMBER_UNIT.match(str(value))
        if not match or match.group(2).casefold() != dominant:
            return np.nan
        return _or_nan(parse_number(match.group(1)))

    coerced = series.map(convert)
    failed = int(coerced.isna().sum() - series.isna().sum())
    return coerced, CoercionReport(
        applied="unit_numeric",
        detail=f"parsed as a number in {display_unit}",
        unit=display_unit,
        failed_cells=max(failed, 0),
        total_cells=len(values),
    )


def try_plain_numeric(series: pd.Series) -> tuple[pd.Series, CoercionReport] | None:
    values = _non_null(series)
    if values.empty:
        return None

    matches = [v for v in values if _BARE_NUMBER.match(str(v).strip())]
    if len(matches) / len(values) < 0.9:
        return None

    convention = detect_decimal_convention(str(v) for v in matches)
    coerced = series.map(
        lambda v: np.nan if pd.isna(v) else _or_nan(parse_number(str(v), convention))
    )
    failed = int(coerced.isna().sum() - series.isna().sum())
    return coerced, CoercionReport(
        applied="numeric",
        detail=f"parsed as a number using {convention} decimal convention",
        decimal_convention=convention,
        failed_cells=max(failed, 0),
        total_cells=len(values),
    )


def _split_stats(values: pd.Series, delimiter: str) -> tuple[float, float, int]:
    """(share of cells containing the delimiter, mean tokens per element, distinct elements)."""
    containing = 0
    elements: list[str] = []
    for value in values:
        text = str(value)
        if delimiter in text:
            containing += 1
        elements.extend(part.strip() for part in text.split(delimiter) if part.strip())

    share = containing / len(values) if len(values) else 0.0
    mean_tokens = (
        sum(len(e.split()) for e in elements) / len(elements) if elements else 0.0
    )
    return share, mean_tokens, len({e.casefold() for e in elements})


def try_list(series: pd.Series) -> tuple[pd.Series, CoercionReport] | None:
    values = _non_null(series)
    if values.empty:
        return None

    # A URL is not a list of its path segments. Every image column in the world would
    # otherwise be shredded on "/" and lose the role that renders the product card.
    if sum(1 for v in values if URL_RE.match(str(v))) / len(values) >= 0.5:
        return None

    best: tuple[str, float] | None = None

    for delimiter in UNAMBIGUOUS_DELIMITERS:
        share, mean_tokens, _distinct = _split_stats(values, delimiter)
        # A quarter of rows is plenty: list columns routinely hold single values.
        if share >= 0.25 and mean_tokens <= 5.0:
            if best is None or share > best[1]:
                best = (delimiter, share)

    if best is None:
        for delimiter in AMBIGUOUS_DELIMITERS:
            share, mean_tokens, distinct = _split_stats(values, delimiter)
            # Prose is full of commas, so demand short elements drawn from a small set.
            if (
                share >= 0.6
                and mean_tokens <= 3.0
                and distinct <= max(25, int(0.5 * len(values)))
            ):
                if best is None or share > best[1]:
                    best = (delimiter, share)

    if best is None:
        return None

    delimiter = best[0]
    coerced = series.map(
        lambda v: np.nan
        if pd.isna(v)
        else [part.strip() for part in str(v).split(delimiter) if part.strip()]
    )
    return coerced, CoercionReport(
        applied="list",
        detail=f"split on {delimiter!r} into multiple values",
        list_delimiter=delimiter,
        total_cells=len(values),
    )


def _or_nan(value: float | None) -> float:
    return np.nan if value is None else value


# --- orchestration -----------------------------------------------------------

#: Order matters. Boolean before numeric so a 1/0 flag is not read as a measurement;
#: currency and units before plain numeric so their markers are not stripped blindly;
#: list last so a delimiter inside otherwise-numeric text does not win.
COERCERS = (
    try_boolean,
    try_currency,
    try_percentage,
    try_unit_numeric,
    try_plain_numeric,
    try_list,
)


def coerce_column(series: pd.Series) -> tuple[pd.Series, CoercionReport]:
    cleaned = normalise_nulls(series)
    null_count = int(cleaned.isna().sum())

    for coercer in COERCERS:
        try:
            outcome = coercer(cleaned)
        except Exception as exc:  # noqa: BLE001 - one bad column must not fail an ingest
            log.warning("coercer %s failed on %r: %s", coercer.__name__, series.name, exc)
            continue
        if outcome is not None:
            coerced, report = outcome
            return coerced, report

    return cleaned, CoercionReport(
        applied=None,
        detail="left as text",
        total_cells=int(len(cleaned) - null_count),
    )


def coerce_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, CoercionReport]]:
    out = pd.DataFrame(index=frame.index)
    reports: dict[str, CoercionReport] = {}
    for column in frame.columns:
        out[column], reports[column] = coerce_column(frame[column])
    return out, reports
