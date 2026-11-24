"""Scenario library (B5): golden scenarios (from committed files) and LLM-generated ones."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, utc_now_column, uuid_pk
from app.db.enums import ScenarioSource, ScenarioStatus


class ScenarioRecord(Base):
    __tablename__ = "scenarios"
    __table_args__ = (
        Index("ix_scenarios_created", text("created_at DESC")),
        # A golden scenario is stored once, keyed by its file name.
        Index("uq_scenarios_golden_key", "golden_key", unique=True),
        CheckConstraint("source <> 'golden' OR golden_key IS NOT NULL", name="golden_has_key"),
        CheckConstraint("status <> 'ready' OR plan IS NOT NULL", name="ready_has_plan"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    source: Mapped[ScenarioSource] = mapped_column(str_enum(ScenarioSource, "source"))
    golden_key: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)  # the plain-English request
    status: Mapped[ScenarioStatus] = mapped_column(str_enum(ScenarioStatus, "status"))
    seed: Mapped[int]
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # a validated ScenarioPlan
    preview: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # simulator: vs a normal day
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    problems: Mapped[list[str] | None] = mapped_column(JSONB)  # last attempt's, if it failed
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = utc_now_column()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
