"""Run traces and metrics (B18), from the app's own tables (decision 5: the source of truth).

`run_trace` rebuilds one run as a timeline: every agent step with the tool calls and LLM calls
it made, the officer's decisions, the instruction and the measured outcome. `metrics`
aggregates a time window: workflow outcomes and durations, tool latency / failures / retries,
and tokens and cost per agent, model and run. The API and `generate_report.py` share both.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AgentStep,
    Approval,
    BunchingEvent,
    DriverInstruction,
    InterventionOutcome,
    LlmUsage,
    Recommendation,
    ToolCallRecord,
    Vehicle,
    WorkflowRun,
)
from app.officer.status import run_messages

RULES_LEVEL = 4


# ------------------------------------------------------------------ trace


class ToolCallOut(BaseModel):
    agent: str
    tool: str
    status: str
    error_code: str | None
    attempts: int
    duration_ms: int
    at: datetime


class LlmCallOut(BaseModel):
    agent: str
    model: str
    fallback_level: int
    success: bool
    error_kind: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    at: datetime


class StepOut(BaseModel):
    index: int
    node: str
    status: str
    routing: str | None
    started_at: datetime
    duration_ms: int | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    error: str | None
    tool_calls: list[ToolCallOut]
    llm_calls: list[LlmCallOut]


class DecisionOut(BaseModel):
    decision: str
    reason: str | None
    edited_params: dict[str, Any] | None
    at: datetime


class TraceTotals(BaseModel):
    steps: int
    tool_calls: int
    tool_failures: int
    retries: int
    llm_calls: int
    llm_fallbacks_to_rules: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class RunTrace(BaseModel):
    run_id: uuid.UUID
    status: str
    end_reason: str | None
    waiting_for: str | None
    event_kind: str | None
    pair: dict[str, str | None]
    started_at: datetime
    finished_at: datetime | None
    duration_s: float | None
    status_messages: list[str]
    steps: list[StepOut]
    unlinked_tool_calls: list[ToolCallOut]  # made outside a recorded step (e.g. a send guard)
    decisions: list[DecisionOut]
    instructions: list[dict[str, Any]]
    outcome: dict[str, Any] | None
    totals: TraceTotals


def _tool(r: ToolCallRecord) -> ToolCallOut:
    return ToolCallOut(
        agent=r.agent, tool=r.tool, status=r.status, error_code=r.error_code,
        attempts=r.attempts, duration_ms=r.duration_ms, at=r.created_at,
    )  # fmt: skip


def _llm(r: LlmUsage) -> LlmCallOut:
    return LlmCallOut(
        agent=r.agent, model=r.model, fallback_level=r.fallback_level, success=r.success,
        error_kind=r.error_kind, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
        cost_usd=float(r.cost_usd), latency_ms=r.latency_ms, at=r.created_at,
    )  # fmt: skip


async def run_trace(session: AsyncSession, run_id: uuid.UUID) -> RunTrace | None:
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return None
    steps = list(
        await session.scalars(
            select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.step_index)
        )
    )
    tools = list(
        await session.scalars(
            select(ToolCallRecord)
            .where(ToolCallRecord.run_id == run_id)
            .order_by(ToolCallRecord.created_at, ToolCallRecord.id)
        )
    )
    llms = list(
        await session.scalars(
            select(LlmUsage).where(LlmUsage.run_id == run_id).order_by(LlmUsage.id)
        )
    )
    tools_by_step: dict[int | None, list[ToolCallOut]] = defaultdict(list)
    for t in tools:
        tools_by_step[t.step_index].append(_tool(t))
    llms_by_step: dict[int | None, list[LlmCallOut]] = defaultdict(list)
    for m in llms:
        llms_by_step[m.step_index].append(_llm(m))

    event = (
        await session.get(BunchingEvent, run.bunching_event_id) if run.bunching_event_id else None
    )
    fleets: dict[uuid.UUID, str] = {}
    if event is not None:
        rows = await session.execute(
            select(Vehicle.id, Vehicle.fleet_number).where(
                Vehicle.id.in_([event.leader_vehicle_id, event.follower_vehicle_id])
            )
        )
        fleets = dict(rows.tuples().all())
    approvals = (
        (
            await session.execute(
                select(Approval)
                .join(Recommendation, Recommendation.id == Approval.recommendation_id)
                .where(Recommendation.run_id == run_id)
                .order_by(Approval.decided_at)
            )
        )
        .scalars()
        .all()
    )
    sent = (
        await session.execute(
            select(DriverInstruction, Vehicle.fleet_number)
            .join(Approval, Approval.id == DriverInstruction.approval_id)
            .join(Recommendation, Recommendation.id == Approval.recommendation_id)
            .join(Vehicle, Vehicle.id == DriverInstruction.vehicle_id)
            .where(Recommendation.run_id == run_id)
            .order_by(DriverInstruction.sent_at)
        )
    ).all()
    outcome_row = None
    if sent:
        outcome_row = await session.scalar(
            select(InterventionOutcome).where(
                InterventionOutcome.driver_instruction_id == sent[-1][0].id
            )
        )
    all_llm = [_llm(m) for m in llms]
    return RunTrace(
        run_id=run.id,
        status=run.status.value,
        end_reason=run.end_reason,
        waiting_for=run.waiting_for,
        event_kind=event.kind.value if event is not None else None,
        pair={
            "leader": fleets.get(event.leader_vehicle_id) if event else None,
            "follower": fleets.get(event.follower_vehicle_id) if event else None,
        },
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_s=(
            round((run.finished_at - run.started_at).total_seconds(), 1)
            if run.finished_at
            else None
        ),
        status_messages=run_messages(run.degraded),
        steps=[
            StepOut(
                index=s.step_index,
                node=s.node,
                status=s.status.value,
                routing=s.routing_decision,
                started_at=s.started_at,
                duration_ms=s.duration_ms,
                input=s.input_summary,
                output=s.output_summary,
                error=s.error,
                tool_calls=tools_by_step.get(s.step_index, []),
                llm_calls=llms_by_step.get(s.step_index, []),
            )
            for s in steps
        ],
        unlinked_tool_calls=tools_by_step.get(None, []),
        decisions=[
            DecisionOut(
                decision=a.decision.value,
                reason=a.reason,
                edited_params=a.edited_params,
                at=a.decided_at,
            )
            for a in approvals
        ],
        instructions=[
            {
                "instruction_id": i.instruction_id,
                "fleet_number": fleet,
                "action_type": i.action_type.value,
                "params": i.params,
                "status": i.status.value,
                "sent_at": i.sent_at.isoformat(),
            }
            for i, fleet in sent
        ],
        outcome=(
            {
                "predicted_benefit_passenger_minutes": (
                    outcome_row.predicted_benefit_passenger_minutes
                ),
                "actual_benefit_passenger_minutes": outcome_row.actual_benefit_passenger_minutes,
                "bunching_resolved": outcome_row.bunching_resolved,
            }
            if outcome_row is not None
            else None
        ),
        totals=TraceTotals(
            steps=len(steps),
            tool_calls=len(tools),
            tool_failures=sum(1 for t in tools if t.status != "ok"),
            retries=sum(max(0, t.attempts - 1) for t in tools),
            llm_calls=sum(1 for m in all_llm if m.fallback_level != RULES_LEVEL),
            llm_fallbacks_to_rules=sum(1 for m in all_llm if m.fallback_level == RULES_LEVEL),
            input_tokens=sum(m.input_tokens for m in all_llm),
            output_tokens=sum(m.output_tokens for m in all_llm),
            cost_usd=round(sum(m.cost_usd for m in all_llm), 6),
        ),
    )


# ------------------------------------------------------------------ metrics


class Percentiles(BaseModel):
    p50: float | None
    p95: float | None
    max: float | None


class RunMetrics(BaseModel):
    total: int
    finished: int
    by_status: dict[str, int]
    by_end_reason: dict[str, int]
    success_rate: float | None  # finished without failing or timing out
    failure_rate: float | None
    duration_s: Percentiles  # finished runs, start to finish (includes waiting for people)
    execution_s: Percentiles  # time the agents actually ran (sum of step durations)


class ToolMetrics(BaseModel):
    tool: str
    calls: int
    failures: int
    failure_rate: float
    retries: int
    latency_ms: Percentiles
    errors: dict[str, int]


class LlmAgentMetrics(BaseModel):
    agent: str
    calls: int  # model calls (attempts), excluding rules answers
    successes: int
    fallbacks_to_rules: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LlmModelMetrics(BaseModel):
    model: str
    calls: int
    successes: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: Percentiles


class Metrics(BaseModel):
    since: datetime
    until: datetime
    runs: RunMetrics
    tools: list[ToolMetrics]
    llm_by_agent: list[LlmAgentMetrics]
    llm_by_model: list[LlmModelMetrics]
    tokens_per_run: float | None
    cost_per_run_usd: float | None


def _pct(values: list[float]) -> Percentiles:
    if not values:
        return Percentiles(p50=None, p95=None, max=None)
    ordered = sorted(values)

    def at(q: float) -> float:
        k = (len(ordered) - 1) * q
        lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 1)

    return Percentiles(p50=at(0.5), p95=at(0.95), max=round(ordered[-1], 1))


FAILED = {"failed", "timed_out"}


async def metrics(session: AsyncSession, hours: float = 24.0) -> Metrics:
    until = datetime.now(UTC)
    since = until - timedelta(hours=hours)
    runs = list(await session.scalars(select(WorkflowRun).where(WorkflowRun.started_at >= since)))
    finished = [r for r in runs if r.finished_at is not None]
    by_status: dict[str, int] = defaultdict(int)
    by_reason: dict[str, int] = defaultdict(int)
    for r in runs:
        by_status[r.status.value] += 1
        if r.end_reason:
            by_reason[r.end_reason] += 1
    failed = sum(1 for r in finished if r.status.value in FAILED)
    step_rows = (
        await session.execute(
            select(AgentStep.run_id, func.sum(AgentStep.duration_ms))
            .join(WorkflowRun, WorkflowRun.id == AgentStep.run_id)
            .where(WorkflowRun.started_at >= since)
            .group_by(AgentStep.run_id)
        )
    ).all()

    tool_rows = list(
        await session.scalars(select(ToolCallRecord).where(ToolCallRecord.created_at >= since))
    )
    per_tool: dict[str, list[ToolCallRecord]] = defaultdict(list)
    for t in tool_rows:
        per_tool[t.tool].append(t)
    tools = []
    for tool, calls in sorted(per_tool.items()):
        failures = [c for c in calls if c.status != "ok"]
        errors: dict[str, int] = defaultdict(int)
        for c in failures:
            errors[f"{c.status}:{c.error_code}" if c.error_code else c.status] += 1
        tools.append(
            ToolMetrics(
                tool=tool,
                calls=len(calls),
                failures=len(failures),
                failure_rate=round(len(failures) / len(calls), 3),
                retries=sum(max(0, c.attempts - 1) for c in calls),
                latency_ms=_pct([float(c.duration_ms) for c in calls]),
                errors=dict(errors),
            )
        )

    llm_rows = list(await session.scalars(select(LlmUsage).where(LlmUsage.created_at >= since)))
    agents: dict[str, list[LlmUsage]] = defaultdict(list)
    models: dict[str, list[LlmUsage]] = defaultdict(list)
    for m in llm_rows:
        agents[m.agent].append(m)
        if m.fallback_level != RULES_LEVEL:
            models[m.model].append(m)
    run_ids = {m.run_id for m in llm_rows if m.run_id is not None}
    total_tokens = sum(m.input_tokens + m.output_tokens for m in llm_rows if m.run_id)
    total_cost = sum(float(m.cost_usd) for m in llm_rows if m.run_id)
    return Metrics(
        since=since,
        until=until,
        runs=RunMetrics(
            total=len(runs),
            finished=len(finished),
            by_status=dict(by_status),
            by_end_reason=dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
            success_rate=round(1 - failed / len(finished), 3) if finished else None,
            failure_rate=round(failed / len(finished), 3) if finished else None,
            duration_s=_pct(
                [(r.finished_at - r.started_at).total_seconds() for r in finished if r.finished_at]
            ),
            execution_s=_pct([float(ms or 0) / 1000 for _, ms in step_rows]),
        ),
        tools=tools,
        llm_by_agent=[
            LlmAgentMetrics(
                agent=agent,
                calls=sum(1 for m in rows if m.fallback_level != RULES_LEVEL),
                successes=sum(1 for m in rows if m.success and m.fallback_level != RULES_LEVEL),
                fallbacks_to_rules=sum(1 for m in rows if m.fallback_level == RULES_LEVEL),
                input_tokens=sum(m.input_tokens for m in rows),
                output_tokens=sum(m.output_tokens for m in rows),
                cost_usd=round(sum(float(m.cost_usd) for m in rows), 6),
            )
            for agent, rows in sorted(agents.items())
        ],
        llm_by_model=[
            LlmModelMetrics(
                model=model,
                calls=len(rows),
                successes=sum(1 for m in rows if m.success),
                input_tokens=sum(m.input_tokens for m in rows),
                output_tokens=sum(m.output_tokens for m in rows),
                cost_usd=round(sum(float(m.cost_usd) for m in rows), 6),
                latency_ms=_pct([float(m.latency_ms) for m in rows]),
            )
            for model, rows in sorted(models.items())
        ],
        tokens_per_run=round(total_tokens / len(run_ids), 1) if run_ids else None,
        cost_per_run_usd=round(total_cost / len(run_ids), 6) if run_ids else None,
    )


__all__ = ["Metrics", "RunTrace", "metrics", "run_trace"]
