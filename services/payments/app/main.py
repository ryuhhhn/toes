import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db.database import close_pool, init_pool, ping_db
from app.routers import payment_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # WHY: the asyncpg pool must exist before any request touches the ledger,
    # and be torn down cleanly so we never leak connections mid-transaction.
    await init_pool()
    yield
    await close_pool()


app = FastAPI(
    title="Payment/Auth Service",
    description=(
        "Owns the preview -> authorize -> confirm payment flow. "
        "Money never moves without a persisted preview and an explicit, "
        "recorded user authorization."
    ),
    lifespan=lifespan,
)

app.include_router(payment_router.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # WHY: an unexpected error must surface as a clean 500 JSON body, not crash
    # the process or leak internals — the ledger keeps the audit trail intact.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error": type(exc).__name__},
    )


@app.get("/health")
async def health() -> dict:
    # WHY: orchestrators/infra need liveness + DB reachability in one probe.
    # ASSUMPTION: ping_db() returns a truthy value on success and may raise on failure.
    try:
        reachable = bool(await ping_db())
    except Exception:
        reachable = False
    return {"status": "ok", "db": "connected" if reachable else "unreachable"}
