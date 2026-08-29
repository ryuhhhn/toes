"""The ingestion pipeline, composed.

Phases 1-3 are pure code and always run. Phases 4-5 use an LLM and are strictly additive:
if the model is unavailable, or returns something that fails validation, the deterministic
profile stands on its own. An ingest never fails because a provider is down.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from app.ingestion.bootstrap import MAX_CANONICAL_VALUES, bootstrap_profile
from app.ingestion.canonicalize import canonicalize_profile_with_llm
from app.ingestion.classify import classify_profile
from app.ingestion.coerce import coerce_frame
from app.ingestion.loader import LoadedTable, load_table
from app.ingestion.profiler import profile_frame
from app.ingestion.schema_map import low_confidence_roles, map_roles
from app.llm.factory import LLMUnavailable, get_llm
from app.models.ingestion import ColumnProfile
from app.models.profile import AgentProfile, SourceInfo

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    profile: AgentProfile
    frame: pd.DataFrame
    column_profiles: list[ColumnProfile]
    table: LoadedTable
    low_confidence: list[str] = field(default_factory=list)

    @property
    def rows(self) -> list[dict]:
        return frame_to_rows(self.frame)


def frame_to_rows(frame: pd.DataFrame) -> list[dict]:
    """JSON-safe records. Lists survive; NaN becomes None rather than a float that breaks JSON."""
    records: list[dict] = []
    for record in frame.to_dict(orient="records"):
        clean: dict = {}
        for key, value in record.items():
            if isinstance(value, (list, tuple, set)):
                clean[str(key)] = list(value)
            elif value is None or (not isinstance(value, str) and pd.isna(value)):
                clean[str(key)] = None
            else:
                clean[str(key)] = value
        records.append(clean)
    return records


def row_hash(rows: list[dict]) -> str:
    """Stable across row order and key order, so a reordered export is not a new catalog."""
    payload = sorted(
        json.dumps(row, sort_keys=True, default=str) for row in rows
    )
    digest = hashlib.sha256()
    for line in payload:
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()[:16]


def analyze_frame(
    frame: pd.DataFrame,
    *,
    merchant_id: str,
    table: LoadedTable,
    reports: dict | None = None,
    role_overrides: dict[str, str] | None = None,
    version: int = 1,
) -> IngestResult:
    column_profiles = profile_frame(frame, reports or {})
    roles = map_roles(column_profiles, role_overrides)

    rows = frame_to_rows(frame)
    source = SourceInfo(
        filename=table.filename,
        sheet=table.sheet,
        row_count=len(frame),
        column_count=len(frame.columns),
        row_hash=row_hash(rows),
    )

    profile = bootstrap_profile(
        column_profiles,
        roles,
        merchant_id,
        source=source,
        notes=table.notes,
        version=version,
    )

    return IngestResult(
        profile=profile,
        frame=frame,
        column_profiles=column_profiles,
        table=table,
        low_confidence=low_confidence_roles(roles),
    )


def analyze_file(
    source: bytes | str | Path,
    *,
    merchant_id: str,
    filename: str | None = None,
    sheet: str | None = None,
    role_overrides: dict[str, str] | None = None,
    version: int = 1,
) -> IngestResult:
    """Phases 1-3: load, coerce, profile, map roles, bootstrap. No LLM, never fails on one."""
    table = load_table(source, filename=filename, sheet=sheet)
    frame, reports = coerce_frame(table.df)
    return analyze_frame(
        frame,
        merchant_id=merchant_id,
        table=table,
        reports=reports,
        role_overrides=role_overrides,
        version=version,
    )


async def enrich_with_llm(result: IngestResult) -> IngestResult:
    """Phases 4-5: label clusters, then classify. Strictly additive and never fatal.

    If no provider is configured, or a call fails, or the output fails validation, the
    deterministic profile from phases 1-3 stands and the ingest still succeeds.
    """
    try:
        llm = get_llm()
    except LLMUnavailable as exc:
        log.info("no LLM configured; keeping the deterministic profile (%s)", exc)
        result.profile.notes.append(
            "info: category analysis was skipped because no language model is configured. "
            "Field names are shown as they appear in your file."
        )
        return result

    by_name = {p.name: p for p in result.column_profiles}

    for spec in result.profile.fields:
        column = by_name.get(spec.column)
        if column is None:
            continue
        values, aliases = await canonicalize_profile_with_llm(column, llm=llm)
        if values:
            spec.canonical_values = values[:MAX_CANONICAL_VALUES]
            spec.aliases = aliases

    titles = _sample_titles(result)
    result.profile = await classify_profile(
        result.profile, result.column_profiles, titles, llm=llm
    )
    return result


def _sample_titles(result: IngestResult, limit: int = 10) -> list[str]:
    title_column = result.profile.roles.title
    if not title_column or title_column not in result.frame.columns:
        return []
    return [str(v) for v in result.frame[title_column].dropna().head(limit).tolist()]


def analyze_rows(
    rows: list[dict], *, merchant_id: str, filename: str = "catalog", version: int = 1
) -> IngestResult:
    """Same pipeline for rows pulled from the Merchant Backend rather than a file upload."""
    frame = pd.DataFrame(rows, dtype=object)
    frame.columns = [str(c) for c in frame.columns]
    coerced, reports = coerce_frame(frame)
    table = LoadedTable(df=frame, filename=filename)
    table.note(f"read {len(frame)} rows x {len(frame.columns)} columns from the merchant API")
    return analyze_frame(
        coerced,
        merchant_id=merchant_id,
        table=table,
        reports=reports,
        version=version,
    )
