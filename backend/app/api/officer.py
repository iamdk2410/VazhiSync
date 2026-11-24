import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import structlog
from arq import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_job_queue, get_redis
from app.api.schemas import (
    ActionParams,
    AlertsResponse,
    BunchingEventOut,
    DecisionRequest,
    DecisionResponse,
    RankedOut,
    RecommendationOut,
    RecommendationsResponse,
    WorldSpeedRequest,
    WorldStatus,
)
from app.auth.deps import require_role
from app.db.enums import ActionType, ApprovalDecision, BunchingEventStatus, Role
from app.db.models import (
    Approval,
    BunchingEvent,
    CandidateAction,
    Recommendation,
    User,
    Vehicle,
    WorkflowRun,
)
from app.db.session import get_session
from app.graph.control import queue_resume
from app.live.channels import SPEED_KEY
from app.live.hub import LiveHub
from app.runner.world import parse_speed

log = structlog.get_logger()

router = APIRouter(prefix="/officer", tags=["officer"])

Officer = Annotated[User, Depends(require_role(Role.OFFICER))]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/alerts", response_model=AlertsResponse)
async def list_alerts(
    _: Officer, session: Annotated[AsyncSession, Depends(get_session)]
) -> AlertsResponse:
    """Open bunching events, newest first (the officer's alert queue)."""
    rows = await session.scalars(
        select(BunchingEvent)
        .where(BunchingEvent.status.in_([BunchingEventStatus.OPEN, BunchingEventStatus.HANDLING]))
        .order_by(BunchingEvent.detected_at.desc())
        .limit(100)
    )
    return AlertsResponse(alerts=[BunchingEventOut.model_validate(r) for r in rows])


_LIVE_UNAVAILABLE = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Live world unavailable"
)


@router.get("/world", response_model=WorldStatus)
async def world_status(
    request: Request, _: Officer, redis: Annotated[Redis, Depends(get_redis)]
) -> WorldStatus:
    """The live world at a glance: simulated clock, speed, vehicles and open events."""
    try:
        speed = parse_speed(await redis.get(SPEED_KEY))
    except RedisError as exc:
        log.warning("world_status_unavailable", error=repr(exc))
        raise _LIVE_UNAVAILABLE from None
    hub: LiveHub | None = getattr(request.app.state, "live_hub", None)
    frame = await hub.latest() if hub else None
    return WorldStatus(
        route_id=frame.route_id if frame else None,
        sim_time=frame.sim_time if frame else None,
        speed=speed,
        vehicles=len(frame.vehicles) if frame else 0,
        stale_vehicles=sum(v.is_stale for v in frame.vehicles) if frame else 0,
        open_events=len(frame.events) if frame else 0,
    )


@router.put("/world/speed", response_model=WorldSpeedRequest)
async def set_world_speed(
    body: WorldSpeedRequest, officer: Officer, redis: Annotated[Redis, Depends(get_redis)]
) -> WorldSpeedRequest:
    """Run the live world at 1x, 5x or 10x; the runner picks it up on its next tick."""
    try:
        await redis.set(SPEED_KEY, str(body.speed))
    except RedisError as exc:
        log.warning("world_speed_unavailable", error=repr(exc))
        raise _LIVE_UNAVAILABLE from None
    log.info("world_speed_set", speed=body.speed, officer_id=str(officer.id))
    return body


# ------------------------------------------------------------------ recommendations (B12)


def _params(params: dict[str, Any]) -> ActionParams:
    return ActionParams.model_validate(params)


