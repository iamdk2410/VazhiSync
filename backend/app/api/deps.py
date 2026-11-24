"""Shared clients for API routes (created on first use, closed in the app lifespan)."""

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from fastapi import Request
from redis.asyncio import Redis

from app.config import get_settings


def get_redis(request: Request) -> Redis:
    """The app's shared Redis client."""
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        redis = Redis.from_url(get_settings().redis_url)
        request.app.state.redis = redis
    return redis


async def get_job_queue(request: Request) -> ArqRedis:
    """ARQ pool for enqueueing background jobs on the worker."""
    pool: ArqRedis | None = getattr(request.app.state, "job_queue", None)
    if pool is None:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        request.app.state.job_queue = pool
    return pool
