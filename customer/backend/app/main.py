"""FastAPI application factory.

Operational constraint from CLAUDE.md: SessionStore is in-process, so this must run under
exactly one uvicorn worker, and without --reload during a demo. Carts live in this process.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import catalog, chat, health, ingest, session
from app.clients.http import close_http_client, get_http_client
from app.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    app.state.http = get_http_client()
    log.info(
        "started | llm=%s embeddings=%s merchant=%s payment=%s",
        settings.llm_provider,
        settings.embedding_provider,
        settings.merchant_base_url,
        settings.payment_base_url,
    )
    try:
        yield
    finally:
        await close_http_client()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Consumer / Agent Backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    # The consumer chat UI and the merchant approval screen are separate origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(catalog.router)
    app.include_router(chat.router)
    app.include_router(session.router)
    return app


app = create_app()
