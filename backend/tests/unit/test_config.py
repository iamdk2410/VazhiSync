import pytest
from pydantic import ValidationError

from app.config import Settings

REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5434/db",
    "JWT_SECRET": "x" * 32,
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def load() -> Settings:
    return Settings(_env_file=None)


def test_defaults_match_claude_md(env: pytest.MonkeyPatch) -> None:
    s = load()
    assert s.stale_gps_seconds == 60
    assert s.bunching_ratio == 0.5
    assert s.closing_ratio == 0.75
    assert s.sim_concurrency == 4
    assert s.local_model == "ollama/qwen3:8b"


def test_cors_origins_comma_separated(env: pytest.MonkeyPatch) -> None:
    env.setenv("CORS_ORIGINS", "http://localhost:5173, http://127.0.0.1:5173")
    assert load().cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_empty_api_key_is_none(env: pytest.MonkeyPatch) -> None:
    env.setenv("GROQ_API_KEY", "")
    assert load().groq_api_key is None


def test_jwt_secret_never_printed(env: pytest.MonkeyPatch) -> None:
    assert "x" * 32 not in repr(load())


def test_missing_jwt_secret_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", REQUIRED["DATABASE_URL"])
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError, match="jwt_secret"):
        load()


def test_short_jwt_secret_fails(env: pytest.MonkeyPatch) -> None:
    env.setenv("JWT_SECRET", "short")
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize(
    ("var", "value"),
    [("STALE_GPS_SECONDS", "0"), ("BUNCHING_RATIO", "1.5"), ("SIM_CONCURRENCY", "0")],
)
def test_out_of_range_values_fail(env: pytest.MonkeyPatch, var: str, value: str) -> None:
    env.setenv(var, value)
    with pytest.raises(ValidationError):
        load()


def test_bunching_ratio_must_be_below_closing_ratio(env: pytest.MonkeyPatch) -> None:
    env.setenv("BUNCHING_RATIO", "0.8")
    env.setenv("CLOSING_RATIO", "0.75")
    with pytest.raises(ValidationError, match="BUNCHING_RATIO"):
        load()
