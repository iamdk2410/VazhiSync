"""World sim-runner entry point: `python -m app.runner.main`.

B6 replaces the idle loop with the live world simulation. For now it only writes a
heartbeat to Redis so the container health check can tell it is alive.
"""

import asyncio
import time

import structlog
from redis.asyncio import Redis

from app.config import get_settings
from app.observability.logging import configure_logging

log = structlog.get_logger()

HEARTBEAT_KEY = "vazhisync:heartbeat:sim-runner"
HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TTL_SECONDS = 30


async def run() -> None:
    configure_logging("sim-runner")
    redis: Redis = Redis.from_url(get_settings().redis_url)
    log.info("sim_runner_started")
    try:
        while True:
            await redis.set(HEARTBEAT_KEY, str(time.time()), ex=HEARTBEAT_TTL_SECONDS)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(run())
