"""Past interventions as numeric cases for similarity retrieval (B22, decision 6)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, utc_now_column, uuid_pk
from app.db.enums import ActionType
from app.db.vector import Vector

CASE_FEATURES = 9  # app/harness/cases.py: FEATURE_NAMES


class Case(Base):
    """One dispatched intervention: the situation it was decided in (a normalised feature
    vector built from MCP observations only), what was done, and what it achieved."""

    __tablename__ = "cases"
    __table_args__ = (Index("ix_cases_route_created", "route_id", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    driver_instruction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("driver_instructions.id", ondelete="CASCADE"), unique=True
    )
    action_type: Mapped[ActionType] = mapped_column(str_enum(ActionType, "action_type"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    features: Mapped[list[float]] = mapped_column(Vector(CASE_FEATURES))
    feature_values: Mapped[dict[str, float]] = mapped_column(JSONB)  # the same, named and raw
    predicted_benefit_passenger_minutes: Mapped[float]
    actual_benefit_passenger_minutes: Mapped[float | None]  # filled when the outcome is measured
    bunching_resolved: Mapped[bool | None]
    created_at: Mapped[datetime] = utc_now_column()
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_kind: Mapped[str] = mapped_column(String(16), server_default="actual")
