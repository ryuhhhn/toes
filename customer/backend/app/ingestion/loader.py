"""FILE -> DataFrame.

Format is decided by content first and extension second, because uploads are routinely
misnamed. Everything the loader decides is recorded as a LoadNote so the merchant approval
screen can show what we did rather than silently guessing.

Nothing here knows what is being sold.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.models.ingestion import LoadNote

log = logging.getLogger(__name__)

#: Tried in order. utf-8-sig first would mask a real utf-8 file; utf-8 first is safe
#: because a BOM makes strict utf-8 decoding succeed with a stray ﻿ we then strip.
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

TEXT_EXTENSIONS = {".csv", ".tsv", ".txt", ".psv", ".dat"}
EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}

_ZIP_MAGIC = b"PK\x03\x04"  # .xlsx is a zip archive
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # legacy .xls

#: How far down the file to look for the real header row.
HEADER_SCAN_ROWS = 12

_WHITESPACE = re.compile(r"\s+")


@dataclass
class LoadedTable:
    df: pd.DataFrame
    filename: str
    sheet: str | None = None
    detected_encoding: str | None = None
    detected_delimiter: str | None = None
    header_row: int = 0
    notes: list[LoadNote] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.df)

    def note(self, message: str, level: str = "info") -> None:
        self.notes.append(LoadNote(level=level, message=message))


class UnreadableFile(ValueError):
    """The bytes are not a table we can read. Surfaced to the merchant, never swallowed."""


# --- format detection --------------------------------------------------------


def detect_format(data: bytes, filename: str) -> str:
    """Content wins over extension: a .csv that is really an .xlsx is a common upload."""
    if data.startswith(_ZIP_MAGIC):
        return "excel"
    if data.startswith(_OLE_MAGIC):
        return "excel_legacy"

    suffix = Path(filename).suffix.lower()
    if suffix in EXCEL_EXTENSIONS:
        # Extension says Excel but the magic bytes disagree — trust the bytes and try text.
        return "text"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    return "text"


def decode_text(data: bytes) -> tuple[str, str]:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # latin-1 cannot fail, so reaching here means the ladder was misconfigured.
    return data.decode("latin-1", errors="replace"), "latin-1"


def sniff_delimiter(text: str) -> str:
    """csv.Sniffer with a comma fallback, biased by counting on the first few lines.

    Sniffer is confidently wrong on files with commas inside quoted prose, so we only
    accept its answer when it names a delimiter we also see repeatedly.
    """
    sample = "\n".join(text.splitlines()[:20])
    if not sample.strip():
        return ","

    candidates = [",", "\t", ";", "|"]
    counts = {d: sample.count(d) for d in candidates}

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="".join(candidates))
        sniffed = dialect.delimiter
        if counts.get(sniffed, 0) > 0:
            return sniffed
    except csv.Error:
        pass

    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


# --- header detection --------------------------------------------------------


def _stringify(value: object) -> str | None:
    """One string form per cell, or None for genuinely empty.

    Excel hands back real Python numbers. Naively stringifying them turns an integer SKU
    into "1001.0", which then fails to join against the merchant's own id.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if pd.isna(value):
            return None
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    return text or None


def _looks_numeric(value: object) -> bool:
    text = str(value).strip().replace(",", "").replace("$", "").replace("€", "")
    if not text:
        return False
    try:
        float(text)
        return True
    except ValueError:
        return False


def _header_score(row: pd.Series) -> float:
    """Header rows are dense, unique and textual. Data rows usually fail one of the three."""
    values = [v for v in row.tolist() if v is not None and str(v).strip() != ""]
    if len(values) < 2:
        return 0.0

    width = max(len(row), 1)
    density = len(values) / width
    uniqueness = len(({str(v).strip().casefold() for v in values})) / len(values)
    textual = sum(0 if _looks_numeric(v) else 1 for v in values) / len(values)

    if density < 0.5 or uniqueness < 0.85 or textual < 0.6:
        return 0.0
    return density * uniqueness * textual


def detect_header_row(raw: pd.DataFrame) -> int:
    """Index of the most header-like row in the first HEADER_SCAN_ROWS.

    Real exports carry title banners, contact details and blank spacers above the header.
    """
    best_index, best_score = 0, 0.0
    for index in range(min(HEADER_SCAN_ROWS, len(raw))):
        score = _header_score(raw.iloc[index])
        if score > best_score:
            best_index, best_score = index, score
        # A perfect-looking first row is the common case; stop early rather than let a
        # later, equally dense row win on a rounding difference.
        if best_score >= 0.99:
            break
    return best_index


# --- column hygiene ----------------------------------------------------------


def clean_column_names(columns: list[object]) -> tuple[list[str], list[str]]:
    """Trim, collapse whitespace, name the unnamed, and dedupe collisions.

    Returns the cleaned names and a list of human-readable notes about what changed.
    """
    cleaned: list[str] = []
    notes: list[str] = []
    seen: dict[str, int] = {}

    for position, raw in enumerate(columns, start=1):
        # pandas represents "missing" differently per dtype (None, np.nan, pd.NA), and a
        # header cell that arrives as NaN must not become a column literally called "nan".
        blank = raw is None or (not isinstance(raw, (list, tuple, set)) and pd.isna(raw))
        name = "" if blank else str(raw)
        name = name.replace("﻿", "")
        name = _WHITESPACE.sub(" ", name).strip()

        if not name or name.lower().startswith("unnamed:"):
            name = f"column_{position}"
            notes.append(f"column {position} had no header; named {name!r}")

        key = name.casefold()
        if key in seen:
            seen[key] += 1
            deduped = f"{name}_{seen[key]}"
            notes.append(f"duplicate column {name!r} renamed to {deduped!r}")
            name = deduped
        else:
            seen[key] = 1

        cleaned.append(name)

    return cleaned, notes


