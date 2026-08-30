from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.catalog import router as catalog_router
from app.config import get_settings
from app.db.database import create_schema, describe_storage, init_engine, storage_mode
from app.normalize import normalize_csv
from app.storage import list_merchants, replace_catalog, replace_raw_rows
from app.tabular import read_upload

log = logging.getLogger(__name__)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SEED_CSV = FIXTURES / "eyewear_mock_data.csv"
SEED_MERCHANT_ID = "eyewear_co"


def _seed_catalog() -> None:
    """Seed from the raw CSV, not from eyewear.json.

    `eyewear.json` is post-normalization output — 50 products already coerced into the fixed
    nine-field shape — which makes it useless as agent input. The agent needs the raw sheet.
    It is kept in fixtures/ for the normalization tests.
    """
    if not SEED_CSV.exists():
        log.warning("seed skipped: %s not found", SEED_CSV)
        return

    try:
        # Through the same reader the upload route uses, so the seed can never be parsed
        # differently from a sheet a merchant actually uploads.
        df = read_upload(SEED_CSV.read_bytes(), SEED_CSV.name)
    except Exception:
        log.exception("seed skipped: could not read %s", SEED_CSV)
        return

    raw_rows = df.where(pd.notna(df), None).to_dict(orient="records")
    stored = replace_raw_rows(
        SEED_MERCHANT_ID, raw_rows, source_filename=SEED_CSV.name
    )

    products, report = normalize_csv(df, SEED_MERCHANT_ID)
    replace_catalog(SEED_MERCHANT_ID, products if report.ok else [])

    log.info(
        "seeded %s: %d raw rows (id_column=%s), %d normalized products",
        SEED_MERCHANT_ID, stored["row_count"], stored["id_column"], len(products),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Settings and the engine are resolved HERE, not at import. The previous version
    # captured DATABASE_URL into a module constant at import time, so a .env loaded any
    # later silently produced in-memory mode with no warning anywhere.
    settings = get_settings()
    init_engine()

    # If a database was explicitly requested we FAIL rather than fall back to memory.
    # Silently degrading is precisely the "debug a store you aren't using" failure this
    # service used to have.
    try:
        create_schema()
    except Exception as exc:
        if storage_mode() == "postgres":
            raise RuntimeError(
                f"MERCHANT_DATABASE_URL is set ({describe_storage()}) but the database is "
                f"unreachable: {exc}\n"
                "Either start it (`docker compose up -d db` from the repo root) or clear "
                "MERCHANT_DATABASE_URL to use the in-memory store."
            ) from exc
        raise

    log.info("merchant backend storage: %s -> %s", storage_mode(), describe_storage())

    if settings.seed_on_startup:
        _seed_catalog()

    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Merchant Backend", version="1.0.0", lifespan=lifespan)

    # The console is served from a different origin, and this app had no CORS at all.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(catalog_router)

    @app.get("/")
    async def root():
        return {
            "service": "Merchant Backend",
            "status": "ok",
            "docs": "/docs",
            "health": "/health",
        }

    @app.get("/health")
    async def healthcheck():
        """`storage` exists so nobody debugs a store they aren't using."""
        return {
            "status": "ok",
            "storage": storage_mode(),
            "merchants": len(list_merchants()),
        }

    return app


app = create_app()
