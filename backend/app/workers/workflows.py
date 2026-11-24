"""ARQ jobs that run the advisor workflow.

`run_workflow` is queued by the world runner when it opens a bunching event. `retry_open_events`
(every minute) re-queues events that are still open with no active run - e.g. after a run ended
on stale data or failed - so nothing is silently dropped. The database claim in
`start_workflow` guarantees one active run per bus pair however often a job is queued.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from arq import ArqRedis
from sqlalchemy import func, select

from app.config import get_settings
from app.db.enums import BunchingEventStatus
from app.db.models import BunchingEvent, WorkflowRun
from app.db.session import get_sessionmaker
from app.graph.workflow import ACTIVE, start_workflow

log = structlog.get_logger()

RETRY_AFTER = timedelta(seconds=60)  # since the event opened / its last run finished


async def run_workflow(ctx: dict[str, Any], event_id: str) -> str:
    result = await start_workflow(
        uuid.UUID(event_id), ctx["harness"], get_sessionmaker(), get_settings(), ctx["redis"]
    )
    return f"{result.status}:{result.end_reason}"


async def retry_open_events(ctx: dict[str, Any]) -> int:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        last_run = (
            select(
                WorkflowRun.bunching_event_id,
                func.max(func.coalesce(WorkflowRun.finished_at, WorkflowRun.started_at)).label(
                    "at"
                ),
                func.count().filter(WorkflowRun.status.in_(ACTIVE)).label("active"),
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
