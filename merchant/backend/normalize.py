"""
CSV ingestion & normalization for general product catalogs.

Minimum requirements for a valid upload:
- id
- title / product name
- price

Optional fields are handled gracefully:
- stock missing/invalid => default to 1 and warn the merchant
- image_url missing => warn but do not block the upload
- category missing => use a generic fallback or optional LLM inference hook
"""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from taxonomy import LLMInferenceClient, ProductTaxonomy

# Global product taxonomy for category inference
_default_taxonomy = ProductTaxonomy()


REQUIRED_COLUMNS = ["id", "title", "price"]
OPTIONAL_COLUMNS = ["description", "category", "attributes", "image_url", "stock"]
ALL_SCHEMA_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

COLUMN_ALIASES = {
    "id": "id",
    "sku": "id",
    "product_id": "id",
    "product_code": "id",
    "item_id": "id",
    "item_code": "id",
    "product_name": "title",
    "product_title": "title",
    "name": "title",
    "item_name": "title",
    "title": "title",
    "description": "description",
    "product_description": "description",
    "price": "price",
    "cost": "price",
    "unit_price": "price",
    "category": "category",
    "product_category": "category",
    "type": "category",
    "stock": "stock",
    "qty": "stock",
    "quantity": "stock",
    "stock_quantity": "stock",
    "inventory": "stock",
    "image": "image_url",
    "image_url": "image_url",
    "img": "image_url",
    "thumbnail": "image_url",
    "thumbnail_url": "image_url",
    "photo": "image_url",
    "photo_url": "image_url",
}


@dataclass
class RowIssue:
    row_index: int
    reason: str


@dataclass
class NormalizationReport:
    merchant_id: str
    rows_in: int = 0
    rows_out: int = 0
    missing_required_columns: list[str] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    aliased_columns: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    skipped_rows: list[RowIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.missing_required_columns) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchant_id": self.merchant_id,
            "ok": self.ok,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_skipped": len(self.skipped_rows),
            "missing_required_columns": self.missing_required_columns,
            "unmapped_columns": self.unmapped_columns,
            "aliased_columns": self.aliased_columns,
            "warnings": self.warnings,
            "skipped_row_details": [
                {"row_index": i.row_index, "reason": i.reason} for i in self.skipped_rows
            ],
        }


def _normalize_header(col: str) -> str:
    s = re.sub(r"\s*\([^)]*\)", "", col.strip())
    s = s.strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    return s.strip("_")


