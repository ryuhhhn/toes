from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

DATABASE_URL = os.getenv("DATABASE_URL")


def _resolved_database_url() -> str | None:
    if not DATABASE_URL:
        return None
    if DATABASE_URL.startswith("postgresql://"):
        return DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    return DATABASE_URL


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


engine = create_engine(_resolved_database_url(), future=True) if _resolved_database_url() else None


def create_schema() -> None:
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
