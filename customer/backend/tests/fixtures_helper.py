"""Fixture discovery.

Every generic test is parameterized over CATALOGS. Adding a spreadsheet to
fixtures/catalogs/ adds it to the whole suite — which is the point: the product claim is
that an unseen catalog needs no code change, so proving it should need no code change either.
"""

from __future__ import annotations

from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "catalogs"

SUPPORTED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xls"}

CATALOGS = sorted(
    p.name for p in FIXTURE_DIR.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES
)


def catalog_path(name: str) -> Path:
    return FIXTURE_DIR / name


def merchant_id_for(name: str) -> str:
    return Path(name).stem