def _coerce_price(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        s = str(value).strip().replace("$", "").replace(",", "")
        if s == "":
            return None
        return round(float(s), 2)
    except (ValueError, TypeError):
        return None


def _coerce_stock(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    s = str(value).strip().lower()
    if s in ("true", "yes", "y", "in stock", "in_stock"):
        return 1
    if s in ("false", "no", "n", "out of stock", "out_of_stock", ""):
        return 0
    try:
        n = int(float(s))
        return max(n, 0)
    except (ValueError, TypeError):
        return None


def _coerce_attributes(value: Any, extra: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if value is not None and not (isinstance(value, float) and math.isnan(value)):
        raw = str(value).strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    result.update(parsed)
                else:
                    result["attributes_raw"] = raw
            except json.JSONDecodeError:
                result["attributes_raw"] = raw
    for k, v in extra.items():
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        result[k] = v
    return result


def _pick_first(row: pd.Series, candidates: list[str]) -> str | None:
    for key in candidates:
        value = row.get(key)
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _add_warning(report: NormalizationReport, message: str) -> None:
    if message not in report.warnings:
        report.warnings.append(message)


def infer_product_type(
    title: str,
    category: str | None = None,
    attributes: dict[str, Any] | None = None,
    llm_client: LLMInferenceClient | None = None,
    taxonomy: ProductTaxonomy | None = None,
    confidence_threshold: float = 0.5,
) -> str | None:
    """
    Infer product category with structured fallback chain.

    1. Use provided category if present
    2. Try LLM if available and confidence is low
    3. Fall back to taxonomy keyword matching
    4. Return "General" as last resort

    Args:
        title: product title
        category: pre-assigned category (takes priority)
        attributes: additional product attributes
        llm_client: optional LLM inference client
        taxonomy: optional custom taxonomy (defaults to built-in)
        confidence_threshold: minimum confidence for taxonomy match (0.0-1.0)

    Returns:
        Inferred or provided category name, or "General" as default.
    """
    # Step 1: Use provided category if available
    text = (category or "").strip()
    if text:
        return text

    # Prepare search haystack
    haystack = " ".join([
        str(title or "").lower(),
        *(str(v).lower() for v in (attributes or {}).values() if isinstance(v, (str, int, float)))
    ])

    # Step 2: Try taxonomy (keyword-based)
    tax = taxonomy or _default_taxonomy
    tax_result = tax.classify(haystack)
    if tax_result and tax_result.confidence >= confidence_threshold:
        return tax_result.category

    # Step 3: Try LLM if available
    if llm_client is not None:
        try:
            llm_result = llm_client.infer_product_type(title=title, attributes=attributes or {})
            if llm_result and llm_result.category:
                return str(llm_result.category).strip()
        except Exception:
            pass

    # Step 4: Fall back to taxonomy result even if below threshold
    if tax_result:
        return tax_result.category

    # Step 5: Default fallback
    return "General"


def normalize_csv(df: pd.DataFrame, merchant_id: str, llm_client: Any | None = None) -> tuple[list[dict[str, Any]], NormalizationReport]:
    report = NormalizationReport(merchant_id=merchant_id, rows_in=len(df))

    original_cols = list(df.columns)
    header_map: dict[str, str] = {}
    for col in original_cols:
        normalized = _normalize_header(str(col))
        mapped = COLUMN_ALIASES.get(normalized, normalized)
        header_map[col] = mapped
        if mapped != normalized:
            report.aliased_columns[col] = mapped
    df = df.rename(columns=header_map)
    df = df.loc[:, ~df.columns.duplicated()]

    missing_required = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_required:
        report.missing_required_columns = missing_required
        return [], report

    extra_cols = [c for c in df.columns if c not in ALL_SCHEMA_COLUMNS]
    report.unmapped_columns = extra_cols
    df = df.dropna(how="all").reset_index(drop=True)

    products: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        title = _pick_first(row, ["title", "name", "product_name", "product_title"])
        if not title:
            report.skipped_rows.append(RowIssue(idx, "missing product title"))
            continue

        price = _coerce_price(row.get("price"))
        if price is None:
            report.skipped_rows.append(RowIssue(idx, f"invalid price: {row.get('price')!r}"))
            continue

        product_id = _pick_first(row, ["id", "sku", "product_id", "product_code", "item_id", "item_code"])
        if not product_id:
            product_id = str(uuid.uuid4())

        stock = _coerce_stock(row.get("stock"))
        if stock is None:
            stock = 1
            _add_warning(
                report,
                "Stock column missing or invalid; defaulted to in-stock (1) so the catalog does not silently sell out-of-stock inventory. Please confirm on the approval screen."
            )

        image_url = _pick_first(row, ["image_url", "image", "img", "photo_url", "thumbnail_url"])
        if not image_url:
            _add_warning(
                report,
                "Image URL missing; product cards may render without imagery. Please review before approval."
            )

        category = _pick_first(row, ["category", "product_category", "type"])
        attributes = _coerce_attributes(row.get("attributes"), {c: row.get(c) for c in extra_cols})
        resolved_category = infer_product_type(title, category, attributes, llm_client)

        products.append({
            "id": str(product_id).strip(),
            "merchant_id": merchant_id,
            "title": title.strip(),
            "description": (str(row.get("description")).strip() if pd.notna(row.get("description")) else ""),
            "price": price,
            "category": resolved_category,
            "attributes": attributes,
            "image_url": image_url.strip() if image_url else None,
            "stock": stock,
        })

    report.rows_out = len(products)
    return products, report
