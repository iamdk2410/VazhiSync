"""Tool-call telemetry (B18): every harness call becomes a `tool_calls` row and a Langfuse span.

The row carries the outcome, the attempts (so retries are countable) and the latency, plus the
agent step that made the call (app/observability/tracing.py). Telemetry is best effort: a
failure to write it is logged and never fails the call.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ToolCallRecord
from app.llm.langfuse import get_tracer
from app.observability.tracing import current_step

log = structlog.get_logger()


@dataclass(frozen=True)
class ToolCallEvent:
    run_id: uuid.UUID
    agent: str
    tool: str
    server: str | None
    status: str  # ok, or the HarnessError kind
    error_code: str | None
    attempts: int
    duration_ms: int
    started_at: datetime
    arguments: dict[str, Any] = field(default_factory=dict)


class ToolTelemetry:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None) -> None:
        self._sessionmaker = sessionmaker

    async def __call__(self, event: ToolCallEvent) -> None:
        step = current_step()
        step_index = step.index if step is not None and step.run_id == str(event.run_id) else None
        if self._sessionmaker is not None:
            try:
                async with self._sessionmaker() as session:
                    session.add(
                        ToolCallRecord(
                            run_id=event.run_id,
                            step_index=step_index,
                            agent=event.agent,
                            tool=event.tool,
                            server=event.server,
                            status=event.status,
                            error_code=event.error_code,
                            attempts=event.attempts,
                            duration_ms=event.duration_ms,
                        )
                    )
                    await session.commit()
            except SQLAlchemyError as exc:
                log.warning("tool_call_not_recorded", tool=event.tool, error=repr(exc)[:200])
        tracer = get_tracer()
        if tracer is not None:
            tracer.span(
                run_id=str(event.run_id),
                name=f"{event.agent}:{event.tool}",
                started=event.started_at,
                ended=datetime.now(UTC),
                input=event.arguments,
                output={"status": event.status, "attempts": event.attempts},
                error=None if event.status == "ok" else f"{event.status}:{event.error_code}",
                metadata={"server": event.server, "duration_ms": event.duration_ms},
            )
