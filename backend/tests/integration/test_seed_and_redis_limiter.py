import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from app.auth.rate_limit import RedisRateLimiter
from app.config import get_settings
from app.db.enums import Role
from app.db.models import DriverAssignment, User, Vehicle
from app.db.seed import SeedError, parse_args, seed
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.services


async def test_seed_creates_officer(db: None) -> None:
    message = await seed(
        role=Role.OFFICER, email="Chief@MTC.in", password="officer-pass", name=None, vehicle=None
    )
    assert "created officer chief@mtc.in" in message
    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.email == "chief@mtc.in"))
    assert user is not None
    assert user.role == Role.OFFICER
    assert user.display_name == "Chief"


async def test_seed_refuses_duplicates(db: None) -> None:
    kwargs = {"email": "a@mtc.in", "password": "password-1", "name": None, "vehicle": None}
    await seed(role=Role.OFFICER, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(SeedError, match="already exists"):
        await seed(role=Role.DRIVER, **kwargs)  # type: ignore[arg-type]


async def test_seed_refuses_passengers(db: None) -> None:
    with pytest.raises(SeedError, match="self-register"):
        await seed(
            role=Role.PASSENGER, email="p@x.in", password="password-1", name=None, vehicle=None
        )


async def test_seed_driver_with_vehicle_assignment(db: None) -> None:
    async with get_sessionmaker()() as session:
        session.add(Vehicle(fleet_number="TN01AN9999", route_id="R21G"))
        await session.commit()
    message = await seed(
        role=Role.DRIVER, email="d@mtc.in", password="password-1", name="D", vehicle="TN01AN9999"
    )
    assert "assigned to TN01AN9999" in message
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(DriverAssignment)) is not None


async def test_seed_rejects_emails_that_register_would_reject(db: None) -> None:
    with pytest.raises(SeedError, match="invalid email"):
        await seed(
            role=Role.OFFICER,
            email="ops@mtc.local",
            password="password-1",
            name=None,
            vehicle=None,
        )


def test_seed_cli_rejects_passenger_role() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--role", "passenger", "--email", "p@x.in"])


async def test_redis_rate_limiter(database_url: str) -> None:
    redis: Redis = Redis.from_url(get_settings().redis_url)
    try:
        await redis.ping()
    except OSError as exc:
        pytest.skip(f"Redis not available: {exc!r}")
    limiter = RedisRateLimiter(redis)
    key = f"vazhisync:test:ratelimit:{uuid.uuid4()}"
    try:
        results = [await limiter.hit(key, limit=2, window_seconds=30) for _ in range(3)]
        assert results[:2] == [None, None]
        assert results[2] is not None
        assert 1 <= results[2] <= 30
    finally:
        await redis.delete(key)
        await limiter.aclose()
