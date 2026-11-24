"""World sim-runner entry point: `python -m app.runner.main`.

Runs the live world (app/runner/world.py) and writes a heartbeat to Redis for the container
health check. The heartbeat runs on its own so health reflects the process, while the world
loop keeps retrying through database/Redis hiccups instead of crashing.
"""

import asyncio
import contextlib
import time

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.observability.logging import configure_logging
from app.runner.route_loader import RouteNotImported, load_world_route
from app.runner.world import WorldRunner

log = structlog.get_logger()

HEARTBEAT_KEY = "vazhisync:heartbeat:sim-runner"
HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TTL_SECONDS = 30
ROUTE_RETRY_SECONDS = 30
ERROR_BACKOFF_MAX_SECONDS = 30


async def heartbeat(redis: Redis) -> None:
    while True:
        try:
            await redis.set(HEARTBEAT_KEY, str(time.time()), ex=HEARTBEAT_TTL_SECONDS)
        except RedisError as exc:
            log.warning("heartbeat_failed", error=repr(exc))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def world_loop(redis: Redis) -> None:
    settings = get_settings()
    sessionmaker = get_sessionmaker()
    runner: WorldRunner | None = None
    failures = 0
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        try:
            if runner is None:
                async with sessionmaker() as session:
                    route = await load_world_route(session, settings.world_route_id)
                runner = WorldRunner.from_settings(route, sessionmaker, redis, settings)
                await runner.setup()
            await runner.tick(settings.world_tick_seconds)
            failures = 0
        except RouteNotImported as exc:
            log.error("world_route_missing", error=str(exc))
            await asyncio.sleep(ROUTE_RETRY_SECONDS)
            continue
        except (SQLAlchemyError, RedisError, OSError) as exc:
            failures += 1
            backoff = min(2**failures, ERROR_BACKOFF_MAX_SECONDS)
            log.warning("world_tick_failed", error=repr(exc), retry_in_s=backoff)
            await asyncio.sleep(backoff)
            continue
        await asyncio.sleep(max(0.0, settings.world_tick_seconds - (loop.time() - started)))


async def run() -> None:
    configure_logging("sim-runner")
    redis: Redis = Redis.from_url(get_settings().redis_url)
    log.info("sim_runner_started")
    beat = asyncio.create_task(heartbeat(redis))
    try:
        await world_loop(redis)
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
