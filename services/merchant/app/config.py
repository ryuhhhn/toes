"""All environment access for the merchant backend.

Two rules this module exists to enforce:

1. **Settings are read at startup, never at import.** `db/database.py` used to capture
   `DATABASE_URL` into a module-level constant and build the engine at import time, so a
   `.env` loaded any later silently yielded in-memory mode with no warning anywhere. Anyone
   who set `DATABASE_URL` and expected Postgres debugged a phantom.
2. **A service-local `.env` wins; the repo-root `.env` is the fallback.** pydantic-settings
   gives precedence to the *last* file in the tuple, so root comes first and local second.
   The reverse order silently makes the root override a service's own file.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Order matters: later files win. Root is the fallback, service-local overrides it.
        env_file=("../../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MERCHANT_DATABASE_URL is preferred because the repo-root .env also carries a bare
    # DATABASE_URL for the payments service, pointing at a different database on the same
    # Postgres instance. Falling back to DATABASE_URL keeps a service-local .env working.
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MERCHANT_DATABASE_URL", "DATABASE_URL"),
    )

    seed_on_startup: bool = True
    cors_origins: str = "*"

    @property
    def sqlalchemy_url(self) -> str | None:
        """psycopg3 is the installed driver, so `postgresql://` needs the explicit dialect."""
        if not self.database_url:
            return None
        if self.database_url.startswith("postgresql://"):
            return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return self.database_url

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()] or ["*"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests only — lets a test change the environment and re-resolve settings."""
    get_settings.cache_clear()
