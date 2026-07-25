"""Environment-backed application configuration."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration shared by API routes and service clients.

    External-service secrets remain optional while bootstrapping so local health checks
    and unit tests do not require live credentials. Each integration validates its own
    required settings when it is initialized.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "staging", "production"] = "development"
    log_level: str = "INFO"
    dev_dashboard_enabled: bool = False
    dev_dashboard_config_writes_enabled: bool = False
    dev_supervisor_enabled: bool = False
    dev_supervisor_url: str = "http://127.0.0.1:8765"
    dev_supervisor_token: SecretStr | None = None
    dev_supervisor_port: int = Field(default=8765, ge=1024, le=65535)
    dev_dashboard_username: str = "inventory-dev"
    dev_dashboard_token: SecretStr | None = None

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6-luna"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "none"
    inventory_agent_model: str = "gpt-5.6-sol"
    inventory_agent_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = (
        "low"
    )
    inventory_agent_enabled: bool = False
    inventory_agent_context_policy: Literal["discard", "summarize"] = "summarize"
    inventory_agent_context_retention_days: int = Field(default=7, ge=1)
    inventory_agent_context_max_tokens: int = Field(default=30_000, ge=1)
    inventory_agent_context_max_items: int = Field(default=300, ge=1, le=350)
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: Literal[512] = 512
    inventory_matching_strategy: Literal["semantic", "fuzzy", "hybrid"] = "semantic"
    inventory_candidate_judging_enabled: bool = True
    inventory_display_timezone: str = "Asia/Singapore"

    telegram_bot_token: SecretStr | None = None
    telegram_bot_username: str | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_webhook_url: str | None = None
    telegram_dev_user_simulation_enabled: bool = False
    telegram_dev_user_simulation_session_minutes: int = Field(default=120, ge=5, le=1440)

    supabase_url: str = "http://127.0.0.1:54321"
    supabase_publishable_key: SecretStr | None = None
    supabase_secret_key: SecretStr | None = None
    supabase_storage_bucket: str = "inventory-source-artifacts"


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings instance per process."""

    return Settings()
