"""Agent step log and LLM usage (rule 9: everything logged with a run_id)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, utc_now_column
from app.db.enums import StepStatus


class AgentStep(Base):
    __tablename__ = "agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_index"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    step_index: Mapped[int]
    node: Mapped[str] = mapped_column(String(64))
    routing_decision: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[StepStatus] = mapped_column(str_enum(StepStatus, "status"))
    input_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = utc_now_column()
    duration_ms: Mapped[int | None]


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_run_created", "run_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Nullable: scenario generation (B5) calls the LLM outside any workflow run.
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    agent: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    fallback_level: Mapped[int]  # 0 primary, 1 backup, 2 local, 3 rule-based
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int]
    success: Mapped[bool]
    error_kind: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = utc_now_column()
