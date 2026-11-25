"""ARQ jobs that run and resume the advisor workflow.

- `run_workflow`: queued by the world runner when it opens a bunching event.
- `resume_workflow`: queued by the API when an officer decides or a driver replies.
- `measure_outcome`: waits until ~15 world-minutes after the driver's ack, then resumes the run
  to measure the outcome (re-queues itself until then, so it follows the world's speed).
- `driver_ack_timeout`: if the driver never acknowledged, the instruction expires and the run
  re-plans.
- `retry_open_events` (every minute): re-queues events still open with no active run.

The graph state lives in the Postgres checkpointer, so any worker can resume any run, and the
database claims in app/graph/workflow.py make every job safe to deliver twice.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from arq import ArqRedis
from redis.exceptions import RedisError
from sqlalchemy import func, select

from app.config import get_settings
from app.db.enums import BunchingEventStatus, InstructionStatus
from app.db.models import BunchingEvent, DriverInstruction, WorkflowRun
from app.db.session import get_sessionmaker
from app.graph.workflow import ACTIVE, resume_workflow, start_workflow
from app.live.channels import LAST_FRAME_KEY
from app.live.schemas import LiveFrame

log = structlog.get_logger()

RETRY_AFTER = timedelta(seconds=60)  # since the event opened / its last run finished
OUTCOME_WINDOW_S = 900.0
MIN_RECHECK_S = 5


async def run_workflow(ctx: dict[str, Any], event_id: str) -> str:
    result = await start_workflow(
        uuid.UUID(event_id),
        ctx["harness"],
        get_sessionmaker(),
        get_settings(),
        ctx["redis"],
        ctx["checkpointer"],
        ctx.get("llm"),
    )
    return f"{result.status}:{result.end_reason}"


async def resume_workflow_job(
    ctx: dict[str, Any], run_id: str, kind: str, value: dict[str, Any]
) -> str:
    result = await resume_workflow(
        uuid.UUID(run_id),
        kind,
        value,
        ctx["harness"],
        get_sessionmaker(),
        get_settings(),
        ctx["redis"],
        ctx["checkpointer"],
        ctx.get("llm"),
    )
    return f"{result.status}:{result.end_reason}"


def clock_seconds(sim_time: str) -> float:
    h, m, s = (int(x) for x in sim_time.split(":"))
    return float(h * 3600 + m * 60 + s)


async def world_now(redis: Any) -> tuple[float, int] | None:
    """(world clock in seconds, speed) from the runner's latest frame."""
    try:
        raw = await redis.get(LAST_FRAME_KEY)
    except RedisError:
        return None
    if raw is None:
        return None
    frame = LiveFrame.model_validate_json(raw)
    return clock_seconds(frame.sim_time), frame.speed


async def measure_outcome(ctx: dict[str, Any], run_id: str, acked_world_time_s: float) -> str:
    queue: ArqRedis = ctx["redis"]
    now = await world_now(queue)
    if now is not None:
        world_s, speed = now
        remaining = acked_world_time_s + OUTCOME_WINDOW_S - world_s
        if remaining > 0:
            delay = max(MIN_RECHECK_S, remaining / max(speed, 1))
            await queue.enqueue_job(
                "measure_outcome",
                run_id,
                acked_world_time_s,
                _job_id=f"measure:{run_id}:{int(world_s)}",
                _defer_by=delay,
            )
            return f"waiting {remaining:.0f} world-s"
    return await resume_workflow_job(ctx, run_id, "outcome", {"kind": "measure"})


async def driver_ack_timeout(ctx: dict[str, Any], run_id: str, instruction_id: str) -> str:
    async with get_sessionmaker()() as session, session.begin():
        row = await session.scalar(
            select(DriverInstruction)
            .where(DriverInstruction.instruction_id == instruction_id)
            .with_for_update()
        )
        if row is None or row.status != InstructionStatus.SENT:
            return "acknowledged"
        row.status = InstructionStatus.EXPIRED
    log.warning("driver_ack_timeout", run_id=run_id, instruction_id=instruction_id)
    return await resume_workflow_job(
        ctx, run_id, "driver", {"kind": "timeout", "instruction_id": instruction_id}
    )


async def retry_open_events(ctx: dict[str, Any]) -> int:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        last_run = (
            select(
                WorkflowRun.bunching_event_id,
                func.max(func.coalesce(WorkflowRun.finished_at, WorkflowRun.started_at)).label(
                    "at"
                ),
                func.count()
                .filter(WorkflowRun.status.in_(ACTIVE), WorkflowRun.finished_at.is_(None))
                .label("active"),
            )
            .group_by(WorkflowRun.bunching_event_id)
            .subquery()
        )
        rows = (
            await session.execute(
                select(BunchingEvent.id)
                .outerjoin(last_run, last_run.c.bunching_event_id == BunchingEvent.id)
                .where(
                    BunchingEvent.status == BunchingEventStatus.OPEN,
                    BunchingEvent.detected_at < now - RETRY_AFTER,
                    func.coalesce(last_run.c.active, 0) == 0,
                    func.coalesce(last_run.c.at, BunchingEvent.detected_at) < now - RETRY_AFTER,
                )
            )
        ).all()
    queue: ArqRedis = ctx["redis"]
    for (event_id,) in rows:
        await queue.enqueue_job(
            "run_workflow", str(event_id), _job_id=f"workflow:{event_id}:{now:%Y%m%d%H%M}"
        )
    if rows:
        log.info("workflow_retries_queued", count=len(rows))
    return len(rows)
