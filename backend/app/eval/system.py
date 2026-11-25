"""Whole-system evaluation (B19): a scenario played through the real stack, auto-approved.

The world runner writes GPS fixes and stop counts to Postgres and opens bunching events; each
event starts the real LangGraph workflow (harness -> MCP servers in-process -> database); the
officer's step is the real decision service (auto-approve: the top recommendation); dispatch
sends through `send_driver_instruction` with all its guards; the driver is auto-acknowledged
and the world carries the action out; ~15 world-minutes later the outcome is measured. The
world runs on a fast-forwarded clock that the MCP servers share, so GPS freshness is judged
on world time.

Scores: interventions sent, sends refused by the guards, rulebook violations among sent
instructions, predicted vs actual benefit (from `intervention_outcomes`) and LLM cost.
"""

import statistics
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from mcp import Client
from mcp.types import CallToolResult, Tool
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.rulebook import RuleContext, violations
from app.config import Settings
from app.db.enums import InstructionStatus
from app.db.models import (
    Approval,
    DriverInstruction,
    InterventionOutcome,
    LlmUsage,
    Recommendation,
    ToolCallRecord,
    User,
    Vehicle,
)
from app.graph.state import Candidate
from app.graph.workflow import RunResult, resume_workflow, start_workflow
from app.harness.client import Transient
from app.harness.harness import ToolHarness
from app.llm.gateway import LlmGateway
from app.officer import decisions
from app.officer.decisions import OfficerDecisionPayload
from app.runner.route_loader import WorldRoute
from app.runner.world import WorldRunner
from mcp_servers import observations as mcp_observations
from mcp_servers.avl_mcp.server import server as avl_server
from mcp_servers.demand_mcp.server import server as demand_server

TICK_S = 10.0
OUTCOME_WINDOW_S = 900.0


class EvalClock:
    """World time for the runner and the MCP servers alike."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 11, 24, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class InProcessMCP:
    """The harness's connector over the in-memory MCP transport (the real protocol)."""

    def __init__(self, down: Sequence[str] = ()) -> None:
        self.servers = {"avl": avl_server, "demand": demand_server}
        self.down = set(down)

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> CallToolResult:
        if server in self.down:
            raise Transient(f"{server} is down")
        async with Client(self.servers[server]) as client:
            return await client.call_tool(tool, arguments)

    async def list_tools(self, server: str, timeout_s: float) -> list[Tool]:
        async with Client(self.servers[server]) as client:
            return list((await client.list_tools()).tools)


