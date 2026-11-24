"""The advisor workflow as one LangGraph graph, and how a run is started and finished.

    START -> headway_monitor --stale--------> END  (flagged; nobody instructed)
                             --not_bunched--> END
                             --generate-----> action_generator -> impact_simulator
             impact_simulator --Send x N--> simulate (one branch per candidate, in parallel)
             simulate -> control_advisor --recommend | escalate | do_nothing--> END

Limits (rule 8): graph step budget, whole-run time limit, per-simulation timeout and a
concurrency semaphore. One active run per bus pair; repeated events are deduplicated.
Every node writes an agent_steps row and every line is logged with the run_id (rule 9).
B12 adds the officer approval interrupt and dispatch after control_advisor.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from arq import ArqRedis
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.action_generator import action_generator
from app.agents.control_advisor import control_advisor
from app.agents.headway_monitor import headway_monitor
from app.agents.impact_simulator import impact_simulator, simulate
from app.config import Settings
from app.db.enums import BunchingEventStatus, WorkflowStatus
from app.db.models import BunchingEvent, RouteDirection, WorkflowRun
from app.graph.deps import WorkflowDeps, recorded
from app.graph.state import WorkflowState
from app.harness.budget import RunBudget
from app.harness.errors import BudgetExceeded, HarnessError
from app.harness.harness import ToolHarness
from app.runner.route_loader import RouteNotImported, load_world_route

log = structlog.get_logger()

ACTIVE = (WorkflowStatus.RUNNING, WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.ESCALATED)

# end_reason -> (run status, event status)
OUTCOMES: dict[str, tuple[WorkflowStatus, BunchingEventStatus]] = {
    "recommended": (WorkflowStatus.AWAITING_APPROVAL, BunchingEventStatus.HANDLING),
    "escalated": (WorkflowStatus.ESCALATED, BunchingEventStatus.HANDLING),
    "do_nothing_recommended": (WorkflowStatus.COMPLETED, BunchingEventStatus.HANDLING),
    "not_bunched": (WorkflowStatus.COMPLETED, BunchingEventStatus.RESOLVED),
    "stale_data": (WorkflowStatus.COMPLETED, BunchingEventStatus.OPEN),  # retried later
}


# ------------------------------------------------------------------ step summaries (agent_steps)


def _in(state: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "candidate" in state:
        out["candidate"] = state["candidate"].key
    if "candidates" in state:
        out["candidates"] = [c.key for c in state["candidates"]]
    if "sim_outcomes" in state:
        out["outcomes"] = len(state["sim_outcomes"])
    return out


def _out(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "snapshot" in result:
        pair = result["snapshot"].pair
        out["pair"] = {
            "leader": pair.leader_fleet_number,
            "follower": pair.follower_fleet_number,
            "headway_s": pair.headway_seconds,
            "scheduled_s": pair.scheduled_headway_seconds,
            "status": pair.status,
        }
    if "candidates" in result:
        out["candidates"] = [c.key for c in result["candidates"]]
        out["dropped"] = [d.model_dump() for d in result.get("dropped", [])]
    for o in result.get("sim_outcomes", []):
        out["outcome"] = o.model_dump(
            include={
                "candidate_key",
                "status",
                "passenger_wait_minutes",
                "bunching_count",
                "error",
                "duration_ms",
            }
        )
    if "recommendation" in result:
        r = result["recommendation"]
        out["recommendation"] = {
            "candidate": r.candidate_key,
            "benefit": r.benefit_passenger_minutes,
            "near_tie": r.is_near_tie,
        }
    if "end_reason" in result:
        out["end_reason"] = result["end_reason"]
    if result.get("degraded"):
        out["degraded"] = result["degraded"]
    return out


# ------------------------------------------------------------------ graph


def fan_out(state: WorkflowState) -> list[Send]:
    return [Send("simulate", {"candidate": c}) for c in state.get("candidates", [])]


def build_graph(deps: WorkflowDeps) -> Any:
    g: StateGraph[WorkflowState] = StateGraph(WorkflowState)

    def node(name: str, fn: Any) -> None:
        g.add_node(name, recorded(name, fn, deps, _in, _out))  # type: ignore[call-overload]

    node("headway_monitor", headway_monitor)
    node("action_generator", action_generator)
    node("impact_simulator", impact_simulator)
    node("simulate", simulate)
    node("control_advisor", control_advisor)
    g.add_edge(START, "headway_monitor")
    g.add_conditional_edges(
        "headway_monitor",
        lambda s: s["route"],
        {"stale": END, "not_bunched": END, "generate": "action_generator"},
    )
    g.add_edge("action_generator", "impact_simulator")
    g.add_conditional_edges("impact_simulator", fan_out, ["simulate"])
    g.add_edge("simulate", "control_advisor")
    g.add_edge("control_advisor", END)
    return g.compile()


# ------------------------------------------------------------------ runs


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID | None  # None: not started (deduplicated / event not open)
    status: str
    end_reason: str | None
    state: dict[str, Any] | None = None


def _seconds(gtfs_time: str) -> int:
    h, m, s = (int(x) for x in gtfs_time.split(":"))
    return h * 3600 + m * 60 + s


async def _claim(
    sessionmaker: async_sessionmaker[AsyncSession], event_id: uuid.UUID
) -> tuple[WorkflowRun, BunchingEvent] | str:
    """Create the run if the event is open and its bus pair has no active run (atomic)."""
    async with sessionmaker() as session, session.begin():
        event = await session.scalar(
            select(BunchingEvent).where(BunchingEvent.id == event_id).with_for_update()
        )
        if event is None:
            return "event_not_found"
        if event.status != BunchingEventStatus.OPEN:
            return "event_not_open"
        busy = await session.scalar(
            select(WorkflowRun.id)
            .join(BunchingEvent, BunchingEvent.id == WorkflowRun.bunching_event_id)
            .where(
                BunchingEvent.leader_vehicle_id == event.leader_vehicle_id,
                BunchingEvent.follower_vehicle_id == event.follower_vehicle_id,
                WorkflowRun.status.in_(ACTIVE),
            )
            .limit(1)
        )
        if busy is not None:
            return "pair_already_handled"
        run = WorkflowRun(bunching_event_id=event.id, status=WorkflowStatus.RUNNING)
        session.add(run)
        event.status = BunchingEventStatus.HANDLING
        await session.flush()
        return run, event


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    status: WorkflowStatus,
    event_status: BunchingEventStatus,
    end_reason: str,
    steps: int,
    error: str | None = None,
) -> None:
    async with sessionmaker() as session, session.begin():
        run = await session.get(WorkflowRun, run_id)
        event = await session.get(BunchingEvent, event_id)
        if run is not None:
            run.status, run.end_reason, run.error = status, end_reason, error
            run.step_count, run.finished_at = steps, datetime.now(UTC)
        if event is not None and event.status == BunchingEventStatus.HANDLING:
            event.status = event_status
            if event_status == BunchingEventStatus.RESOLVED:
                event.resolved_at = datetime.now(UTC)


async def start_workflow(
    event_id: uuid.UUID,
    harness: ToolHarness,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_queue: ArqRedis,
) -> RunResult:
    claimed = await _claim(sessionmaker, event_id)
    if isinstance(claimed, str):
        log.info("workflow_skipped", event_id=str(event_id), reason=claimed)
        return RunResult(None, "skipped", claimed)
    run, event = claimed
    run_id = run.id
    with structlog.contextvars.bound_contextvars(run_id=str(run_id), event_id=str(event_id)):
        log.info("workflow_started", route_id=event.route_id)
        budget = RunBudget.from_settings(run_id, settings)
        try:
            async with sessionmaker() as session:
                route = await load_world_route(session, event.route_id)
                last = {
                    d.direction_id: _seconds(d.last_departure)
                    for d in await session.scalars(
                        select(RouteDirection).where(RouteDirection.route_id == event.route_id)
                    )
                }
            deps = WorkflowDeps(
                run_id=str(run_id),
                harness=harness,
                budget=budget,
                sessionmaker=sessionmaker,
                settings=settings,
                route=route,
                last_departure_s=last,
                sim_semaphore=asyncio.Semaphore(settings.sim_concurrency),
                job_queue=job_queue,
            )
            initial: WorkflowState = {
                "run_id": str(run_id),
                "event_id": str(event_id),
                "route_id": event.route_id,
                "leader_vehicle_id": str(event.leader_vehicle_id),
                "follower_vehicle_id": str(event.follower_vehicle_id),
            }
            final = await asyncio.wait_for(
                build_graph(deps).ainvoke(
                    initial, config={"recursion_limit": settings.max_graph_steps + 5}
                ),
                timeout=settings.run_time_limit_seconds,
            )
        except TimeoutError:
            return await _failed(
                sessionmaker,
                run_id,
                event_id,
                WorkflowStatus.TIMED_OUT,
                "run_time_limit",
                budget.steps,
            )
        except BudgetExceeded as exc:
            return await _failed(
                sessionmaker,
                run_id,
                event_id,
                WorkflowStatus.FAILED,
                f"budget:{exc.code}",
                budget.steps,
                str(exc),
            )
        except HarnessError as exc:
            return await _failed(
                sessionmaker,
                run_id,
                event_id,
                WorkflowStatus.FAILED,
                f"tools:{exc.kind}:{exc.tool}",
                budget.steps,
                str(exc),
            )
        except RouteNotImported as exc:
            return await _failed(
                sessionmaker,
                run_id,
                event_id,
                WorkflowStatus.FAILED,
                "route_not_imported",
                0,
                str(exc),
            )
        end_reason = final.get("end_reason") or "not_bunched"
        status, event_status = OUTCOMES[end_reason]
        await _finish(
            sessionmaker, run_id, event_id, status, event_status, end_reason, budget.steps
        )
        log.info(
            "workflow_finished", status=status.value, end_reason=end_reason, steps=budget.steps
        )
        return RunResult(run_id, status.value, end_reason, dict(final))


async def _failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    status: WorkflowStatus,
    reason: str,
    steps: int,
    error: str | None = None,
) -> RunResult:
    # The event goes back to OPEN so a later run can try again.
    await _finish(
        sessionmaker, run_id, event_id, status, BunchingEventStatus.OPEN, reason, steps, error
    )
    log.warning("workflow_failed", status=status.value, end_reason=reason, error=error)
    return RunResult(run_id, status.value, reason)
