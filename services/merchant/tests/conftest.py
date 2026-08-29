"""Test isolation from the environment.

`app/config.py` reads the repo-root `.env` as a fallback, which is what makes one unified
`.env` work for both compose and bare `uvicorn` runs. The side effect is that a developer's
`MERCHANT_DATABASE_URL` would otherwise leak into the test suite and point it at a real
Postgres — so the tests would pass or fail depending on whether a container happened to be
running, which is not a test suite.

These tests exercise the in-memory store deliberately. Setting the variables to empty
strings (rather than deleting them) matters: os.environ takes precedence over dotenv files
in pydantic-settings, so an empty value here reliably beats whatever the root `.env` says.
"""

import os

os.environ["MERCHANT_DATABASE_URL"] = ""
os.environ["DATABASE_URL"] = ""
os.environ["SEED_ON_STARTUP"] = "false"

# Imported after the environment is pinned, so the lru_cached settings and the lazily
# built engine both resolve to in-memory mode.
from app.config import reset_settings_cache  # noqa: E402
from app.db.database import reset_engine  # noqa: E402

reset_settings_cache()
reset_engine()
