"""Per-run dependencies of the graph nodes, and the step recorder (agent_steps, rule 9)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

import structlog
from arq import ArqRedis
from langgraph.errors import GraphInterrupt
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.enums import StepStatus
from app.db.models import AgentStep
from app.harness.budget import RunBudget
from app.harness.harness import OptionalResult, ToolHarness
from app.llm.gateway import LlmGateway
from app.llm.langfuse import get_tracer
from app.observability.tracing import observation_id, step_scope
from app.runner.route_loader import WorldRoute
from simulator import Observations

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# Common random numbers: every candidate sees the same seeds, so per-seed differences are fair.
SIM_SEEDS = (11, 22, 33, 44, 55)
SIM_HORIZON_MIN = 15.0


@dataclass
class WorkflowDeps:
    run_id: str
    harness: ToolHarness
    budget: RunBudget
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    route: WorldRoute
    last_departure_s: dict[int, int]
    sim_semaphore: asyncio.Semaphore
    seeds: tuple[int, ...] = SIM_SEEDS
    horizon_min: float = SIM_HORIZON_MIN
    observations: Observations | None = None  # built once by the Impact Simulator
    job_queue: ArqRedis | None = None  # simulation jobs (B11)
    sim_queue: str = "vazhisync:sims"
    first_step: int = 0  # steps already recorded for this run (a resumed run continues the count)
    llm: LlmGateway | None = None  # B9: None -> agents use their rules only
    _step: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._step = self.first_step

    async def tool(self, agent: str, tool: str, args: dict[str, Any], expect: type[T]) -> T:
        result = await self.harness.call(self.budget, agent, tool, args)
        if not isinstance(result, expect):
            raise TypeError(f"{tool} returned {type(result).__name__}, expected {expect.__name__}")
        return result

    async def optional_tool(self, agent: str, tool: str, args: dict[str, Any]) -> OptionalResult:
        return await self.harness.call_optional(self.budget, agent, tool, args)

    def next_step(self) -> int:
        self._step += 1
        return self._step

    async def record(
        self,
        index: int,
        node: str,
        status: StepStatus,
        *,
        routing: str | None,
        inputs: dict[str, Any],
        outputs: dict[str, Any] | None,
        error: str | None,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        async with self.sessionmaker() as session:
            session.add(
                AgentStep(
                    run_id=self.run_id,
                    step_index=index,
                    node=node,
                    routing_decision=routing,
                    status=status,
                    input_summary=inputs,
                    output_summary=outputs,
                    error=error,
                    started_at=started_at,
                    duration_ms=duration_ms,
                )
            )
            await session.commit()
        tracer = get_tracer()
        if tracer is not None:
            tracer.span(
                run_id=self.run_id,
                name=node,
                started=started_at,
                input=inputs,
                output=outputs,
                error=error,
                observation_id=observation_id(self.run_id, index),
                metadata={"step": index, "status": status.value, "routing": routing},
            )
        log.info(
            "agent_step",
            run_id=self.run_id,
            step=index,
            node=node,
            status=status.value,
            routing=routing,
            duration_ms=duration_ms,
            error=error,
        )


Node = Callable[[dict[str, Any], WorkflowDeps], Awaitable[dict[str, Any]]]
Summary = Callable[[dict[str, Any]], dict[str, Any]]


def recorded(
    name: str, fn: Node, deps: WorkflowDeps, summarize_in: Summary, summarize_out: Summary
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Wrap a node: charge the step budget, time it, and write its agent_steps row."""

    async def node(state: dict[str, Any]) -> dict[str, Any]:
        deps.budget.charge_step(name)
        index = deps.next_step()
        started_at, t0 = datetime.now(UTC), time.monotonic()
        inputs = summarize_in(state)
        try:
            with step_scope(deps.run_id, index, name):  # B18: tool / LLM calls link to this step
                out = await fn(state, deps)
        except GraphInterrupt:
            # The graph paused here (officer / driver / outcome); it resumes later from a
            # checkpoint and this node runs again from the top. Recorded as a pause, not an error.
            await deps.record(
                index,
                name,
                StepStatus.SKIPPED,
                routing="interrupt",
                inputs=inputs,
                outputs=None,
                error=None,
                started_at=started_at,
                duration_ms=round((time.monotonic() - t0) * 1000),
            )
            raise
        except Exception as exc:
            await deps.record(
                index,
                name,
                StepStatus.ERROR,
                routing=None,
                inputs=inputs,
                outputs=None,
                error=f"{type(exc).__name__}: {exc}"[:1000],
                started_at=started_at,
                duration_ms=round((time.monotonic() - t0) * 1000),
            )
            raise
        await deps.record(
            index,
            name,
            StepStatus.OK,
            routing=out.get("route"),
            inputs=inputs,
            outputs=summarize_out(out),
            error=None,
            started_at=started_at,
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
        return out

    return node
