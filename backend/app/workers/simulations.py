"""Counterfactual simulation jobs (B11), on their own queue and worker (`sim-worker`).

One job = one candidate simulated over several seeds. The payload carries only the
observations the MCP tools returned plus the candidate, so the worker builds its what-if model
with `from_observations` exactly as the Impact Simulator would (rule 1). Job state lives in
`simulation_jobs` (pending -> running -> completed / failed; the workflow marks timed_out).

A separate queue keeps simulations from competing with workflow runs for worker slots: a run
waiting on its simulations can never starve them (no deadlock). The simulator is CPU-bound pure
Python, so jobs run in a process pool (one process per concurrent job): threads would take turns
on the GIL and the "parallel" jobs would really run one after another.

Run: `arq app.workers.simulations.SimWorkerSettings`.
"""

import asyncio
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from typing import Any, ClassVar

import structlog

from app.agents.impact_simulator import payload_to_job, run_seeds
from app.config import get_settings
from app.db.enums import JobStatus
from app.db.models import SimulationJob
from app.db.session import get_sessionmaker
from app.observability.logging import configure_logging
from app.runner.route_loader import load_world_route
from app.workers.queue import arq_redis_settings
from simulator import ActionError, RouteModel

log = structlog.get_logger()

SIM_QUEUE = "vazhisync:sims"
_routes: dict[str, RouteModel] = {}
_pool: ProcessPoolExecutor | None = None


def process_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=get_settings().sim_concurrency)
    return _pool


def _warm() -> int:
    import simulator  # noqa: F401  (import the simulator in each process up front)

    return 0


async def warm_pool() -> None:
    """Start every process now. Spawning on demand blocks the event loop while each new process
    imports the code (seconds on Windows), which would stall the first jobs."""
    pool, loop = process_pool(), asyncio.get_running_loop()
    n = get_settings().sim_concurrency
    await asyncio.gather(*(loop.run_in_executor(pool, _warm) for _ in range(n)))


def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None


async def _route(route_id: str) -> RouteModel:
    if route_id not in _routes:
        async with get_sessionmaker()() as session:
            _routes[route_id] = (await load_world_route(session, route_id)).model
    return _routes[route_id]


async def _update(job_id: uuid.UUID, **values: Any) -> None:
    async with get_sessionmaker()() as session:
        row = await session.get(SimulationJob, job_id)
        if row is None:
            return
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()


async def simulate_candidate(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Returns {"status": "completed", "runs": [metrics per seed]} or {"status": "failed", ...}."""
    job_id = uuid.UUID(payload["job_id"])
    await _update(job_id, status=JobStatus.RUNNING, started_at=datetime.now(UTC))
    t0 = time.monotonic()
    try:
        route = await _route(str(payload["route_id"]))
        job = payload_to_job(payload, route)
        log.info(
            "sim_job_started", job_id=str(job_id), run_id=job.run_id, candidate=job.candidate_key
        )
        runs = await asyncio.get_running_loop().run_in_executor(
            process_pool(),
            run_seeds,
            route,
            job.observations,
            job.config,
            job.action,
            job.seeds,
            job.minutes,
        )
    except (ActionError, KeyError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        await _update(job_id, status=JobStatus.FAILED, error=error, finished_at=datetime.now(UTC))
        log.warning("sim_job_failed", job_id=str(job_id), error=error)
        return {"status": "failed", "error": error}
    result = {
        "status": "completed",
        "duration_ms": round((time.monotonic() - t0) * 1000),
        "runs": [
            {
                "passenger_wait_minutes": m.passenger_wait_minutes,
                "bunching_count": m.bunching_count,
                "left_behind": m.left_behind,
                "overcarried": m.overcarried,
                "displaced": m.displaced,
                "headway_cv": m.headway_cv,
            }
            for m in runs
        ],
    }
    await _update(job_id, status=JobStatus.COMPLETED, result=result, finished_at=datetime.now(UTC))
    log.info("sim_job_completed", job_id=str(job_id), duration_ms=result["duration_ms"])
    return result


async def on_startup(ctx: dict[str, Any]) -> None:
    configure_logging("sim-worker")
    await warm_pool()  # start the processes now, not on the first jobs
    log.info("sim_worker_started", queue=SIM_QUEUE, max_jobs=get_settings().sim_concurrency)


async def on_shutdown(ctx: dict[str, Any]) -> None:
    shutdown_pool()


class SimWorkerSettings:
    functions: ClassVar[list[Any]] = [simulate_candidate]
    queue_name = SIM_QUEUE
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = arq_redis_settings()
    max_jobs = get_settings().sim_concurrency  # rule 8: concurrency limit
    job_timeout = get_settings().sim_timeout_seconds * 3  # backstop; the workflow times out first
    max_tries = 1  # a failed simulation is reported, not retried (deterministic: same result)
    allow_abort_jobs = True  # the workflow aborts jobs it stopped waiting for
    health_check_interval = 10
