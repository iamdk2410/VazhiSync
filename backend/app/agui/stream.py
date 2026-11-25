"""API side: serve a run's AG-UI events as Server-Sent Events (decision 10).

Attach = the hash's RunView as a synthesized RUN_STARTED + STATE_SNAPSHOT (plus a re-opened
text message if the explanation is mid-stream), then the Redis Stream from exactly the id that
snapshot includes, until the segment's RUN_FINISHED / RUN_ERROR. Every frame carries the Redis
entry id, so a reconnect with `Last-Event-ID` continues without gaps or duplicates. A run with
no stream (older than the 24 h retention, or published while Redis was down) is rebuilt from
the database instead.
"""

import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ag_ui.core import (
    BaseEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageStartEvent,
)
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agui.publisher import event_json, officer_interrupt
from app.agui.view import CandidateView, PairView, RecommendationView, RunView, StepView
from app.db.enums import CandidateSource, StepStatus, WorkflowStatus
from app.db.models import (
    AgentStep,
    Approval,
    BunchingEvent,
    CandidateAction,
    DriverInstruction,
    InterventionOutcome,
    Recommendation,
    Vehicle,
    WorkflowRun,
)
from app.live.channels import agui_stream_key, agui_view_key
from app.officer.status import run_messages

END_TYPES = {"RUN_FINISHED", "RUN_ERROR"}
HEARTBEAT_S = 15.0
MAX_TAIL_S = 600.0  # a segment ends long before; the client simply re-attaches after this

Disconnected = Callable[[], Awaitable[bool]]


def sse(data: str, event_id: str | None = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}data: {data}\n\n"


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


@dataclass(frozen=True)
class Position:
    """Where an attach starts: the view snapshot and the stream id it already includes."""

    view: RunView
    last_id: str
    segment_id: str
    finished: bool
    finish: str | None
    open_message: dict[str, str] | None


async def position(redis: Redis, run_id: str) -> Position | None:
    try:
        raw = await redis.hgetall(agui_view_key(run_id))  # type: ignore[misc]
    except RedisError:
        return None
    if not raw:
        return None
    fields = {_text(k): _text(v) for k, v in raw.items()}
    meta = json.loads(fields.get("meta", "{}"))
    return Position(
        view=RunView.model_validate_json(fields["view"]),
        last_id=fields.get("last_id") or "0-0",
        segment_id=meta.get("segment_id") or run_id,
        finished=meta.get("segment_state") == "finished",
        finish=meta.get("finish"),
        open_message=meta.get("open_message"),
    )


def _prefix(run_id: str, pos: Position) -> list[str]:
    """The synthesized start of an attach: the client rehydrates from this alone."""
    events: list[BaseEvent] = [
        RunStartedEvent(thread_id=run_id, run_id=pos.segment_id),
        StateSnapshotEvent(snapshot=pos.view.doc()),
    ]
    if pos.open_message is not None and not pos.finished:
        events.append(TextMessageStartEvent(message_id=pos.open_message["id"], role="assistant"))
        if pos.open_message["text"]:
            events.append(
                TextMessageContentEvent(
                    message_id=pos.open_message["id"], delta=pos.open_message["text"]
                )
            )
    frames = [sse(event_json(e), pos.last_id) for e in events]
    if pos.finished and pos.finish:
        frames.append(sse(pos.finish, pos.last_id))
    return frames


async def tail(
    redis: Redis,
    run_id: str,
    from_id: str,
    disconnected: Disconnected,
    *,
    heartbeat_s: float = HEARTBEAT_S,
    max_s: float = MAX_TAIL_S,
) -> AsyncIterator[str]:
    """Stream entries after `from_id` until the segment ends (or the client leaves)."""
    key = agui_stream_key(run_id)
    cursor = from_id
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        if await disconnected():
            return
        try:
            batches = await redis.xread({key: cursor}, count=200, block=int(heartbeat_s * 1000))
        except RedisError:
            return
        if not batches:
            yield ": keep-alive\n\n"
            continue
        for _, entries in batches:
            for entry_id, fields in entries:
                cursor = _text(entry_id)
                data = _text(fields.get(b"e", fields.get("e", b"{}")))
                yield sse(data, cursor)
                if json.loads(data).get("type") in END_TYPES:
                    return


