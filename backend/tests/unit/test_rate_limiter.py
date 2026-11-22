import pytest
from fastapi import HTTPException, Request
from redis.exceptions import ConnectionError as RedisConnectionError

from app.auth.rate_limit import MemoryRateLimiter, rate_limited


async def test_memory_limiter_blocks_after_limit() -> None:
    limiter = MemoryRateLimiter()
    results = [await limiter.hit("k", limit=3, window_seconds=60) for _ in range(4)]
    assert results[:3] == [None, None, None]
    assert results[3] is not None
    assert 1 <= results[3] <= 60


async def test_memory_limiter_keys_are_independent() -> None:
    limiter = MemoryRateLimiter()
    await limiter.hit("a", limit=1, window_seconds=60)
    assert await limiter.hit("b", limit=1, window_seconds=60) is None


async def test_memory_limiter_window_resets() -> None:
    limiter = MemoryRateLimiter()
    await limiter.hit("k", limit=1, window_seconds=0)
    assert await limiter.hit("k", limit=1, window_seconds=0) is None


class _BrokenLimiter:
    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        raise RedisConnectionError("redis down")


class _FullLimiter:
    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        return 42


def _request() -> Request:
    return Request({"type": "http", "client": ("10.0.0.1", 1234), "headers": [], "app": None})


async def test_redis_outage_fails_open() -> None:
    """A Redis outage must not lock every user out of login (logged, then allowed)."""
    await rate_limited("login")(_request(), _BrokenLimiter())


async def test_exceeded_limit_raises_429_with_retry_after() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await rate_limited("login")(_request(), _FullLimiter())
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "42"}
