"""Fixed-window rate limiting for the public auth routes (login, register)."""

import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

import structlog
from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings

log = structlog.get_logger()

WINDOW_SECONDS = 60


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        """Count one request. Returns seconds to wait if the limit is exceeded, else None."""
        ...


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
        if int(count) > limit:
            return max(int(ttl), 1)
        return None

    async def aclose(self) -> None:
        await self._redis.aclose()


class MemoryRateLimiter:
    """In-process limiter for tests."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> int | None:
        now = time.monotonic()
        started, count = self._windows.get(key, (now, 0))
        if now - started >= window_seconds:
            started, count = now, 0
        count += 1
        self._windows[key] = (started, count)
        if count > limit:
            return max(int(window_seconds - (now - started)), 1)
        return None


def get_rate_limiter(request: Request) -> RateLimiter:
    """One Redis-backed limiter per app (tests override this dependency)."""
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = RedisRateLimiter(Redis.from_url(get_settings().redis_url))
        request.app.state.rate_limiter = limiter
    return limiter


def rate_limited(scope: str) -> Callable[..., Awaitable[None]]:
    """Dependency: allow AUTH_RATE_LIMIT_PER_MINUTE requests per client IP for `scope`."""

    async def dependency(
        request: Request, limiter: Annotated[RateLimiter, Depends(get_rate_limiter)]
    ) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"vazhisync:ratelimit:{scope}:{client_ip}"
        limit = get_settings().auth_rate_limit_per_minute
        try:
            retry_after = await limiter.hit(key, limit, WINDOW_SECONDS)
        except RedisError as exc:
            # Fail open: a Redis outage must not lock every user out. Logged for alerting.
            log.warning("rate_limit_unavailable", scope=scope, error=repr(exc))
            return
        if retry_after is not None:
            log.info("rate_limited", scope=scope, client_ip=client_ip)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency
