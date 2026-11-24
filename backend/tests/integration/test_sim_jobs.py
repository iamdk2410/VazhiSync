"""B11 acceptance: four candidates finish in parallel in under ~10 s on the simulation worker,
and a deliberately slow job times out without blocking the rest.

The workflow submits one ARQ job per candidate (one per Send branch) to a real ARQ worker
running in-process; job states land in `simulation_jobs`. The slow job is a test-only worker
function wrapping the real one - production code has no "be slow" switch.
"""

import asyncio
import time
import uuid
from typing import Any

import pytest
from arq import ArqRedis
from arq.worker import Worker
from sqlalchemy import select

from app.config import get_settings
from app.db.enums import JobStatus
from app.db.models import Recommendation, SimulationJob
from app.db.session import get_sessionmaker
from app.graph.workflow import start_workflow
from app.runner.world import WorldRunner
from app.workers.simulations import simulate_candidate
from tests.integration.sim_worker import running_worker
from tests.integration.test_workflow import (  # noqa: F401
    bunched,
    clock,
    harness,
    only_event,
    redis,
    state_of,
)

pytestmark = pytest.mark.services


async def jobs(run_id: uuid.UUID) -> list[SimulationJob]:
    async with get_sessionmaker()() as session:
        return list(
            await session.scalars(select(SimulationJob).where(SimulationJob.run_id == run_id))
        )


async def test_four_candidates_finish_in_parallel_quickly(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
) -> None:
    event = await only_event()
    settings = get_settings()
    t0 = time.monotonic()
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )
    elapsed = time.monotonic() - t0
    state = state_of(result)

    candidates = state["candidates"]
    assert len(candidates) == 4  # hold, skip, shorter hold + do nothing
    assert elapsed < 10, f"workflow took {elapsed:.1f} s"
    rows = await jobs(result.run_id)  # type: ignore[arg-type]
    assert len(rows) == 4 and all(r.status == JobStatus.COMPLETED for r in rows)
    assert all(r.seeds == [11, 22, 33, 44, 55] and r.result for r in rows)
    # They really ran at the same time: every job started before any job finished.
    assert max(r.started_at for r in rows if r.started_at) < min(
        r.finished_at for r in rows if r.finished_at
    )

    # Confidence ranges and the bad case are in the ranking the officer will see.
    ranking = state["recommendation"].ranking
    for r in ranking[:-1]:
        assert r.benefit_low <= r.benefit_passenger_minutes <= r.benefit_high
    async with get_sessionmaker()() as session:
        stored = await session.scalar(
            select(Recommendation).where(Recommendation.run_id == result.run_id)
        )
    assert stored is not None and {"benefit_low", "bad_case_benefit"} <= set(stored.ranking[0])


async def test_slow_job_times_out_without_blocking_the_rest(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    job_queue: ArqRedis,
) -> None:
    async def slow_for_the_short_hold(
        ctx: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        candidate = payload["candidate"]
        if candidate["action_type"] == "hold" and candidate["params"]["seconds"] < 120:
            await asyncio.sleep(60)  # a simulation that hangs
        return await simulate_candidate(ctx, payload)

    event = await only_event()
    settings = get_settings().model_copy(update={"sim_timeout_seconds": 3.0})
    async with running_worker(slow_for_the_short_hold) as worker:
        t0 = time.monotonic()
        result = await start_workflow(
            event.id, harness(settings), get_sessionmaker(), settings, job_queue
        )
        elapsed = time.monotonic() - t0
        state = state_of(result)

        outcomes = {o.candidate_key: o for o in state["sim_outcomes"]}
        slow = [k for k, o in outcomes.items() if o.status == "timed_out"]
        assert len(slow) == 1 and slow[0].startswith("hold:") and slow[0].endswith(":90")
        assert sum(o.status == "completed" for o in outcomes.values()) == 3
        assert elapsed < 3.0 + 4  # waited for the timeout, not for the 60 s job

        # The recommendation comes from the jobs that finished, and says what didn't.
        rec = state["recommendation"]
        assert rec.candidate_key != slow[0]
        assert slow[0] not in [r.candidate_key for r in rec.ranking]
        assert "Not simulated in time" in rec.explanation
        rows = {r.status for r in await jobs(result.run_id)}  # type: ignore[arg-type]
        assert rows == {JobStatus.COMPLETED, JobStatus.TIMED_OUT}

        for _ in range(50):  # the hung job was aborted, freeing its worker slot
            if not worker.tasks or all(t.done() for t in worker.tasks.values()):
                break
            await asyncio.sleep(0.1)
        assert all(t.done() for t in worker.tasks.values())
