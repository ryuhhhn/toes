"""
Storage seam for the Merchant Backend.

Per spec 2.2, the real implementation should be a single Postgres instance,
multi-tenant via merchant_id on every row. This module supports both:
- SQLAlchemy/Postgres when MERCHANT_DATABASE_URL (or DATABASE_URL) is set
- in-memory fallback for local demo/test usage when neither is set

The public function signatures stay stable so the API layer does not need to
change when the backing store is swapped.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.db.database import (
    Category,
    Merchant,
    Product,
    RawRows,
    Session,
    create_schema,
    get_engine,
)

# merchant_id -> {product_id -> product dict}
_DB: dict[str, dict[str, dict[str, Any]]] = {}


def _ensure_merchant(session: Any, merchant_id: str) -> Merchant:
    merchant = session.get(Merchant, merchant_id)
    if merchant is not None:
        return merchant

    merchant = Merchant(id=merchant_id, created_at="now")
    merged = session.merge(merchant)
    session.flush()
    return merged


def _use_sql() -> bool:
    """Resolved from the lazily-built engine, not from os.environ.

    The old version re-read DATABASE_URL at call time but still required an `engine`
    decided at import, so setting the variable late gave in-memory mode silently.
    """
    return get_engine() is not None


def replace_catalog(merchant_id: str, products: list[dict[str, Any]]) -> None:
    """Upload semantics: a fresh CSV upload replaces this merchant's catalog."""
    if _use_sql():
        create_schema()
        with Session(get_engine()) as session:
            session.query(Product).filter(Product.merchant_id == merchant_id).delete()
            _ensure_merchant(session, merchant_id)

            seen_categories: set[str] = set()
            for product in products:
                category_name = (product.get("category") or "").strip()
                if category_name and category_name not in seen_categories:
                    upsert_category(merchant_id, category_name)
                    seen_categories.add(category_name)

                row = Product(
                    id=product["id"],
                    merchant_id=merchant_id,
                    title=product.get("title", ""),
                    description=product.get("description", ""),
                    price=float(product.get("price", 0)),
                    category=product.get("category"),
                    attributes=product.get("attributes", {}) or {},
                    image_url=product.get("image_url"),
                    stock=int(product.get("stock", 0)),
                )
                session.add(row)
            session.commit()
        return

    _DB[merchant_id] = {p["id"]: p for p in products}


def get_catalog(merchant_id: str) -> list[dict[str, Any]]:
    if _use_sql():
        with Session(get_engine()) as session:
            rows = session.query(Product).filter(Product.merchant_id == merchant_id).all()
            return [{
                "id": row.id,
                "merchant_id": row.merchant_id,
                "title": row.title,
                "description": row.description,
                "price": row.price,
                "category": row.category,
                "attributes": row.attributes or {},
                "image_url": row.image_url,
                "stock": row.stock,
            } for row in rows]
    return list(_DB.get(merchant_id, {}).values())


def get_product(merchant_id: str, product_id: str) -> Optional[dict[str, Any]]:
    if _use_sql():
        with Session(get_engine()) as session:
            row = session.query(Product).filter(Product.merchant_id == merchant_id, Product.id == product_id).first()
            if row is None:
                return None
            return {
                "id": row.id,
                "merchant_id": row.merchant_id,
                "title": row.title,
                "description": row.description,
                "price": row.price,
                "category": row.category,
                "attributes": row.attributes or {},
                "image_url": row.image_url,
                "stock": row.stock,
            }
    return _DB.get(merchant_id, {}).get(product_id)