class _CapturedQueue:
    """Stands in for the job queue where the system would defer work to a worker: the
    evaluation runs those steps itself, straight away and in order."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("resume_workflow", "measure_outcome", "driver_ack_timeout"):
            self.jobs.append((name, args))
            return None
        return await self._real.enqueue_job(name, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def hard_rule_violations(
    instruction: DriverInstruction, route: WorldRoute, world_time_s: float
) -> list[str]:
    """The rulebook's hard rules (R1-R4) re-checked on what was actually sent. Cooldowns (R5)
    are left out: the instruction itself would trip them."""
    candidate = Candidate(
        key=instruction.instruction_id,
        action_type=instruction.action_type.value,
        vehicle_id=instruction.vehicle_id,
        fleet_number=None,
        params=dict(instruction.params),
        rationale="sent",
    )
    model = route.model
    ctx = RuleContext(
        route_id=model.route_id,
        world_time_s=world_time_s,
        now=datetime.now(UTC),
        stop_names={s.stop_id: s.name for d in model.directions for s in d.stops},
        last_departure_s={d.direction_id: 24 * 3600 for d in model.directions},
        headway_s={d.direction_id: float(d.scheduled_headway_s) for d in model.directions},
    )
    return [v for v in violations(candidate, ctx, None) if not v.startswith("R5")]


@dataclass
class SystemEvalResult:
    minutes: float
    events: int = 0
    runs: int = 0
    recommendations: int = 0
    approved: int = 0
    instructions_sent: int = 0
    sends_refused: int = 0
    rule_violations_sent: int = 0
    outcomes_measured: int = 0
    predicted_vs_actual: list[tuple[float, float]] = field(default_factory=list)
    llm_cost_usd: float = 0.0
    llm_tokens: int = 0
    end_reasons: dict[str, int] = field(default_factory=dict)
    run_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def mean_abs_error(self) -> float | None:
        if not self.predicted_vs_actual:
            return None
        return round(statistics.fmean(abs(p - (a or 0)) for p, a in self.predicted_vs_actual), 1)


async def run_system_eval(
    route: WorldRoute,
    runner_factory: Any,
    *,
    minutes: float,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    job_queue: Any,
    checkpointer: BaseCheckpointSaver[Any],
    officer_email: str,
    clock: EvalClock,
    llm: LlmGateway | None = None,
    monkeypatch_clock: Any = None,
) -> SystemEvalResult:
    """Play `minutes` of world time. `runner_factory(enqueue)` builds the WorldRunner (already
    set up on the scenario's world, using `clock`)."""
    if monkeypatch_clock is not None:
        monkeypatch_clock(mcp_observations, "utc_now", clock)
    harness = ToolHarness.from_settings(settings, sessionmaker, InProcessMCP())
    queue: Any = _CapturedQueue(job_queue)  # an ArqRedis to the code it's handed to
    raised: list[uuid.UUID] = []

    async def enqueue(event_id: uuid.UUID) -> None:
        raised.append(event_id)

    runner: WorldRunner = await runner_factory(enqueue)
    async with sessionmaker() as session:
        officer = await session.scalar(select(User).where(User.email == officer_email))
    if officer is None:
        raise RuntimeError(f"no officer account {officer_email!r} for auto-approval")
    result = SystemEvalResult(minutes=minutes)
    waiting_outcome: dict[uuid.UUID, float] = {}  # run -> acked world time

    def record(r: RunResult) -> None:
        if r.end_reason:
            result.end_reasons[r.end_reason] = result.end_reasons.get(r.end_reason, 0) + 1

    async def advance_run(r: RunResult) -> None:
        """Take a paused run through the human steps: approve, acknowledge, measure later."""
        while r.run_id is not None and r.waiting_for in ("officer", "driver"):
            if r.waiting_for == "officer":
                result.recommendations += 1
                async with sessionmaker() as session:
                    rec = await decisions.open_recommendation(session, r.run_id)
                    if rec is None:
                        return
                    queue.jobs.clear()
                    await decisions.decide(
                        session, queue, rec.id, OfficerDecisionPayload(decision="approve"), officer
                    )
                result.approved += 1
                r = await _drain(queue, r)
            elif r.waiting_for == "driver":
                run_id = r.run_id
                if run_id is None:
                    return
                world_s = runner.sim.state.time_s
                async with sessionmaker() as session, session.begin():
                    sent = await session.scalar(
                        select(DriverInstruction)
                        .where(DriverInstruction.status == InstructionStatus.SENT)
                        .order_by(DriverInstruction.sent_at.desc())
                    )
                    if sent is None:
                        return
                    sent.status, sent.acknowledged_at = InstructionStatus.ACKNOWLEDGED, clock()
                    fleet = await session.scalar(
                        select(Vehicle.fleet_number).where(Vehicle.id == sent.vehicle_id)
                    )
                    item = {
                        "instruction_id": sent.instruction_id,
                        "fleet_number": fleet,
                        "action_type": sent.action_type.value,
                        "params": sent.params,
                    }
                runner.apply_instruction(item)
                value = {
                    "kind": "ack",
                    "instruction_id": item["instruction_id"],
                    "world_time_s": world_s,
                }
                r = await resume_workflow(
                    run_id, "driver", value, harness, sessionmaker, settings, queue,
                    checkpointer, llm,
                )  # fmt: skip
                record(r)
                if r.waiting_for == "outcome" and r.run_id is not None:
                    waiting_outcome[r.run_id] = world_s
                return
        record(r)

    async def _drain(q: Any, r: RunResult) -> RunResult:
        for name, args in list(q.jobs):
            q.jobs.remove((name, args))
            if name == "resume_workflow":
                run_id, kind, value = args
                r = await resume_workflow(
                    uuid.UUID(run_id), kind, value, harness, sessionmaker, settings, q,
                    checkpointer, llm,
                )  # fmt: skip
        return r

    elapsed = 0.0
    while elapsed < minutes * 60:
        clock.advance(TICK_S)
        await runner.tick(TICK_S)
        elapsed += TICK_S
        while raised:
            event_id = raised.pop(0)
            result.events += 1
            r = await start_workflow(
                event_id, harness, sessionmaker, settings, queue, checkpointer, llm
            )
            if r.run_id is not None:
                result.runs += 1
                result.run_ids.append(r.run_id)
                await advance_run(r)
        world_s = runner.sim.state.time_s
        for run_id, acked in list(waiting_outcome.items()):
            if world_s - acked >= OUTCOME_WINDOW_S:
                del waiting_outcome[run_id]
                r = await resume_workflow(
                    run_id, "outcome", {"kind": "measure"}, harness, sessionmaker, settings,
                    queue, checkpointer, llm,
                )  # fmt: skip
                record(r)

    # Everything is counted by the runs this evaluation started: the world (and so the send
    # guard's timestamps) runs on the evaluation clock, not the wall clock.
    ids = result.run_ids or [uuid.uuid4()]
    async with sessionmaker() as session:
        sent = (
            await session.scalars(
                select(DriverInstruction)
                .join(Approval, Approval.id == DriverInstruction.approval_id)
                .join(Recommendation, Recommendation.id == Approval.recommendation_id)
                .where(Recommendation.run_id.in_(ids))
            )
        ).all()
        result.instructions_sent = len(sent)
        result.rule_violations_sent = sum(
            1 for i in sent if hard_rule_violations(i, route, runner.sim.state.time_s)
        )
        result.sends_refused = int(
            await session.scalar(
                select(func.count())
                .select_from(ToolCallRecord)
                .where(
                    ToolCallRecord.run_id.in_(ids),
                    ToolCallRecord.tool == "send_driver_instruction",
                    ToolCallRecord.status != "ok",
                )
            )
            or 0
        )
        outcomes = (
            await session.execute(
                select(
                    InterventionOutcome.predicted_benefit_passenger_minutes,
                    InterventionOutcome.actual_benefit_passenger_minutes,
                ).where(InterventionOutcome.driver_instruction_id.in_([i.id for i in sent] or ids))
            )
        ).all()
        result.outcomes_measured = len(outcomes)
        result.predicted_vs_actual = [(float(p or 0), float(a or 0)) for p, a in outcomes]
        cost, tokens = (
            await session.execute(
                select(
                    func.coalesce(func.sum(LlmUsage.cost_usd), Decimal(0)),
                    func.coalesce(func.sum(LlmUsage.input_tokens + LlmUsage.output_tokens), 0),
                ).where(LlmUsage.run_id.in_(ids))
            )
        ).one()
        result.llm_cost_usd, result.llm_tokens = float(cost), int(tokens)
    return result