async def attach(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    disconnected: Disconnected,
    last_event_id: str | None = None,
) -> AsyncIterator[str]:
    rid = str(run_id)
    if last_event_id:
        exists = False
        with contextlib.suppress(RedisError):
            exists = bool(await redis.exists(agui_stream_key(rid)))
        if exists:  # a reconnect: continue exactly where the client was
            async for frame in tail(redis, rid, last_event_id, disconnected):
                yield frame
            return
    pos = await position(redis, rid)
    if pos is None:
        for frame in await db_frames(sessionmaker, run_id):
            yield frame
        return
    for frame in _prefix(rid, pos):
        yield frame
    if pos.finished:
        return
    async for frame in tail(redis, rid, pos.last_id, disconnected):
        yield frame


async def resumed(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    from_id: str | None,
    disconnected: Disconnected,
) -> AsyncIterator[str]:
    """The segment a resume starts, streamed from the position captured before queueing it."""
    if from_id is None:
        async for frame in attach(redis, sessionmaker, run_id, disconnected):
            yield frame
        return
    async for frame in tail(redis, str(run_id), from_id, disconnected):
        yield frame


# ------------------------------------------------------------------ database fallback


def candidate_key(action_type: str, fleet: str | None, params: dict[str, Any], source: str) -> str:
    """The workflow's candidate keys (action_generator / dispatch.apply_edit), rebuilt."""
    if action_type == "do_nothing":
        return "do_nothing"
    if source == CandidateSource.OFFICER_EDIT.value:
        return f"edit:{fleet}:" + "-".join(f"{k}={v}" for k, v in sorted(params.items()))
    if action_type == "hold":
        return f"hold:{fleet}:{params.get('seconds')}"
    if action_type == "skip_stop":
        return f"skip:{fleet}:{'+'.join(params.get('stop_ids', []))}"
    return f"short_turn:{fleet}:{params.get('at_stop_id')}"


_RUN_STATUS = {
    WorkflowStatus.RUNNING: "running",
    WorkflowStatus.AWAITING_APPROVAL: "paused",
    WorkflowStatus.ESCALATED: "paused",
    WorkflowStatus.DISPATCHED: "paused",
    WorkflowStatus.FAILED: "failed",
    WorkflowStatus.TIMED_OUT: "failed",
}
_STEP_STATUS = {StepStatus.OK: "done", StepStatus.ERROR: "error", StepStatus.SKIPPED: "paused"}


