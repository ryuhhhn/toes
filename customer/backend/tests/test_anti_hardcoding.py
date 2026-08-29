"""Stage I3 — the anti-hardcoding guard.

Crude on purpose, and the only thing that reliably stops niche leakage at 2am the night
before a demo. It greps app/ for vocabulary drawn from the fixture catalogs. If any of it
appears in the code, some category knowledge has escaped the Agent Profile, which is
exactly the failure the whole design exists to prevent.

Hard rule from CLAUDE.md: no file under app/ may reference a domain-specific column name,
attribute, value, or piece of category knowledge. Not in constants, not in prompts, not in
fallbacks, not in tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

#: Vocabulary from fixtures/catalogs/. Product nouns, brand names, category attributes and
#: the fixtures' own attribute column names. Deliberately drawn from both catalogs, so
#: tuning to either one trips it.
#:
#: Role vocabulary is deliberately absent. "item code", "retail price", "units in stock"
#: and "image link" are names for the id/price/stock/image *roles*, which every catalogue
#: has and which schema_map.py must recognise. Those are commerce vocabulary. An attribute
#: name is category knowledge, and that is what may never appear.
BANNED_VOCABULARY = [
    # Catalog A: product types and brands
    "dewalt", "makita", "milwaukee", "ryobi", "metabo", "einhell", "hilti",
    "drill", "jigsaw", "sander", "grinder", "circular saw", "mitre", "nailer",
    "chuck", "cordless", "corded", "voltage", "torque", "rpm",
    # Catalog A: column names
    "qty_on_hand", "price_usd", "tool_type", "power_source", "battery_included",
    "chuck_size", "warranty_years", "product_name", "image_url",
    # Catalog B: product types and attributes
    "assam", "darjeeling", "sencha", "gyokuro", "oolong", "puerh", "rooibos",
    "honeybush", "chamomile", "jasmine", "matcha", "genmaicha", "hojicha",
    "bergamot", "caffeine", "tisane", "infusion", "steep", "flush",
    # Catalog B: attribute column names
    "origin country", "leaf grade", "flavour notes", "certifications",
    "caffeine level", "net weight", "tasting description",
    # Merchant ids
    "power_tools", "tea_and_infusions",
    # Categories named as illustrative examples in the design docs — using either as a
    # target would be the same mistake as tuning to a fixture.
    "laptop", "skincare", "eyewear", "spectacle", "sunglass",
]

#: Words that are legitimately generic despite reading as domain-ish. Kept explicit so the
#: exemption is a decision on the record rather than a silently missing entry.
ALLOWED_SUBSTRINGS = {
    "tool",  # "tool call", "tool registry" — agent vocabulary, not product vocabulary
    "toolu",  # Anthropic tool-use id prefix
}

SKIP_DIRS = {"__pycache__"}


def _python_files() -> list[Path]:
    return [
        path
        for path in APP_DIR.rglob("*.py")
        if not any(part in SKIP_DIRS for part in path.parts)
    ]


def test_app_directory_is_actually_being_scanned():
    files = _python_files()
    assert len(files) > 20, "the guard is not looking at the code it is meant to protect"


@pytest.mark.parametrize("term", BANNED_VOCABULARY)
def test_no_fixture_vocabulary_appears_in_app_code(term):
    pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    offenders: list[str] = []

    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            if any(allowed in line.lower() for allowed in ALLOWED_SUBSTRINGS):
                # Only exempt when the allowed word is what actually matched.
                if not pattern.search(re.sub(r"tool\w*", "", line, flags=re.IGNORECASE)):
                    continue
            offenders.append(f"{path.relative_to(APP_DIR.parent)}:{number}: {line.strip()}")

    assert not offenders, (
        f"category-specific vocabulary {term!r} leaked into app/:\n" + "\n".join(offenders)
    )


def test_no_module_branches_on_a_category_name():
    """`if category == ...` is the shape the design forbids outright."""
    pattern = re.compile(r"(if|elif)\s+.*\bcategory\b\s*(==|!=|\bin\b)")
    offenders = []

    for path in _python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(APP_DIR.parent)}:{number}: {line.strip()}")

    assert not offenders, "code branches on a category name:\n" + "\n".join(offenders)


def test_no_per_niche_configuration_files_exist():
    """A per-niche YAML is the other way category knowledge escapes the profile."""
    config_files = [
        p
        for p in APP_DIR.rglob("*")
        if p.suffix.lower() in {".yaml", ".yml", ".json", ".toml"} and p.is_file()
    ]
    assert not config_files, (
        "category configuration must be derived at ingest, not shipped as files: "
        f"{[str(p) for p in config_files]}"
    )


def test_the_only_hardcoded_category_word_is_a_meaningless_placeholder():
    """bootstrap's fallback category must carry no meaning at all."""
    from app.ingestion.bootstrap import DEFAULT_CATEGORY

    assert DEFAULT_CATEGORY == "products"