def _frame_from_raw(raw: pd.DataFrame, header_row: int) -> tuple[pd.DataFrame, list[str]]:
    header = raw.iloc[header_row].tolist()
    body = raw.iloc[header_row + 1 :].reset_index(drop=True)
    names, notes = clean_column_names(header)
    body.columns = names
    return body, notes


# --- readers -----------------------------------------------------------------


def _read_text(data: bytes, table: LoadedTable) -> pd.DataFrame:
    text, encoding = decode_text(data)
    table.detected_encoding = encoding
    if encoding not in ("utf-8", "utf-8-sig"):
        table.note(f"decoded as {encoding}; non-ASCII characters may be approximate", "warning")

    delimiter = sniff_delimiter(text)
    table.detected_delimiter = delimiter

    raw = pd.read_csv(
        io.StringIO(text),
        sep=delimiter,
        header=None,
        dtype=str,
        keep_default_na=False,
        na_values=[],
        skip_blank_lines=False,
        engine="python",
        on_bad_lines="skip",
    )
    return raw


def _read_excel(data: bytes, table: LoadedTable, sheet: str | None, legacy: bool) -> pd.DataFrame:
    engine = "xlrd" if legacy else "openpyxl"
    try:
        # keep_default_na=False matters: pandas' default NA vocabulary includes "None",
        # which is a legitimate value in plenty of real enums. Missingness is decided in
        # coerce.py against one explicit token list, not by two layers disagreeing.
        # dtype=object rather than str: with keep_default_na=False, dtype=str stringifies
        # real empty cells into the literal "nan". Stringifying is done in _stringify,
        # which also stops an integer SKU arriving as "1001.0".
        book = pd.read_excel(
            io.BytesIO(data),
            sheet_name=None,
            header=None,
            dtype=object,
            engine=engine,
            keep_default_na=False,
            na_values=[],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the merchant as an upload error
        raise UnreadableFile(f"could not read workbook: {exc}") from exc

    if not book:
        raise UnreadableFile("workbook contains no sheets")

    if sheet is not None:
        if sheet not in book:
            raise UnreadableFile(f"sheet {sheet!r} not found; available: {list(book)}")
        chosen = sheet
    elif len(book) == 1:
        chosen = next(iter(book))
    else:
        # Most data rows wins; the merchant can override with ?sheet=.
        chosen = max(book, key=lambda name: len(book[name].dropna(how="all")))
        others = [name for name in book if name != chosen]
        table.note(
            f"workbook has {len(book)} sheets; read {chosen!r} (most rows). "
            f"Others available: {others}",
            "warning",
        )

    table.sheet = chosen
    return book[chosen]


# --- entry point -------------------------------------------------------------


def load_table(
    source: bytes | str | Path,
    *,
    filename: str | None = None,
    sheet: str | None = None,
) -> LoadedTable:
    """Read any supported spreadsheet into a clean, string-typed DataFrame.

    Values stay as strings: coercion is a separate, reportable phase, and letting pandas
    guess types here would hide exactly the decisions the merchant needs to see.
    """
    if isinstance(source, (str, Path)):
        path = Path(source)
        data = path.read_bytes()
        filename = filename or path.name
    else:
        data = source
        filename = filename or "upload"

    if not data:
        raise UnreadableFile("file is empty")

    table = LoadedTable(df=pd.DataFrame(), filename=filename)
    fmt = detect_format(data, filename)

    if fmt in ("excel", "excel_legacy"):
        raw = _read_excel(data, table, sheet, legacy=(fmt == "excel_legacy"))
    else:
        raw = _read_text(data, table)

    if raw.empty:
        raise UnreadableFile("file contains no rows")

    # Blank-out whitespace-only cells before header detection so spacer rows score zero.
    raw = raw.map(_stringify)

    table.header_row = detect_header_row(raw)
    if table.header_row > 0:
        table.note(
            f"skipped {table.header_row} row(s) above the header row", "warning"
        )

    frame, name_notes = _frame_from_raw(raw, table.header_row)
    for message in name_notes:
        table.note(message)

    before_rows = len(frame)
    frame = frame.dropna(how="all").reset_index(drop=True)
    if before_rows != len(frame):
        table.note(f"dropped {before_rows - len(frame)} fully empty row(s)")

    before_cols = list(frame.columns)
    frame = frame.dropna(axis=1, how="all")
    dropped = [c for c in before_cols if c not in frame.columns]
    if dropped:
        table.note(f"dropped fully empty column(s): {dropped}", "warning")

    if frame.empty:
        raise UnreadableFile("no data rows found below the header")

    table.df = frame
    table.note(f"read {len(frame)} rows x {len(frame.columns)} columns")
    return table
