"""ARQ worker entry point: `arq app.workers.main.WorkerSettings`.

Jobs: scenario generation (B5), advisor workflow runs (B10); simulation jobs arrive in B11.
Health: `arq --check app.workers.main.WorkerSettings`.
"""

from typing import Any, ClassVar

import structlog
from arq import cron, func
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.graph.checkpoint import postgres_checkpointer
from app.harness.harness import ToolHarness
from app.lab.library import sync_golden
from app.llm.gateway import LlmGateway
from app.observability.logging import configure_logging
from app.runner.route_loader import RouteNotImported
from app.workers.queue import arq_redis_settings
from app.workers.scenarios import generate_scenario
from app.workers.workflows import (
    driver_ack_timeout,
    early_warning,
    measure_outcome,
    resume_workflow_job,
    retry_open_events,
    run_workflow,
)

log = structlog.get_logger()

SCENARIO_JOB_TIMEOUT_S = 900  # up to 3 LLM attempts on a laptop GPU, plus the preview


async def ping(ctx: dict[str, Any]) -> str:
    """Placeholder job so the worker has something registered."""
    return "pong"


async def on_startup(ctx: dict[str, Any]) -> None:
    configure_logging("worker")
    log.info("worker_started")
    ctx["harness"] = ToolHarness.from_settings(get_settings(), get_sessionmaker())
    await ctx["harness"].discover()  # logged; a down server is just marked unavailable
    ctx["checkpointer_cm"] = postgres_checkpointer(get_settings().database_url)
    ctx["checkpointer"] = await ctx["checkpointer_cm"].__aenter__()  # graph state (B12)
    settings = get_settings()
    ctx["llm"] = (
        LlmGateway.from_settings(
            settings, get_sessionmaker(), include_local=settings.llm_local_in_runs
        )
        if settings.llm_enabled
        else None
    )  # B9: candidate ideas and explanations; None -> rules only
    try:
        async with get_sessionmaker()() as session:
            await sync_golden(session, get_settings().world_route_id)
    except (RouteNotImported, SQLAlchemyError) as exc:
        # Not fatal: the worker still serves jobs; golden scenarios appear once possible.
        log.warning("golden_sync_skipped", error=str(exc))


async def on_shutdown(ctx: dict[str, Any]) -> None:
    if "checkpointer_cm" in ctx:
        await ctx["checkpointer_cm"].__aexit__(None, None, None)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        ping,
        # One try: the Ollama client retries transient errors and the director regenerates.
        func(generate_scenario, timeout=SCENARIO_JOB_TIMEOUT_S, max_tries=1),
        # The run enforces its own time limit; ARQ's timeout is only a backstop.
        func(
            run_workflow,
            name="run_workflow",
            timeout=get_settings().run_time_limit_seconds + 60,
            max_tries=1,
        ),
        func(
            resume_workflow_job,
            name="resume_workflow",
            timeout=get_settings().run_time_limit_seconds + 60,
            max_tries=1,
        ),
        func(measure_outcome, timeout=get_settings().run_time_limit_seconds + 60, max_tries=1),
        func(driver_ack_timeout, timeout=get_settings().run_time_limit_seconds + 60, max_tries=1),
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(retry_open_events, second={0}, run_at_startup=False),
        # B21: every EARLY_WARNING_EVERY_SECONDS (cron is unique: a slow sweep never overlaps)
        cron(
            early_warning,
            second=set(range(0, 60, get_settings().early_warning_every_seconds)),
            run_at_startup=False,
            timeout=60,
        ),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = arq_redis_settings()
    max_jobs = get_settings().sim_concurrency
    health_check_interval = 10
