"""Statistical fingerprint of every column. No LLM, no domain knowledge.

The profiler is the source of truth the LLM is later checked against: a canonical value the
classifier proposes must trace back to something recorded here, or it is rejected. That
asymmetry — code enumerates, the model only interprets — is what stops invented attributes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pandas as pd

from app.ingestion.coerce import URL_RE
from app.models.ingestion import ColumnKind, CoercionReport, ColumnProfile

#: Beyond this a column carries no usable signal, whatever it is called.
UNUSABLE_NULL_RATE = 0.95

#: Enum thresholds from the column-kind heuristic in CLAUDE.md.
MAX_ENUM_DISTINCT = 25
MAX_ENUM_RATIO = 0.3

#: Matches the probe skip rule: above 12 choices a question stops being answerable,
#: so a column that small is an enum regardless of how few rows the catalog has.
SMALL_ENUM_DISTINCT = 12

#: Word counts above which a text column reads as prose rather than a label.
FREE_TEXT_TOKENS = 12
PROSE_TOKENS_WHEN_UNIQUE = 5

#: Identifiers are short. A multi-word all-unique column is a title, not an id.
MAX_IDENTIFIER_TOKENS = 2

MAX_VALUE_COUNTS = 50
MAX_SAMPLES = 5

NUMERIC_COERCIONS = {"currency", "numeric", "unit_numeric", "percentage"}


def _explode(series: pd.Series) -> list[str]:
    """Multi-valued cells are counted per element; counting combinations invents enums."""
    values: list[str] = []
    for value in series.dropna():
        if isinstance(value, (list, tuple, set)):
            values.extend(str(v).strip() for v in value if str(v).strip())
        else:
            values.append(str(value).strip())
    return values


def _looks_like_code(values: list[str]) -> bool:
    """Distinguishes an identifier from a merely near-unique label.

    Uniqueness is not enough: a "grade" or "variant" column can be almost all-distinct
    without being an id. Real identifiers are machine-shaped — they carry digits, or they
    are strikingly uniform in length.
    """
    if not values:
        return False

    with_digits = sum(1 for v in values if any(c.isdigit() for c in v)) / len(values)
    if with_digits >= 0.7:
        return True

    lengths = [len(v) for v in values]
    mean_length = sum(lengths) / len(lengths)
    if mean_length == 0:
        return False
    spread = (sum((n - mean_length) ** 2 for n in lengths) / len(lengths)) ** 0.5
    return (spread / mean_length) < 0.15


def _mean_token_len(values: list[str]) -> float:
    if not values:
        return 0.0
    return sum(len(v.split()) for v in values) / len(values)


def _decide_kind(
    *,
    report: CoercionReport,
    null_rate: float,
    distinct: int,
    ratio: float,
    non_null: int,
    values: list[str],
    mean_tokens: float,
) -> ColumnKind:
    if null_rate >= UNUSABLE_NULL_RATE or non_null == 0:
        return ColumnKind.UNUSABLE

    if report.applied == "boolean":
        return ColumnKind.BOOLEAN
    if report.applied == "list":
        return ColumnKind.CATEGORICAL_MULTI
    if report.applied in NUMERIC_COERCIONS:
        return ColumnKind.NUMERIC

    # Remaining columns are text; decide what kind of text.
    # URL and prose are checked before identifier: an image column and a description
    # column are both all-unique, so uniqueness alone would classify them as ids.
    if values and sum(1 for v in values if URL_RE.match(v)) / len(values) >= 0.8:
        return ColumnKind.URL

    if mean_tokens > FREE_TEXT_TOKENS or (
        ratio > 0.9 and mean_tokens >= PROSE_TOKENS_WHEN_UNIQUE
    ):
        return ColumnKind.FREE_TEXT

    if (
        ratio > 0.95
        and null_rate < 0.05
        and mean_tokens <= MAX_IDENTIFIER_TOKENS
        and _looks_like_code(values)
    ):
        return ColumnKind.IDENTIFIER

    # The ratio test is a proxy for "values repeat enough to be a set of choices". On a
    # small catalog it is inflated by row count alone, so a low absolute distinct count
    # is accepted as independent evidence — otherwise short catalogs lose every enum,
    # and with it everything worth asking the shopper about.
    if distinct <= MAX_ENUM_DISTINCT and (
        ratio < MAX_ENUM_RATIO or distinct <= SMALL_ENUM_DISTINCT
    ):
        return ColumnKind.CATEGORICAL_ENUM

    return ColumnKind.CATEGORICAL_HIGH_CARD


def profile_column(
    series: pd.Series, *, name: str, report: CoercionReport | None = None
) -> ColumnProfile:
    report = report or CoercionReport()
    total = len(series)
    non_null_series = series.dropna()
    non_null = len(non_null_series)
    null_rate = 1.0 - (non_null / total) if total else 1.0

    exploded = _explode(series)
    counter = Counter(exploded)
    distinct = len(counter)
    ratio = distinct / non_null if non_null else 0.0
    mean_tokens = _mean_token_len(exploded)

    kind = _decide_kind(
        report=report,
        null_rate=null_rate,
        distinct=distinct,
        ratio=ratio,
        non_null=non_null,
        values=exploded,
        mean_tokens=mean_tokens,
    )

    numeric_min = numeric_max = None
    if kind is ColumnKind.NUMERIC and non_null:
        numbers = pd.to_numeric(non_null_series, errors="coerce").dropna()
        if len(numbers):
            numeric_min, numeric_max = float(numbers.min()), float(numbers.max())

    samples: list[Any] = []
    for value in non_null_series.tolist():
        if value not in samples:
            samples.append(value)
        if len(samples) >= MAX_SAMPLES:
            break

    return ColumnProfile(
        name=name,
        raw_dtype=str(series.dtype),
        kind=kind,
        null_rate=round(null_rate, 4),
        distinct_count=distinct,
        cardinality_ratio=round(ratio, 4),
        samples=samples,
        value_counts=dict(counter.most_common(MAX_VALUE_COUNTS)),
        numeric_min=numeric_min,
        numeric_max=numeric_max,
        mean_token_len=round(mean_tokens, 2),
        coercion=report,
    )


def profile_frame(
    frame: pd.DataFrame, reports: dict[str, CoercionReport] | None = None
) -> list[ColumnProfile]:
    reports = reports or {}
    return [
        profile_column(frame[column], name=str(column), report=reports.get(str(column)))
        for column in frame.columns
    ]
