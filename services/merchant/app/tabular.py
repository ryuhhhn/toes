"""Reading an uploaded sheet into a DataFrame.

Merchants keep catalogs in spreadsheets. A service that only speaks CSV makes "export to
CSV first" a precondition for using the product at all, and the row that survives that
export is not always the row the merchant looked at.

Parsing is chosen by extension, then falls back to CSV, so a mislabelled file gets a
chance rather than a 400. Everything is read with `dtype=str` — the merchant stores raw
rows and serves them untouched (docs/CONTRACTS.md §0), and pandas' type inference is
exactly the coercion that rule forbids: it would turn a "01234" SKU into 1234 and a
"£129.00" price into a string pandas had already given up on.
"""

from __future__ import annotations

from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd

#: Extensions we will try a specific reader for. Anything else is attempted as CSV.
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}
LEGACY_EXCEL_SUFFIXES = {".xls"}
DELIMITED_SUFFIXES = {".csv", ".tsv", ".txt"}

SUPPORTED_SUFFIXES = EXCEL_SUFFIXES | LEGACY_EXCEL_SUFFIXES | DELIMITED_SUFFIXES

#: What the console advertises in its file picker.
ACCEPT_ATTRIBUTE = ",".join(sorted(SUPPORTED_SUFFIXES))


class UnreadableUpload(ValueError):
    """The file could not be read as a table by any reader we have."""


def _read_delimited(raw: bytes, suffix: str) -> pd.DataFrame:
    # utf-8-sig strips the BOM Excel writes on "CSV UTF-8", which otherwise turns the
    # first column's name into "\ufeffsku" and quietly breaks every id lookup.
    text = raw.decode("utf-8-sig", errors="replace")
    return pd.read_csv(StringIO(text), sep="\t" if suffix == ".tsv" else ",", dtype=str)


def _read_excel(raw: bytes, engine: str | None) -> pd.DataFrame:
    # sheet_name=0: the first sheet only. Merging every sheet of a workbook would be a
    # guess about what the merchant meant, and this service does not guess.
    return pd.read_excel(BytesIO(raw), sheet_name=0, dtype=str, engine=engine)


def read_upload(raw: bytes, filename: str | None) -> pd.DataFrame:
    """Parse an uploaded catalog. Raises UnreadableUpload with every reader's complaint."""
    suffix = Path(filename or "").suffix.lower()
    attempts: list[tuple[str, callable]] = []

    # A NUL byte means this is not text, and the delimited reader must not be offered it.
    # Given binary rubbish pandas cheerfully returns a one-column, zero-row frame whose
    # header is the rubbish — a "successful" parse that would replace the merchant's
    # catalog with nothing. A CSV never contains a NUL; every binary format does.
    binary = b"\x00" in raw

    if suffix in EXCEL_SUFFIXES:
        attempts.append(("excel", lambda: _read_excel(raw, "openpyxl")))
    elif suffix in LEGACY_EXCEL_SUFFIXES:
        # .xls needs xlrd, which we do not ship: pandas dropped .xls support from
        # openpyxl entirely. Try anyway — an .xls that is really a CSV is common — and
        # let the fallback catch it.
        attempts.append(("excel", lambda: _read_excel(raw, None)))
    elif not binary:
        attempts.append(("delimited", lambda: _read_delimited(raw, suffix)))
        # A spreadsheet saved with the wrong extension is common enough to be worth one
        # more try before refusing the upload.
        attempts.append(("excel", lambda: _read_excel(raw, None)))
    else:
        attempts.append(("excel", lambda: _read_excel(raw, None)))

    # A CSV renamed .xlsx is just as common as the reverse.
    if suffix not in DELIMITED_SUFFIXES and not binary:
        attempts.append(("delimited", lambda: _read_delimited(raw, suffix)))

    problems: list[str] = []
    for label, reader in attempts:
        try:
            df = reader()
        except Exception as exc:  # noqa: BLE001 - every reader's failure is reportable
            problems.append(f"{label}: {exc}")
            continue
        if df.shape[1] == 0:
            problems.append(f"{label}: the file has no columns")
            continue
        return df

    raise UnreadableUpload("; ".join(problems) or "no reader could parse this file")