@router.get("/recommendations", response_model=RecommendationsResponse)
async def pending_recommendations(_: Officer, session: Session) -> RecommendationsResponse:
    """Runs waiting for an officer's decision, with their latest recommendation."""
    rows = (
        await session.execute(
            select(WorkflowRun, Recommendation, CandidateAction, Vehicle.fleet_number)
            .join(Recommendation, Recommendation.run_id == WorkflowRun.id)
            .join(CandidateAction, CandidateAction.id == Recommendation.candidate_id)
            .outerjoin(Vehicle, Vehicle.id == CandidateAction.vehicle_id)
            .where(WorkflowRun.waiting_for == "officer")
            .where(Recommendation.regeneration_round == WorkflowRun.regeneration_round)
            .order_by(Recommendation.created_at.desc())
        )
    ).all()
    return RecommendationsResponse(
        recommendations=[
            RecommendationOut(
                id=rec.id,
                run_id=run.id,
                run_status=run.status,
                round=rec.regeneration_round,
                action_type=cand.action_type,
                vehicle_id=cand.vehicle_id,
                fleet_number=fleet,
                params=_params(cand.params),
                benefit_passenger_minutes=rec.benefit_passenger_minutes,
                explanation=rec.explanation,
                is_near_tie=rec.is_near_tie,
                ranking=[RankedOut.model_validate(r) for r in rec.ranking],
                created_at=rec.created_at,
                expires_at=rec.expires_at,
            )
            for run, rec, cand, fleet in rows
        ]
    )


@router.post(
    "/recommendations/{recommendation_id}/decision",
    response_model=DecisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "Not awaiting a decision, or the recommendation expired"}},
)
async def decide(
    recommendation_id: uuid.UUID,
    body: DecisionRequest,
    officer: Officer,
    session: Session,
    queue: Annotated[ArqRedis, Depends(get_job_queue)],
) -> DecisionResponse:
    """Approve, edit (hold length) or reject. Recorded with the officer's id; the run resumes on
    the worker. Deciding twice (a double-click) returns the first decision, nothing more."""
    rec = await session.get(Recommendation, recommendation_id)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Recommendation not found")
    rec_id, run_id = rec.id, rec.run_id  # plain values: a rollback below expires the ORM objects
    existing = await session.scalar(select(Approval).where(Approval.recommendation_id == rec_id))
    if existing is not None:
        return DecisionResponse(status="already_decided", approval_id=existing.id, run_id=run_id)
    run = await session.get(WorkflowRun, run_id)
    if (
        run is None
        or run.waiting_for != "officer"
        or run.regeneration_round != rec.regeneration_round
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Not awaiting a decision")
    candidate = await session.get(CandidateAction, rec.candidate_id)
    if body.decision == "reject" and not body.reason:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="A reason is required")
    edited: dict[str, Any] | None = None
    if body.decision == "edit":
        if candidate is None or candidate.action_type != ActionType.HOLD or body.seconds is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Only a hold's length can be edited"
            )
        edited = {"seconds": body.seconds}

    if rec.expires_at is not None and rec.expires_at < datetime.now(UTC):
        await queue_resume(queue, run_id, "officer", {"decision": "expired"}, f"{rec_id}:expired")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="This recommendation expired; re-planning with fresh data",
        )

    decision = {
        "approve": ApprovalDecision.APPROVED,
        "edit": ApprovalDecision.EDITED,
        "reject": ApprovalDecision.REJECTED,
    }[body.decision]
    approval = Approval(
        recommendation_id=rec_id,
        officer_id=officer.id,
        decision=decision,
        edited_params=edited,
        reason=body.reason,
    )
    session.add(approval)
    try:
        await session.commit()
    except IntegrityError:  # a concurrent click won (one approval per recommendation)
        await session.rollback()
        first = await session.scalar(select(Approval).where(Approval.recommendation_id == rec_id))
        if first is None:
            raise
        return DecisionResponse(status="already_decided", approval_id=first.id, run_id=run_id)
    value = {
        "decision": body.decision,
        "approval_id": str(approval.id),
        "officer_id": str(officer.id),
        "params": edited,
        "reason": body.reason,
    }
    await queue_resume(queue, run_id, "officer", value, str(rec_id))
    log.info(
        "officer_decision",
        run_id=str(run_id),
        recommendation_id=str(rec_id),
        decision=body.decision,
        officer_id=str(officer.id),
    )
    return DecisionResponse(status="accepted", approval_id=approval.id, run_id=run_id)
