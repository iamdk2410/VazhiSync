"""Bunching events and the advisor workflow: candidates, simulations, recommendation,
approval, dispatch, outcome."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, utc_now_column, uuid_pk
from app.db.enums import (
    ActionType,
    ApprovalDecision,
    BunchingEventStatus,
    BunchingSeverity,
    CandidateSource,
    InstructionStatus,
    JobStatus,
    WorkflowStatus,
)

Json = dict[str, Any]


class BunchingEvent(Base):
    __tablename__ = "bunching_events"
    __table_args__ = (
        Index("ix_bunching_events_status_detected", "status", text("detected_at DESC")),
        CheckConstraint("leader_vehicle_id <> follower_vehicle_id", name="distinct_vehicles"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    leader_vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id"))
    follower_vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id"))
    detected_at: Mapped[datetime] = utc_now_column()
    headway_seconds: Mapped[float]
    scheduled_headway_seconds: Mapped[float]
    severity: Mapped[BunchingSeverity] = mapped_column(str_enum(BunchingSeverity, "severity"))
    status: Mapped[BunchingEventStatus] = mapped_column(
        str_enum(BunchingEventStatus, "status"), server_default=BunchingEventStatus.OPEN.value
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowRun(Base):
    """One advisor run. `id` is the run_id carried by every log line (rule 9)."""

    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    bunching_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bunching_events.id"), index=True
    )
    status: Mapped[WorkflowStatus] = mapped_column(
        str_enum(WorkflowStatus, "status"), server_default=WorkflowStatus.RUNNING.value
    )
    step_count: Mapped[int] = mapped_column(server_default=text("0"))
    regeneration_round: Mapped[int] = mapped_column(server_default=text("0"))
    end_reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = utc_now_column()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CandidateAction(Base):
    __tablename__ = "candidate_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type = 'do_nothing' OR vehicle_id IS NOT NULL", name="vehicle_unless_noop"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    action_type: Mapped[ActionType] = mapped_column(str_enum(ActionType, "action_type"))
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vehicles.id"))
    params: Mapped[Json] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    source: Mapped[CandidateSource] = mapped_column(str_enum(CandidateSource, "source"))
    rationale: Mapped[str | None] = mapped_column(Text)
    regeneration_round: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = utc_now_column()


class SimulationJob(Base):
    __tablename__ = "simulation_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_actions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus, "status"), server_default=JobStatus.PENDING.value
    )
    seeds: Mapped[list[int]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    result: Mapped[Json | None] = mapped_column(JSONB)  # averaged metrics from the simulator
    error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = utc_now_column()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[uuid.UUID] = uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidate_actions.id"))
    ranking: Mapped[list[Json]] = mapped_column(JSONB)  # computed by the simulator, not the LLM
    benefit_passenger_minutes: Mapped[float]
    explanation: Mapped[str] = mapped_column(Text)  # written by the LLM
    is_near_tie: Mapped[bool] = mapped_column(server_default=text("false"))
    regeneration_round: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = utc_now_column()


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint("decision <> 'rejected' OR reason IS NOT NULL", name="reject_needs_reason"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # One decision per recommendation: a double-clicked Approve cannot create two approvals.
    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recommendations.id", ondelete="CASCADE"), unique=True
    )
    officer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[ApprovalDecision] = mapped_column(str_enum(ApprovalDecision, "decision"))
    edited_params: Mapped[Json | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = utc_now_column()


class DriverInstruction(Base):
    __tablename__ = "driver_instructions"

    id: Mapped[uuid.UUID] = uuid_pk()
    instruction_id: Mapped[str] = mapped_column(String(64), unique=True)  # idempotency key
    # One instruction per approval (B7): a replayed or double-clicked dispatch can't send twice.
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), unique=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id"), index=True)
    driver_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    action_type: Mapped[ActionType] = mapped_column(str_enum(ActionType, "action_type"))
    params: Mapped[Json] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[InstructionStatus] = mapped_column(
        str_enum(InstructionStatus, "status"), server_default=InstructionStatus.SENT.value
    )
    sent_at: Mapped[datetime] = utc_now_column()
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InterventionOutcome(Base):
    """Measured ~15 min after dispatch; long-term memory for the Action Generator."""

    __tablename__ = "intervention_outcomes"

    id: Mapped[uuid.UUID] = uuid_pk()
    driver_instruction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("driver_instructions.id", ondelete="CASCADE"), unique=True
    )
    action_type: Mapped[ActionType] = mapped_column(str_enum(ActionType, "action_type"))
    measured_at: Mapped[datetime] = utc_now_column()
    headway_cv_before: Mapped[float | None]
    headway_cv_after: Mapped[float | None]
    avg_wait_minutes_before: Mapped[float | None]
    avg_wait_minutes_after: Mapped[float | None]
    bunching_resolved: Mapped[bool | None]
    metrics: Mapped[Json] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
