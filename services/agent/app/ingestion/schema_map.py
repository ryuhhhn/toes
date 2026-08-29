"""Columns -> canonical roles: id, title, price, stock, image, text[].

Names alone are not enough — a column called "code" is often not the identifier, and a
column called "cost" is sometimes the supplier's cost rather than the retail price. So a
name match must be corroborated by the shape of the data before a role is assigned, and
every assignment carries a confidence the approval screen can question.

The synonym tables are commerce vocabulary (sku, price, stock), not category knowledge.
Nothing here knows what is being sold.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from rapidfuzz import fuzz

from app.models.ingestion import ColumnKind, ColumnProfile, RoleConfidence
from app.models.profile import Roles

log = logging.getLogger(__name__)

SYNONYMS: dict[str, tuple[str, ...]] = {
    "id": (
        "id", "sku", "code", "item code", "item id", "item number", "product id",
        "product code", "article number", "article code", "part number", "mpn", "ean",
        "upc", "gtin", "barcode", "reference", "ref", "identifier", "product number",
    ),
    "title": (
        "name", "title", "product name", "product title", "item name", "product",
        "item", "model", "model name", "display name", "label",
    ),
    "price": (
        "price", "retail price", "unit price", "sale price", "list price", "rrp",
        "msrp", "amount", "cost", "price incl vat", "price ex vat", "selling price",
        "our price", "value",
    ),
    "stock": (
        "stock", "qty", "quantity", "inventory", "stock level", "units in stock",
        "qty on hand", "quantity on hand", "on hand", "available", "availability",
        "in stock", "stock count", "units",
    ),
    "image": (
        "image", "image url", "image link", "img", "img url", "photo", "picture",
        "thumbnail", "thumb", "media", "image src", "picture url",
    ),
    "text": (
        "description", "details", "detail", "notes", "note", "summary", "about",
        "features", "overview", "long description", "short description", "copy",
        "spec notes", "specification",
    ),
}

#: Roles are claimed in this order so the most structurally distinctive win first.
ROLE_ORDER = ("id", "price", "stock", "image", "title", "text")

#: Below this a role is left unset and raised as a question rather than guessed at.
ACCEPT_THRESHOLD = 0.45
FUZZY_THRESHOLD = 85

NAME_WEIGHT = 0.55
SHAPE_WEIGHT = 0.45

MAX_TEXT_COLUMNS = 3

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_name(name: str) -> str:
    return _NON_ALNUM.sub(" ", str(name).casefold()).strip()


def name_score(column: str, synonyms: Iterable[str]) -> float:
    normalised = normalise_name(column)
    if not normalised:
        return 0.0

    tokens = set(normalised.split())
    best = 0.0

    for synonym in synonyms:
        if normalised == synonym:
            return 1.0
        synonym_tokens = set(synonym.split())
        if synonym_tokens and synonym_tokens <= tokens:
            best = max(best, 0.9)
            continue
        ratio = fuzz.token_set_ratio(normalised, synonym)
        if ratio >= FUZZY_THRESHOLD:
            best = max(best, (ratio / 100.0) * 0.8)

    return best


def _is_integral(profile: ColumnProfile) -> bool:
    return all(float(v).is_integer() for v in _numeric_samples(profile))


def _numeric_samples(profile: ColumnProfile) -> list[float]:
    out: list[float] = []
    for value in profile.samples:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            continue
    return out


def shape_score(role: str, profile: ColumnProfile) -> float:
    """How much the data itself supports this role. Zero is disqualifying."""
    kind = profile.kind

    if kind is ColumnKind.UNUSABLE:
        return 0.0

    if role == "id":
        if kind is ColumnKind.IDENTIFIER:
            return 1.0
        if profile.cardinality_ratio > 0.95 and profile.null_rate < 0.05:
            return 0.7
        return 0.0

    if role == "price":
        if profile.coercion.applied == "currency":
            return 1.0
        if kind is ColumnKind.NUMERIC and (profile.numeric_min or 0) >= 0:
            # Plausible but unproven: no currency marker was found in the data.
            return 0.55
        return 0.0

    if role == "stock":
        if kind is ColumnKind.BOOLEAN:
            return 0.7
        if kind is ColumnKind.NUMERIC and (profile.numeric_min or 0) >= 0:
            return 1.0 if _is_integral(profile) else 0.6
        return 0.0

    if role == "image":
        return 1.0 if kind is ColumnKind.URL else 0.0

    if role == "title":
        if kind in (ColumnKind.NUMERIC, ColumnKind.BOOLEAN, ColumnKind.URL):
            return 0.0
        if profile.cardinality_ratio > 0.7 and 1 <= profile.mean_token_len <= 10:
            return 1.0
        if profile.cardinality_ratio > 0.4:
            return 0.5
        return 0.0

    if role == "text":
        if kind is ColumnKind.FREE_TEXT:
            return 1.0
        if kind is ColumnKind.CATEGORICAL_HIGH_CARD and profile.mean_token_len >= 4:
            return 0.6
        return 0.0

    return 0.0


def _combined(role: str, profile: ColumnProfile) -> tuple[float, float, float]:
    names = name_score(profile.name, SYNONYMS[role])
    shape = shape_score(role, profile)
    if shape == 0.0:
        return 0.0, names, shape
    return names * NAME_WEIGHT + shape * SHAPE_WEIGHT, names, shape


def _reason(column: str, names: float, shape: float) -> str:
    return f"{column!r}: name match {names:.2f}, data shape {shape:.2f}"


def map_roles(
    profiles: list[ColumnProfile], overrides: dict[str, str] | None = None
) -> Roles:
    """Assign roles greedily, most structurally distinctive first, never reusing a column."""
    roles = Roles()
    overrides = overrides or {}
    claimed: set[str] = set()
    by_name = {p.name: p for p in profiles}

    # Merchant corrections win outright, and are recorded as such.
    for role, column in overrides.items():
        if role in ROLE_ORDER and column in by_name:
            if role == "text":
                roles.text = [column]
            else:
                setattr(roles, role, column)
            claimed.add(column)
            roles.confidence[role] = RoleConfidence(
                column=column, confidence=1.0, reason="set by the merchant"
            )

    for role in ROLE_ORDER:
        if role in overrides:
            continue

        scored = [
            (_combined(role, profile), profile)
            for profile in profiles
            if profile.name not in claimed
        ]
        scored = [((total, names, shape), p) for (total, names, shape), p in scored if total > 0]
        scored.sort(key=lambda item: item[0][0], reverse=True)

        if role == "text":
            chosen = [
                (score, profile)
                for score, profile in scored
                if score[0] >= ACCEPT_THRESHOLD
            ][:MAX_TEXT_COLUMNS]
            if chosen:
                roles.text = [profile.name for _score, profile in chosen]
                claimed.update(roles.text)
                best = chosen[0]
                roles.confidence["text"] = RoleConfidence(
                    column=roles.text[0],
                    confidence=round(best[0][0], 3),
                    reason=_reason(best[1].name, best[0][1], best[0][2]),
                )
            else:
                roles.confidence["text"] = RoleConfidence(
                    confidence=0.0, reason="no descriptive text column found"
                )
            continue

        if scored and scored[0][0][0] >= ACCEPT_THRESHOLD:
            (total, names, shape), profile = scored[0]
            setattr(roles, role, profile.name)
            claimed.add(profile.name)
            roles.confidence[role] = RoleConfidence(
                column=profile.name,
                confidence=round(total, 3),
                reason=_reason(profile.name, names, shape),
            )
        else:
            near = scored[0] if scored else None
            roles.confidence[role] = RoleConfidence(
                confidence=round(near[0][0], 3) if near else 0.0,
                reason=(
                    f"no confident match; closest was {near[1].name!r}"
                    if near
                    else "no candidate column"
                ),
            )

    return roles


def low_confidence_roles(roles: Roles, threshold: float = 0.7) -> list[str]:
    """Roles the approval screen should ask about instead of presenting as settled."""
    return [
        role
        for role, confidence in roles.confidence.items()
        if confidence.confidence < threshold
    ]
