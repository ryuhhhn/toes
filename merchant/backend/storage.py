"""
Storage seam for the Merchant Backend.

Per spec 2.2, the real implementation should be a single Postgres instance,
multi-tenant via merchant_id on every row. This module supports both:
- SQLAlchemy/Postgres when DATABASE_URL is set
- in-memory fallback for local demo/test usage when DATABASE_URL is unset

The public function signatures stay stable so the API layer does not need to
change when the backing store is swapped.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from database import Category, Merchant, Product, Session, create_schema, engine

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
    return engine is not None and bool(os.getenv("DATABASE_URL"))


def replace_catalog(merchant_id: str, products: list[dict[str, Any]]) -> None:
    """Upload semantics: a fresh CSV upload replaces this merchant's catalog."""
    if _use_sql():
        create_schema()
        with Session(engine) as session:
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
        with Session(engine) as session:
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
        with Session(engine) as session:
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
        with Session(engine) as session:
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
        with Session(engine) as session:
            rows = session.query(Category).filter(Category.merchant_id == merchant_id).order_by(Category.name.asc()).all()
            return [row.name for row in rows]
    return sorted({p.get("category") for p in get_catalog(merchant_id) if p.get("category")})


def upsert_category(merchant_id: str, category_name: str) -> None:
    if not category_name:
        return
    if _use_sql():
        create_schema()
        with Session(engine) as session:
            _ensure_merchant(session, merchant_id)
            existing = session.query(Category).filter(Category.merchant_id == merchant_id, Category.name == category_name).first()
            if existing is None:
                session.add(Category(merchant_id=merchant_id, name=category_name))
            session.commit()
        return
    return
