"""AG-UI for the officer console (decision 10): watch a workflow run as an event stream and
resolve its approval interrupt. Officers only (rule 10).

- GET  /officer/runs                    runs to attach to (active first)
- GET  /officer/runs/{run_id}/events    SSE: attach (snapshot + live tail), Last-Event-ID resumes
- POST /officer/runs/{run_id}/agui      AG-UI RunAgentInput with `resume`: approve / edit /
                                        reject the open interrupt, then stream the resumed run

Live bus positions, driver instructions and passenger alerts stay on /ws/live.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

import structlog
from ag_ui.core import RunAgentInput
from arq import ArqRedis
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.agui import stream
from app.api.deps import get_job_queue, get_redis
from app.auth.deps import require_role
from app.db.enums import Role
from app.db.models import BunchingEvent, User, Vehicle, WorkflowRun
from app.db.session import get_session, get_sessionmaker
from app.officer import decisions
from app.officer.decisions import DecisionRefused, OfficerDecisionPayload

log = structlog.get_logger()

router = APIRouter(prefix="/officer/runs", tags=["officer", "ag-ui"])

Officer = Annotated[User, Depends(require_role(Role.OFFICER))]
Session = Annotated[AsyncSession, Depends(get_session)]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
CANCELLED_REASON = "cancelled in the officer console"


class RunSummary(BaseModel):
    run_id: uuid.UUID
    status: str
    waiting_for: str | None
    end_reason: str | None
    leader: str | None
    follower: str | None
    round: int
    started_at: datetime
    finished_at: datetime | None


class RunsResponse(BaseModel):
    runs: list[RunSummary]


def _sse(frames: object) -> StreamingResponse:
    return StreamingResponse(frames, media_type="text/event-stream", headers=SSE_HEADERS)  # type: ignore[arg-type]


@router.get("", response_model=RunsResponse)
async def list_runs(
    _: Officer,
    session: Session,
    active: Annotated[bool, Query(description="only runs that haven't finished")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RunsResponse:
    leader, follower = aliased(Vehicle), aliased(Vehicle)
    query = (
        select(WorkflowRun, leader.fleet_number, follower.fleet_number)
        .outerjoin(BunchingEvent, BunchingEvent.id == WorkflowRun.bunching_event_id)
        .outerjoin(leader, leader.id == BunchingEvent.leader_vehicle_id)
        .outerjoin(follower, follower.id == BunchingEvent.follower_vehicle_id)
        .order_by(WorkflowRun.started_at.desc())
        .limit(limit)
    )
    if active:
        query = query.where(WorkflowRun.finished_at.is_(None))
    rows = (await session.execute(query)).all()
    return RunsResponse(
        runs=[
            RunSummary(
                run_id=run.id,
                status=run.status.value,
                waiting_for=run.waiting_for,
                end_reason=run.end_reason,
                leader=lead,
                follower=follow,
                round=run.regeneration_round,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
            for run, lead, follow in rows
        ]
    )


@router.get(
    "/{run_id}/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}, 404: {"description": "No such run"}},
)
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    _: Officer,
    session: Session,
    redis: Annotated[Redis, Depends(get_redis)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Attach to a run, in progress or not: RUN_STARTED + STATE_SNAPSHOT rehydrate the console,
    then the live events until this segment ends. Reconnect with `Last-Event-ID`."""
    if await session.get(WorkflowRun, run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found")
    return _sse(
        stream.attach(redis, get_sessionmaker(), run_id, request.is_disconnected, last_event_id)
    )


@router.post(
    "/{run_id}/agui",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}},
        404: {"description": "No such run"},
        409: {"description": "No open interrupt with that id, or the recommendation expired"},
        422: {"description": "Invalid resume payload"},
    },
)
async def resume_run(
    run_id: uuid.UUID,
    body: RunAgentInput,
    request: Request,
    officer: Officer,
    session: Session,
    redis: Annotated[Redis, Depends(get_redis)],
    queue: Annotated[ArqRedis, Depends(get_job_queue)],
) -> StreamingResponse:
    """Resolve the approval interrupt (AG-UI `resume`) and stream the resumed run. Uses the
    same decision rules as POST /officer/recommendations/{id}/decision."""
    if await session.get(WorkflowRun, run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Run not found")
    if body.thread_id != str(run_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="threadId must be the run id"
        )
    if not body.resume:  # a plain run request: runs are started by the backend, so just attach
        return _sse(stream.attach(redis, get_sessionmaker(), run_id, request.is_disconnected))
    if len(body.resume) != 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Resolve exactly one interrupt"
        )
    entry = body.resume[0]
    rec = await decisions.open_recommendation(session, run_id)
    if rec is None or str(rec.id) != entry.interrupt_id:
        if await decisions.already_decided(session, run_id, entry.interrupt_id):
            # A late double-click: that interrupt was resolved; show where the run is now.
            return _sse(stream.attach(redis, get_sessionmaker(), run_id, request.is_disconnected))
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="No open interrupt with that id on this run"
        )
    try:
        payload = (
            OfficerDecisionPayload(decision="reject", reason=CANCELLED_REASON)
            if entry.status == "cancelled"
            else OfficerDecisionPayload.model_validate(entry.payload or {})
        )
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=exc.errors(include_url=False)
        ) from None
    before = await stream.position(redis, str(run_id))  # stream from here: no race
    try:
        result = await decisions.decide(session, queue, rec.id, payload, officer)
    except DecisionRefused as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from None
    log.info("agui_resume", run_id=str(run_id), decision=payload.decision, status=result.status)
    kind: Literal["accepted", "already_decided"] = result.status
    if kind == "already_decided":  # the resume ran (or is running): show where it is now
        return _sse(stream.attach(redis, get_sessionmaker(), run_id, request.is_disconnected))
    return _sse(
        stream.resumed(
            redis,
            get_sessionmaker(),
            run_id,
            before.last_id if before else None,
            request.is_disconnected,
        )
    )
