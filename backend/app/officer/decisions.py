"""An officer's decision on a recommendation (B12), shared by `POST /officer/recommendations/
{id}/decision` and the AG-UI resume (`POST /officer/runs/{run_id}/agui`), so both behave the
same: one approval per recommendation, reject needs a reason, only a hold's length is
editable, an expired recommendation is re-planned instead of approved.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from arq import ArqRedis
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ActionType, ApprovalDecision
from app.db.models import Approval, CandidateAction, Recommendation, User, WorkflowRun
from app.graph.control import queue_resume

log = structlog.get_logger()

MAX_HOLD_SECONDS = 180


class OfficerDecisionPayload(BaseModel):
    """What the officer sends: the AG-UI interrupt's `responseSchema` and the REST body."""

    decision: Literal["approve", "edit", "reject"]
    seconds: int | None = Field(default=None, ge=1, le=MAX_HOLD_SECONDS)  # edit: hold length
    reason: str | None = Field(default=None, min_length=3, max_length=300)  # reject: required


class DecisionRefused(Exception):
    """The decision can't be taken; `status_code` is the HTTP status the API answers with."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class DecisionResult:
    status: Literal["accepted", "already_decided"]
    approval_id: uuid.UUID
    run_id: uuid.UUID


async def decide(
    session: AsyncSession,
    queue: ArqRedis,
    recommendation_id: uuid.UUID,
    body: OfficerDecisionPayload,
    officer: User,
) -> DecisionResult:
    """Record the decision and queue the run's resume. Deciding twice (a double-click, or
    the REST and AG-UI paths racing) returns the first decision, nothing more."""
    rec = await session.get(Recommendation, recommendation_id)
    if rec is None:
        raise DecisionRefused(404, "Recommendation not found")
    rec_id, run_id = rec.id, rec.run_id  # plain values: a rollback below expires the ORM objects
    existing = await session.scalar(select(Approval).where(Approval.recommendation_id == rec_id))
    if existing is not None:
        return DecisionResult("already_decided", existing.id, run_id)
    run = await session.get(WorkflowRun, run_id)
    if (
        run is None
        or run.waiting_for != "officer"
        or run.regeneration_round != rec.regeneration_round
    ):
        raise DecisionRefused(409, "Not awaiting a decision")
    candidate = await session.get(CandidateAction, rec.candidate_id)
    if body.decision == "reject" and not body.reason:
        raise DecisionRefused(422, "A reason is required")
    edited: dict[str, Any] | None = None
    if body.decision == "edit":
        if candidate is None or candidate.action_type != ActionType.HOLD or body.seconds is None:
            raise DecisionRefused(422, "Only a hold's length can be edited")
        edited = {"seconds": body.seconds}

    if rec.expires_at is not None and rec.expires_at < datetime.now(UTC):
        await queue_resume(queue, run_id, "officer", {"decision": "expired"}, f"{rec_id}:expired")
        raise DecisionRefused(409, "This recommendation expired; re-planning with fresh data")

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
        return DecisionResult("already_decided", first.id, run_id)
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
    return DecisionResult("accepted", approval.id, run_id)


async def open_recommendation(session: AsyncSession, run_id: uuid.UUID) -> Recommendation | None:
    """The recommendation a run waiting for the officer is paused on (its current round)."""
    run = await session.get(WorkflowRun, run_id)
    if run is None or run.waiting_for != "officer":
        return None
    rec: Recommendation | None = await session.scalar(
        select(Recommendation)
        .where(
            Recommendation.run_id == run_id,
            Recommendation.regeneration_round == run.regeneration_round,
        )
        .order_by(Recommendation.created_at.desc())
        .limit(1)
    )
    return rec


async def already_decided(session: AsyncSession, run_id: uuid.UUID, recommendation_id: str) -> bool:
    """Is this one of the run's recommendations, already approved / edited / rejected?"""
    try:
        rec_id = uuid.UUID(recommendation_id)
    except ValueError:
        return False
    found = await session.scalar(
        select(Approval.id)
        .join(Recommendation, Recommendation.id == Approval.recommendation_id)
        .where(Recommendation.id == rec_id, Recommendation.run_id == run_id)
    )
    return found is not None
