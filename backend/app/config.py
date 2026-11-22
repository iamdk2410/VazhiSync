"""Application settings, loaded from the repo-root `.env` (or the process environment)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Infrastructure
    database_url: str = Field(min_length=1)
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    jwt_secret: SecretStr = Field(min_length=32)  # HS256 key: RFC 7518 recommends >= 32 bytes
    jwt_expire_minutes: int = Field(default=60, ge=1, le=24 * 60)
    auth_rate_limit_per_minute: int = Field(default=10, ge=1)

    # Bunching detection (rule 4: staleness threshold lives in config)
    stale_gps_seconds: int = Field(default=60, gt=0)
    bunching_ratio: float = Field(default=0.5, gt=0, lt=1)
    closing_ratio: float = Field(default=0.75, gt=0, lt=1)

    # Workflow limits (rule 8)
    sim_timeout_seconds: float = Field(default=10.0, gt=0)
    sim_concurrency: int = Field(default=4, ge=1)
    max_graph_steps: int = Field(default=25, ge=1)

    # LLMs
    primary_model: str = ""
    backup_model: str = ""
    local_model: str = "ollama/qwen3:8b"
    ollama_url: str = "http://localhost:11434"

    # Provider keys (optional until B9)
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    # Langfuse (optional: observability profile)
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "http://localhost:3000"

    # API
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(
        "anthropic_api_key",
        "openai_api_key",
        "gemini_api_key",
        "langfuse_secret_key",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _ratios_ordered(self) -> Self:
        if self.bunching_ratio >= self.closing_ratio:
            raise ValueError("BUNCHING_RATIO must be smaller than CLOSING_RATIO")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
