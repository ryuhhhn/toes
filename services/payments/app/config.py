"""All environment access for the payments service.

WHY this exists: every other module used to call `os.environ.get` with its own
inline default, so `DATABASE_URL` defaulted to `localhost:5432/payments` in three
different places and the root `.env` was never read at all. On a machine where
something else owns 5432 that produced an authentication failure rather than an
obvious misconfiguration.

`env_file` precedence: pydantic-settings gives the LAST file in the tuple the
higher precedence, so `("../../.env", ".env")` means a service-local `.env`
overrides the repo root. Verified experimentally in phase 4 — the order is
counter-intuitive and reversing it silently breaks every local override.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The host port is 5433 on the dev machine: a native PostgreSQL service owns
    # 5432 and on Windows shadows the container's publish silently.
    database_url: str = "postgresql://postgres:postgres@localhost:5433/payments"

    visa_mode: str = "mock"

    # WHY payments owns this clock: the ledger enforces expiry, so the agent must
    # read the server's `expires_at` rather than mint its own from a second TTL.
    payment_ttl_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests set env vars after import; this lets them take effect."""
    get_settings.cache_clear()