def update_product(merchant_id: str, product_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
    if _use_sql():
        with Session(get_engine()) as session:
            row = session.query(Product).filter(Product.merchant_id == merchant_id, Product.id == product_id).first()
            if row is None:
                return None
            for key, value in updates.items():
                if value is not None:
                    setattr(row, key, value)
            if "category" in updates and updates["category"] is not None:
                upsert_category(merchant_id, str(updates["category"]).strip())
            session.commit()
            return get_product(merchant_id, product_id)

    tenant = _DB.get(merchant_id)
    if not tenant or product_id not in tenant:
        return None
    tenant[product_id].update({k: v for k, v in updates.items() if v is not None})
    return tenant[product_id]


def search_catalog(
    merchant_id: str,
    query: Optional[str] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    in_stock_only: bool = False,
) -> list[dict[str, Any]]:
    """
    Structured filtering (spec 2.3, Option A). Matches `query` against
    title/description case-insensitively; filters by category and price
    range; optionally excludes out-of-stock items so the agent's
    search_catalog tool never surfaces unpurchasable products by default.
    """
    results = get_catalog(merchant_id)

    if query:
        q = query.lower()
        results = [
            p for p in results
            if q in p.get("title", "").lower() or q in p.get("description", "").lower()
        ]
    if category:
        results = [p for p in results if (p.get("category") or "").lower() == category.lower()]
    if min_price is not None:
        results = [p for p in results if p.get("price", 0) >= min_price]
    if max_price is not None:
        results = [p for p in results if p.get("price", 0) <= max_price]
    if in_stock_only:
        results = [p for p in results if p.get("stock", 0) > 0]

    return results


def get_categories(merchant_id: str) -> list[str]:
    if _use_sql():
        with Session(get_engine()) as session:
            rows = session.query(Category).filter(Category.merchant_id == merchant_id).order_by(Category.name.asc()).all()
            return [row.name for row in rows]
    return sorted({p.get("category") for p in get_catalog(merchant_id) if p.get("category")})


def upsert_category(merchant_id: str, category_name: str) -> None:
    if not category_name:
        return
    if _use_sql():
        create_schema()
        with Session(get_engine()) as session:
            _ensure_merchant(session, merchant_id)
            existing = session.query(Category).filter(Category.merchant_id == merchant_id, Category.name == category_name).first()
            if existing is None:
                session.add(Category(merchant_id=merchant_id, name=category_name))
            session.commit()
        return
    return


# --- raw rows -----------------------------------------------------------------
#
# The uploaded sheet, verbatim. This is what the agent reads (docs/CONTRACTS.md §0):
# the merchant stores raw rows and serves them untouched; the agent derives all meaning.
# The normalized `Product` above is the merchant console's own view and must never be
# what the agent consumes.

# merchant_id -> {"rows": [...], "id_column": str|None, "uploaded_at": str, "source_filename": str|None}
_RAW: dict[str, dict[str, Any]] = {}


def first_unique_column(rows: list[dict[str, Any]]) -> Optional[str]:
    """Storage-level id detection: first column that is fully populated and fully unique.

    Not a claim about semantics — deriving roles properly is the agent's job. Ported from
    the agent's stubs/mock_services.py so both sides detect the id the same way.
    """
    if not rows:
        return None
    for column in rows[0]:
        values = [row.get(column) for row in rows]
        if all(v not in (None, "") for v in values) and len(set(map(str, values))) == len(values):
            return column
    return None


def _stringify(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every value a string or None — the contract the agent's coerce step relies on.

    It needs the original text (`"£129.00"`, not `129.0`) to detect currency and units.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            str(k): (None if v is None or v != v else str(v))  # v != v catches NaN
            for k, v in row.items()
        })
    return out


def replace_raw_rows(
    merchant_id: str,
    rows: list[dict[str, Any]],
    id_column: Optional[str] = None,
    source_filename: Optional[str] = None,
) -> dict[str, Any]:
    """Upload semantics: a fresh upload replaces this merchant's raw sheet."""
    clean = _stringify(rows)
    resolved = id_column or first_unique_column(clean)
    uploaded_at = datetime.now(timezone.utc).isoformat()

    if _use_sql():
        create_schema()
        with Session(get_engine()) as session:
            _ensure_merchant(session, merchant_id)
            session.merge(RawRows(
                merchant_id=merchant_id,
                id_column=resolved,
                rows=clean,
                row_count=len(clean),
                uploaded_at=uploaded_at,
                source_filename=source_filename,
            ))
            session.commit()
    else:
        _RAW[merchant_id] = {
            "rows": clean,
            "id_column": resolved,
            "uploaded_at": uploaded_at,
            "source_filename": source_filename,
        }

    return {
        "merchant_id": merchant_id,
        "id_column": resolved,
        "row_count": len(clean),
        "uploaded_at": uploaded_at,
        "source_filename": source_filename,
    }


def _load_raw(merchant_id: str) -> Optional[dict[str, Any]]:
    if _use_sql():
        with Session(get_engine()) as session:
            record = session.get(RawRows, merchant_id)
            if record is None:
                return None
            return {
                "rows": list(record.rows or []),
                "id_column": record.id_column,
                "uploaded_at": record.uploaded_at,
                "source_filename": record.source_filename,
            }
    return _RAW.get(merchant_id)


def get_raw_rows(
    merchant_id: str, ids: Optional[list[str]] = None
) -> Optional[dict[str, Any]]:
    """Raw rows for a merchant. `ids` returns exactly those rows, in the order requested.

    Returns None when the merchant is unknown, so the caller can 404 rather than serve an
    empty catalog that looks like a merchant with no products.
    """
    record = _load_raw(merchant_id)
    if record is None:
        return None

    rows = record["rows"]
    id_column = record["id_column"]

    if ids is not None:
        wanted = [i for i in (s.strip() for s in ids) if i]
        if id_column:
            lookup = {str(r.get(id_column)): r for r in rows if r.get(id_column) is not None}
        else:
            lookup = {}
        rows = [lookup[i] for i in wanted if i in lookup]

    return {
        "merchant_id": merchant_id,
        "id_column": id_column,
        "row_count": len(rows),
        "rows": rows,
    }


def list_merchants() -> list[dict[str, Any]]:
    """Every merchant that has raw rows stored."""
    if _use_sql():
        with Session(get_engine()) as session:
            records = session.query(RawRows).all()
            return [
                {
                    "merchant_id": r.merchant_id,
                    "row_count": r.row_count,
                    "id_column": r.id_column,
                }
                for r in records
            ]
    return [
        {
            "merchant_id": merchant_id,
            "row_count": len(record["rows"]),
            "id_column": record["id_column"],
        }
        for merchant_id, record in _RAW.items()
    ]
