"""ARQ worker entry point: `arq app.workers.main.WorkerSettings`.

Jobs: scenario generation (B5); simulation jobs arrive in B11.
Health: `arq --check app.workers.main.WorkerSettings`.
"""

from typing import Any, ClassVar

import structlog
from arq import func
from arq.connections import RedisSettings
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.lab.library import sync_golden
from app.observability.logging import configure_logging
from app.runner.route_loader import RouteNotImported
from app.workers.scenarios import generate_scenario

log = structlog.get_logger()

SCENARIO_JOB_TIMEOUT_S = 900  # up to 3 LLM attempts on a laptop GPU, plus the preview


async def ping(ctx: dict[str, Any]) -> str:
    """Placeholder job so the worker has something registered."""
    return "pong"


async def on_startup(ctx: dict[str, Any]) -> None:
    configure_logging("worker")
    log.info("worker_started")
    try:
        async with get_sessionmaker()() as session:
            await sync_golden(session, get_settings().world_route_id)
    except (RouteNotImported, SQLAlchemyError) as exc:
        # Not fatal: the worker still serves jobs; golden scenarios appear once possible.
        log.warning("golden_sync_skipped", error=str(exc))


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        ping,
        # One try: the Ollama client retries transient errors and the director regenerates.
        func(generate_scenario, timeout=SCENARIO_JOB_TIMEOUT_S, max_tries=1),
    ]
    on_startup = on_startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = get_settings().sim_concurrency
    health_check_interval = 10
