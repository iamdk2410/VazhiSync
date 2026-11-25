"""Application settings, loaded from the repo-root `.env` (or the process environment)."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class GpsDropout(BaseModel):
    """A bus whose GPS goes silent for a while (simulated seconds from the world's start)."""

    bus_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
    start_s: int = Field(ge=0)
    duration_s: int = Field(gt=0)


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

    # Early warning (B21): look ahead with "do nothing" from MCP observations
    early_warning_enabled: bool = True
    early_warning_every_seconds: int = Field(default=30, ge=10, le=300)
    early_warning_horizon_minutes: float = Field(default=15.0, gt=0, le=30)
    early_warning_step_seconds: float = Field(default=30.0, ge=5, le=120)
    early_warning_seeds: int = Field(default=3, ge=1, le=7)
    early_warning_min_agree: int = Field(default=2, ge=1)  # seeds that must bunch the pair
    early_warning_cooldown_minutes: float = Field(default=5.0, ge=0)  # per pair, between alerts

    # LLMs (B9). "<provider>/<model id>"; providers: groq, openrouter, ollama. An empty model or
    # a missing key skips that tier: primary -> backup -> fallback -> local -> rules.
    primary_model: str = "groq/openai/gpt-oss-120b"
    backup_model: str = "groq/qwen/qwen3.8-27b"
    fallback_model: str = "openrouter/google/gemma-4-31b-it:free"
    local_model: str = "ollama/qwen3:8b"
    ollama_url: str = "http://localhost:11434"
    groq_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    llm_timeout_seconds: float = Field(default=20.0, gt=0)  # per attempt (local: 3x)
    llm_attempts: int = Field(default=2, ge=1, le=4)  # timeouts / connection / 5xx only (rule 7)
    llm_max_output_tokens: int = Field(default=800, ge=64)
    llm_rate_limit_cooldown_seconds: float = Field(default=60.0, gt=0)  # 429 -> skip the tier
    llm_run_token_budget: int = Field(default=12_000, ge=0)  # whole run, all agents
    llm_agent_token_budget: int = Field(default=5_000, ge=0)  # one agent within a run
    llm_time_reserve_seconds: float = Field(default=45.0, ge=0)  # run time kept for the rest
    # The local model inside live workflow runs. Off: qwen3:8b on this machine needs ~75 s per
    # call, longer than a run can spare, so when every hosted tier is rate-limited the agents
    # go straight to their rules. Scenario generation (B5) uses Ollama directly either way.
    llm_local_in_runs: bool = False
    llm_enabled: bool = True  # false: agents use their rules only

    # Langfuse (optional: observability profile)
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "http://localhost:3000"

    # MCP client and agentic harness (B8)
    avl_mcp_url: str = "http://localhost:8101/mcp"
    demand_mcp_url: str = "http://localhost:8102/mcp"
    tool_timeout_seconds: float = Field(default=10.0, gt=0)
    tool_attempts: int = Field(default=3, ge=1, le=5)  # transient errors only (rule 7)
    tool_unavailable_cooldown_seconds: float = Field(default=30.0, gt=0)
    run_time_limit_seconds: float = Field(default=120.0, gt=0)  # whole workflow run (rule 8)
    agent_max_tool_calls: int = Field(default=12, ge=1)  # per agent per run (rule 8)

    # Live world runner (B6)
    world_route_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")] = "32B"
    world_seed: int = 1
    world_speed: Literal[1, 5, 10] = 1  # starting speed; officers change it at runtime
    world_tick_seconds: float = Field(default=2.0, ge=1.0, le=5.0)  # real seconds per tick
    world_queue_every_seconds: float = Field(default=10.0, ge=1.0)  # stop_queues write interval
    world_retention_hours: float = Field(default=6.0, gt=0)  # older positions/queues are pruned
    world_gps_dropouts: list[GpsDropout] = []  # JSON in the env, e.g. [{"bus_id": ...}]

    # API
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(
        "groq_api_key",
        "openrouter_api_key",
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
