"""Central configuration. No other module reads os.environ directly."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM inference -----------------------------------------------------
    llm_provider: str = "openai"  # openai | anthropic
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    llm_timeout_seconds: float = 90.0

    # --- Embeddings --------------------------------------------------------
    embedding_provider: str = "ollama"  # ollama | openai
    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_model: str = "nomic-embed-text"
    openai_embed_model: str = "text-embedding-3-small"
    embed_timeout_seconds: float = 20.0
    embed_batch_size: int = 32

    # --- Downstream services ----------------------------------------------
    merchant_base_url: str = "http://localhost:9001"
    payment_base_url: str = "http://localhost:9001"
    http_timeout_seconds: float = 20.0

    # --- Agent behaviour ---------------------------------------------------
    max_probes_per_turn: int = 2
    max_probes_per_session: int = 4
    max_tool_rounds: int = 5
    preview_ttl_seconds: int = 300
    search_min_results: int = 3
    search_top_k: int = 6
    session_ttl_seconds: int = 60 * 60 * 4

    # --- Storage -----------------------------------------------------------
    data_dir: Path = Path("./data")

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    def ensure_dirs(self) -> None:
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
