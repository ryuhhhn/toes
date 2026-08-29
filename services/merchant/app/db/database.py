from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import JSON, Engine, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from app.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False, default="now")
    products: Mapped[list["Product"]] = relationship(back_populates="merchant")
    categories: Mapped[list["Category"]] = relationship(back_populates="merchant")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    merchant: Mapped[Merchant] = relationship(back_populates="categories")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False, default="")
    price: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    merchant: Mapped[Merchant] = relationship(back_populates="products")


class RawRows(Base):
    """The uploaded sheet, stored verbatim.

    One row per merchant: an upload replaces the whole thing, matching `replace_catalog`.
    This is what the agent reads. `Product` above is the merchant console's own normalized
    view and must never be what the agent consumes — normalizing at upload destroys the raw
    columns the agent's profiler needs, and a normalized row cannot be un-normalized.
    """

    __tablename__ = "raw_rows"

    merchant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id_column: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_at: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)


# --- engine, built lazily at startup rather than at import --------------------

_engine: Engine | None = None
_initialized = False


def init_engine() -> Engine | None:
    """Build the engine from settings. Called once, from the app's lifespan.

    Idempotent, so tests and the seed path can call it freely.
    """
    global _engine, _initialized

    if _initialized:
        return _engine

    url = get_settings().sqlalchemy_url
    if url:
        try:
            _engine = create_engine(url, future=True)
        except Exception:  # pragma: no cover - bad URL should not kill the service
            log.exception("could not create engine for %s — falling back to in-memory", _redact(url))
            _engine = None
    else:
        _engine = None

    _initialized = True
    return _engine


def get_engine() -> Engine | None:
    """The engine, initializing on first use so bare imports still work."""
    if not _initialized:
        return init_engine()
    return _engine


def reset_engine() -> None:
    """Tests only — drop the cached engine so settings can be re-read."""
    global _engine, _initialized
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _initialized = False


def storage_mode() -> str:
    """What `GET /health` reports, so nobody debugs a store they aren't using."""
    return "postgres" if get_engine() is not None else "memory"


def _redact(url: str) -> str:
    """Never log a password."""
    if "@" not in url or "//" not in url:
        return url
    scheme, rest = url.split("//", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}//{user}:***@{host}"


def describe_storage() -> str:
    url = get_settings().sqlalchemy_url
    return _redact(url) if url else "in-memory (no MERCHANT_DATABASE_URL / DATABASE_URL set)"


def create_schema() -> None:
    engine = get_engine()
    if engine is None:
        return
    Base.metadata.create_all(bind=engine)


def load_json_attrs(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    if isinstance(value, dict):
        return value
    return {}
