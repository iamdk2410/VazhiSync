import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from dotenv import dotenv_values
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent

# Where Postgres-backed tests run: TEST_DATABASE_URL, else the .env DATABASE_URL with a
# `_test` suffix on the database name. Read before the unit-test defaults below are applied.
_dev_database_url = os.environ.get("DATABASE_URL") or dotenv_values(REPO_ROOT / ".env").get(
    "DATABASE_URL"
)
_dev_redis_url = os.environ.get("REDIS_URL") or dotenv_values(REPO_ROOT / ".env").get("REDIS_URL")

# Unit tests must not depend on a developer's .env: provide the required settings up front.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5434/test")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-at-least-16-chars")


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------- services (Postgres + Redis)


def _test_database_url() -> str | None:
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    if not _dev_database_url:
        return None
    base = make_url(_dev_database_url)
    return base.set(database=f"{base.database}_test").render_as_string(hide_password=False)


async def _ensure_database(url: str) -> None:
    target = make_url(url)
    admin = create_async_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target.database},
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await admin.dispose()


def _unavailable(reason: str) -> None:
    if os.environ.get("REQUIRE_SERVICES") == "1":
        pytest.fail(f"services required but unavailable: {reason}")
    pytest.skip(f"Postgres/Redis not available ({reason}); start: docker compose up -d")


@pytest.fixture(scope="session")
def database_url() -> str:
    """Create + migrate the test database once per session (sync: alembic runs its own loop)."""
    url = _test_database_url()
    if url is None:
        _unavailable("no DATABASE_URL / TEST_DATABASE_URL")
        raise AssertionError  # unreachable
    try:
        asyncio.run(asyncio.wait_for(_ensure_database(url), timeout=10))
    except (OSError, TimeoutError) as exc:
        _unavailable(repr(exc))

    from alembic import command
    from alembic.config import Config

    from app.config import get_settings
    from app.db.session import get_engine, get_sessionmaker

    os.environ["DATABASE_URL"] = url
    if _dev_redis_url:
        os.environ["REDIS_URL"] = _dev_redis_url
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    command.upgrade(Config(str(BACKEND / "alembic.ini")), "head")
    return url


TABLES_TO_KEEP = {"alembic_version", "spatial_ref_sys"}


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[None]:
    """Empty every application table before the test."""
    from app.db.base import Base
    from app.db.session import get_engine

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables if t.name not in TABLES_TO_KEEP)
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@dataclass(frozen=True)
class Account:
    email: str
    password: str


@pytest.fixture
async def accounts(db: None) -> dict[str, Account]:
    """One user per role, created directly (officers/drivers can't self-register)."""
    from app.auth.service import create_user
    from app.db.enums import Role
    from app.db.session import get_sessionmaker

    created: dict[str, Account] = {}
    async with get_sessionmaker()() as session:
        for role in Role:
            account = Account(
                email=f"{role.value}@test.vazhisync", password=f"{role.value}-pass-123"
            )
            await create_user(
                session,
                email=account.email,
                password=account.password,
                role=role,
                display_name=role.value.title(),
            )
            created[role.value] = account
    return created


@pytest.fixture
async def network(db: None) -> None:
    """A minimal route + stops, for tests that need valid route_id/stop_id foreign keys."""
    from geoalchemy2 import WKTElement

    from app.db.models import Route, Stop
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        session.add(Route(id="R21G", short_name="21G", long_name="Test", agency="MTC", source={}))
        session.add_all(
            Stop(
                id=f"STOP_{i}",
                name=f"Stop {i}",
                geom=WKTElement(f"POINT(80.25 13.0{i})", srid=4326),
            )
            for i in range(1, 4)
        )
        await session.commit()
