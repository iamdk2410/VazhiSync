"""ARQ worker entry point: `arq app.workers.main.WorkerSettings`.

Simulation jobs arrive in B11. Health: `arq --check app.workers.main.WorkerSettings`.
"""

from typing import Any, ClassVar

import structlog
from arq.connections import RedisSettings

from app.config import get_settings
from app.observability.logging import configure_logging

log = structlog.get_logger()


async def ping(ctx: dict[str, Any]) -> str:
    """Placeholder job so the worker has something registered."""
    return "pong"


async def on_startup(ctx: dict[str, Any]) -> None:
    configure_logging("worker")
    log.info("worker_started")


class WorkerSettings:
    functions: ClassVar[list[Any]] = [ping]
    on_startup = on_startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = get_settings().sim_concurrency
    health_check_interval = 10
