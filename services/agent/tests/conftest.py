"""Test-wide isolation and wiring.

Two things happen here:

1. DATA_DIR is redirected to a temp directory, so a test run never touches real profiles
   or a real index.
2. The merchant and payment stubs are mounted in-process over an ASGI transport, and the
   shared httpx client is pointed at them. That means the real MerchantClient and
   PaymentClient code paths are exercised, rather than mocked around.

Embeddings are used for real when Ollama is reachable, and the suite degrades to
filter-only assertions when it is not, so it passes on a machine without Ollama.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="visa-agent-tests-"))
os.environ["DATA_DIR"] = str(_TMP)
os.environ["MERCHANT_BASE_URL"] = "http://stub"
os.environ["PAYMENT_BASE_URL"] = "http://stub"
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("OPENAI_API_KEY", "")

import httpx  # noqa: E402
import pytest  # noqa: E402

import app.clients.http as http_module  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture
def data_dir() -> Path:
    return _TMP


@pytest.fixture(scope="session", autouse=True)
def stub_services():
    """Mount the stubs in-process and route the shared httpx client at them."""
    from stubs.mock_services import CATALOGS, load_catalogs
    from stubs.mock_services import app as stub_app

    CATALOGS.clear()
    CATALOGS.update(load_catalogs())

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stub_app), base_url="http://stub"
    )
    http_module._client = client
    yield CATALOGS


@pytest.fixture(autouse=True)
def reset_registry():
    """Each test gets a clean registry; indices are rebuilt on demand."""
    from app.retrieval.registry import get_registry

    registry = get_registry()
    registry._indices.clear()
    yield registry


async def embeddings_available() -> bool:
    from app.embeddings.base import EmbeddingUnavailable
    from app.embeddings.factory import get_embedder

    try:
        return await get_embedder().healthy()
    except (EmbeddingUnavailable, Exception):  # noqa: B014 - health must never raise
        return False