async def view_from_db(session: AsyncSession, run_id: uuid.UUID) -> RunView | None:
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return None
    view = RunView(
        run_id=str(run_id),
        status=_RUN_STATUS.get(run.status, "finished"),
        waiting_for=run.waiting_for,
        end_reason=run.end_reason,
        round=run.regeneration_round,
        degraded=list(run.degraded or []),
        status_messages=run_messages(run.degraded),
    )
    fleets = dict((await session.execute(select(Vehicle.id, Vehicle.fleet_number))).all())
    event = await session.get(BunchingEvent, run.bunching_event_id)
    if event is not None:
        scheduled = event.scheduled_headway_seconds or 1.0
        view.pair = PairView(
            direction_id=-1,  # not stored on the event
            leader=fleets.get(event.leader_vehicle_id, "?"),
            follower=fleets.get(event.follower_vehicle_id, "?"),
            headway_s=round(event.headway_seconds, 1),
            scheduled_headway_s=round(scheduled, 1),
            ratio=round(event.headway_seconds / scheduled, 3),
            status="bunched",
        )
    steps = await session.scalars(
        select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.step_index)
    )
    view.steps = [
        StepView(
            id=str(s.step_index),
            name=s.node,
            status=_STEP_STATUS.get(s.status, "done"),
            routing=s.routing_decision,
        )
        for s in steps
    ]
    candidates = (
        await session.scalars(
            select(CandidateAction).where(
                CandidateAction.run_id == run_id,
                CandidateAction.regeneration_round == run.regeneration_round,
            )
        )
    ).all()
    keys: dict[uuid.UUID, str] = {}
    for c in candidates:
        fleet = fleets.get(c.vehicle_id) if c.vehicle_id else None
        key = candidate_key(c.action_type.value, fleet, c.params, c.source.value)
        keys[c.id] = key
        view.candidates.append(
            CandidateView(
                key=key,
                action_type=c.action_type.value,
                fleet_number=fleet,
                params=c.params,
                source=c.source.value,
                rationale=c.rationale or "",
                round=c.regeneration_round,
            )
        )
    rec = await session.scalar(
        select(Recommendation)
        .where(Recommendation.run_id == run_id)
        .order_by(Recommendation.created_at.desc())
        .limit(1)
    )
    if rec is not None:
        view.recommendation = RecommendationView(
            recommendation_id=str(rec.id),
            candidate_key=keys.get(rec.candidate_id, str(rec.candidate_id)),
            benefit_passenger_minutes=rec.benefit_passenger_minutes,
            ranking=list(rec.ranking),
            explanation=rec.explanation,
            near_tie=rec.is_near_tie,
            end_reason="escalated" if rec.is_near_tie else "recommended",
            expires_at=rec.expires_at.isoformat() if rec.expires_at else None,
            similar_cases=list(rec.similar_cases or []),
            confidence=rec.confidence,
            confidence_note=rec.confidence_note,
        )
        instruction = await session.scalar(
            select(DriverInstruction)
            .join(Approval, Approval.id == DriverInstruction.approval_id)
            .join(Recommendation, Recommendation.id == Approval.recommendation_id)
            .where(Recommendation.run_id == run_id)
            .order_by(DriverInstruction.sent_at.desc())
            .limit(1)
        )
        if instruction is not None:
            view.instruction = {
                "instruction_id": instruction.instruction_id,
                "fleet_number": fleets.get(instruction.vehicle_id),
                "action_type": instruction.action_type.value,
                "params": instruction.params,
                "status": instruction.status.value,
            }
            outcome = await session.scalar(
                select(InterventionOutcome).where(
                    InterventionOutcome.driver_instruction_id == instruction.id
                )
            )
            if outcome is not None:
                view.outcome = {
                    "predicted_benefit_passenger_minutes": (
                        outcome.predicted_benefit_passenger_minutes
                    ),
                    "actual_benefit_passenger_minutes": outcome.actual_benefit_passenger_minutes,
                    "bunching_resolved": outcome.bunching_resolved,
                }
    return view


async def db_frames(sessionmaker: async_sessionmaker[AsyncSession], run_id: uuid.UUID) -> list[str]:
    """RUN_STARTED, STATE_SNAPSHOT and RUN_FINISHED rebuilt from the database (no live tail:
    the run's events are gone or were never published)."""
    async with sessionmaker() as session:
        view = await view_from_db(session, run_id)
    if view is None:
        return []
    rid, segment = str(run_id), f"db-{run_id}"
    interrupt = officer_interrupt(view) if view.waiting_for == "officer" else None
    if interrupt is not None:
        finished = RunFinishedEvent(
            thread_id=rid,
            run_id=segment,
            outcome=RunFinishedInterruptOutcome(type="interrupt", interrupts=[interrupt]),
        )
    else:
        result: dict[str, Any] = {"status": view.status}
        if view.end_reason:
            result["end_reason"] = view.end_reason
        if view.waiting_for:
            result["waiting_for"] = view.waiting_for
        result["live"] = False
        finished = RunFinishedEvent(thread_id=rid, run_id=segment, result=result)
    events: list[BaseEvent] = [
        RunStartedEvent(thread_id=rid, run_id=segment),
        StateSnapshotEvent(snapshot=view.doc()),
        finished,
    ]
    return [sse(event_json(e)) for e in events]
