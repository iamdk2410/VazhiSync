"""A real ARQ simulation worker, run in-process on the test Redis database (B11 tests)."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

from arq import func
from arq.worker import Worker

from app.workers import simulations
from app.workers.queue import arq_redis_settings
from app.workers.simulations import SIM_QUEUE, simulate_candidate

JobFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


@contextlib.asynccontextmanager
async def running_worker(
    job: JobFn = simulate_candidate, max_jobs: int = 4
) -> AsyncIterator[Worker]:
    simulations._routes.clear()
    worker = Worker(
        functions=[func(cast(Any, job), name="simulate_candidate")],
        queue_name=SIM_QUEUE,
        redis_settings=arq_redis_settings(),
        max_jobs=max_jobs,
        allow_abort_jobs=True,
        handle_signals=False,
        poll_delay=0.05,
        max_tries=1,
    )
    await simulations.warm_pool()  # as the real sim-worker does on startup
    task = asyncio.create_task(worker.async_run())
    try:
        yield worker
    finally:
        # Worker.close() signals with SIGUSR1, which Windows lacks: stop it by hand.
        for job_task in list(worker.tasks.values()):
            job_task.cancel()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if worker._pool is not None:
            await worker._pool.aclose()
        simulations.shutdown_pool()
