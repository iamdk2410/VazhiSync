"""The advisor workflow as one LangGraph graph, and how a run is started, paused and resumed.

    START -> headway_monitor --stale / not_bunched--> END
                             --generate--> action_generator -> impact_simulator
             impact_simulator --Send x N--> simulate -> control_advisor
             control_advisor --do_nothing--> END
                             --recommend / escalate--> await_officer (interrupt)
             await_officer --approve--> dispatch --sent--> await_driver (interrupt)
                           --edit--> apply_edit -> impact_simulator (re-simulate)
                           --reject--> action_generator (next round)
                           --replan--> headway_monitor (expired: fresh data)
             await_driver --ack--> await_outcome (interrupt) -> measure -> END
                          --replan--> headway_monitor (can't comply / no ack)

The graph is checkpointed in Postgres (thread id = run id): a paused run survives restarts of
any process and is resumed by a worker job (`resume_workflow`). `workflow_runs.waiting_for`
says which interrupt it waits on; a resume of the wrong kind, or a second concurrent resume,
is refused under a row lock. Limits (rule 8): step budget and time limit per active segment,
per-simulation timeout, semaphore; at most MAX_ROUNDS re-plans. Every node writes agent_steps
and every log line carries the run_id (rule 9).
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from arq import ArqRedis
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.action_generator import action_generator
from app.agents.control_advisor import control_advisor
from app.agents.dispatch import (
    apply_edit,
    await_driver,
    await_officer,
    await_outcome,
    dispatch,
    measure,
)
from app.agents.headway_monitor import headway_monitor
from app.agents.impact_simulator import impact_simulator, simulate
from app.agui.publisher import RunEventPublisher
from app.config import Settings
from app.db.enums import BunchingEventKind, BunchingEventStatus, WorkflowStatus
from app.db.models import AgentStep, BunchingEvent, Recommendation, RouteDirection, WorkflowRun
from app.graph.deps import WorkflowDeps, recorded
from app.graph.state import WorkflowState
from app.harness.budget import RunBudget
from app.harness.errors import BudgetExceeded, HarnessError
from app.harness.harness import ToolHarness
from app.llm.gateway import LlmGateway
from app.runner.route_loader import RouteNotImported, load_world_route

log = structlog.get_logger()

ACTIVE = (
    WorkflowStatus.RUNNING,
    WorkflowStatus.AWAITING_APPROVAL,
    WorkflowStatus.ESCALATED,
    WorkflowStatus.DISPATCHED,
)

# A run that ended: end_reason -> (run status, event status)
ENDED: dict[str, tuple[WorkflowStatus, BunchingEventStatus]] = {
    "do_nothing_recommended": (WorkflowStatus.COMPLETED, BunchingEventStatus.HANDLING),
    "not_bunched": (WorkflowStatus.COMPLETED, BunchingEventStatus.RESOLVED),
    "stale_data": (WorkflowStatus.COMPLETED, BunchingEventStatus.OPEN),  # retried later
    "outcome_measured": (WorkflowStatus.COMPLETED, BunchingEventStatus.RESOLVED),
    "rejected": (WorkflowStatus.REJECTED, BunchingEventStatus.DISMISSED),  # the officer decided
    "replans_exhausted": (WorkflowStatus.FAILED, BunchingEventStatus.DISMISSED),
}


# ------------------------------------------------------------------ step summaries (agent_steps)


def _in(state: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "candidate" in state:
        out["candidate"] = state["candidate"].key
    if "round" in state:
        out["round"] = state["round"]
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
    for key in ("decision", "instruction", "driver_reply", "end_reason", "round", "degraded"):
        if result.get(key):
            out[key] = result[key]
    return out


# ------------------------------------------------------------------ graph


def fan_out(state: WorkflowState) -> list[Send]:
    round_ = state.get("round", 0)
    return [
        Send("simulate", {"candidate": c}) for c in state.get("candidates", []) if c.round == round_
    ]


def _route(state: WorkflowState) -> str:
    return state["route"]


def build_graph(deps: WorkflowDeps, checkpointer: BaseCheckpointSaver[Any]) -> Any:
    g: StateGraph[WorkflowState] = StateGraph(WorkflowState)

    def node(name: str, fn: Any) -> None:
        g.add_node(name, recorded(name, fn, deps, _in, _out))  # type: ignore[call-overload]

    for name, fn in (
        ("headway_monitor", headway_monitor),
        ("action_generator", action_generator),
        ("impact_simulator", impact_simulator),
        ("simulate", simulate),
        ("control_advisor", control_advisor),
        ("await_officer", await_officer),
        ("apply_edit", apply_edit),
        ("dispatch", dispatch),
        ("await_driver", await_driver),
        ("await_outcome", await_outcome),
        ("measure", measure),
    ):
        node(name, fn)
    g.add_edge(START, "headway_monitor")
    g.add_conditional_edges(
        "headway_monitor",
        _route,
        {"stale": END, "not_bunched": END, "generate": "action_generator"},
    )
    g.add_edge("action_generator", "impact_simulator")
    g.add_conditional_edges("impact_simulator", fan_out, ["simulate"])
    g.add_edge("simulate", "control_advisor")
    g.add_conditional_edges(
        "control_advisor",
        _route,
        {"recommend": "await_officer", "escalate": "await_officer", "do_nothing": END},
    )
    g.add_conditional_edges(
        "await_officer",
        _route,
        {
            "approve": "dispatch",
            "edit": "apply_edit",
            "reject": "action_generator",
            "replan": "headway_monitor",
            "exhausted": END,
        },
    )
    g.add_edge("apply_edit", "impact_simulator")
    g.add_conditional_edges(
        "dispatch",
        _route,
        {"sent": "await_driver", "resolved": END, "replan": "headway_monitor", "exhausted": END},
    )
    g.add_conditional_edges(
        "await_driver",
        _route,
        {"ack": "await_outcome", "replan": "headway_monitor", "exhausted": END},
    )
    g.add_edge("await_outcome", "measure")
    g.add_edge("measure", END)
    return g.compile(checkpointer=checkpointer)


# ------------------------------------------------------------------ runs


@dataclass(frozen=True)
class RunResult:
    run_id: uuid.UUID | None  # None: not started / not resumed
    status: str
    end_reason: str | None
    state: dict[str, Any] | None = None
    waiting_for: str | None = None


def _seconds(gtfs_time: str) -> int:
    h, m, s = (int(x) for x in gtfs_time.split(":"))
    return h * 3600 + m * 60 + s


def _config(run_id: uuid.UUID, settings: Settings) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": str(run_id)},
        "recursion_limit": settings.max_graph_steps + 10,
    }


def abandoned_after(settings: Settings) -> timedelta:
    """A 'running' run idle this long has no worker any more: every segment is capped by the
    run time limit, so twice that plus a margin can't be a live run."""
    return timedelta(seconds=2 * settings.run_time_limit_seconds + 60)


async def _superseded(session: AsyncSession, run: WorkflowRun, abandoned: timedelta) -> bool:
    """Close an active run that no longer describes the present, so a fresh run can take the
    pair; without this a stuck run would block its bus pair forever:
    - waiting for an officer whose recommendation has expired (or never had an expiry);
    - 'running' with no worker claim for longer than any segment can last (the worker died,
      e.g. Docker stopped mid-run)."""
    now = datetime.now(UTC)
    if run.status == WorkflowStatus.RUNNING:
        if now - run.last_active_at < abandoned:
            return False
        run.status, run.waiting_for = WorkflowStatus.FAILED, None
        run.end_reason, run.finished_at = "abandoned", now
        run.error = "no worker finished this run (it stopped mid-run)"
        await _dismiss_event(session, run, now)
        log.warning("workflow_abandoned", run_id=str(run.id))
        return True
    if run.waiting_for != "officer":
        return False
    expires = await session.scalar(
        select(func.max(Recommendation.expires_at)).where(Recommendation.run_id == run.id)
    )
    if expires is not None and expires > now:
        return False
    run.status, run.waiting_for = WorkflowStatus.COMPLETED, None
    run.end_reason, run.finished_at = "superseded", now
    await _dismiss_event(session, run, now)
    log.info("workflow_superseded", run_id=str(run.id))
    return True


async def _dismiss_event(session: AsyncSession, run: WorkflowRun, now: datetime) -> None:
    """The closed run's event: a newer event for the same pair takes over, so this one is
    dismissed (otherwise it would sit in 'handling' forever)."""
    event = await session.get(BunchingEvent, run.bunching_event_id)
    if event is not None and event.status in (
        BunchingEventStatus.OPEN,
        BunchingEventStatus.HANDLING,
    ):
        event.status, event.resolved_at = BunchingEventStatus.DISMISSED, now


async def _claim_start(
    sessionmaker: async_sessionmaker[AsyncSession],
    event_id: uuid.UUID,
    abandoned: timedelta,
) -> tuple[uuid.UUID, BunchingEvent] | str:
    """Create the run if the event is open and its bus pair has no active run (atomic)."""
    async with sessionmaker() as session, session.begin():
        event = await session.scalar(
            select(BunchingEvent).where(BunchingEvent.id == event_id).with_for_update()
        )
        if event is None:
            return "event_not_found"
        if event.status != BunchingEventStatus.OPEN:
            return "event_not_open"
        active = (
            await session.scalars(
                select(WorkflowRun)
                .join(BunchingEvent, BunchingEvent.id == WorkflowRun.bunching_event_id)
                .where(
                    BunchingEvent.leader_vehicle_id == event.leader_vehicle_id,
                    BunchingEvent.follower_vehicle_id == event.follower_vehicle_id,
                    WorkflowRun.status.in_(ACTIVE),
                    # A finished run is never active, whatever its status says (runs from
                    # before B12 ended at the recommendation as 'awaiting_approval').
                    WorkflowRun.finished_at.is_(None),
                )
                .with_for_update(of=WorkflowRun)
            )
        ).all()
        for other in active:
            if not await _superseded(session, other, abandoned):
                return "pair_already_handled"
        run = WorkflowRun(bunching_event_id=event.id, status=WorkflowStatus.RUNNING)
        session.add(run)
        event.status = BunchingEventStatus.HANDLING
        await session.flush()
        return run.id, event


async def _claim_resume(
    sessionmaker: async_sessionmaker[AsyncSession], run_id: uuid.UUID, kind: str
) -> tuple[BunchingEvent, int] | str:
    """Take the paused run if it waits for exactly this kind of input (atomic)."""
    async with sessionmaker() as session, session.begin():
        run = await session.scalar(
            select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
        )
        if run is None:
            return "run_not_found"
        if run.waiting_for != kind:
            return f"not_waiting_for_{kind}"
        run.status, run.waiting_for = WorkflowStatus.RUNNING, None
        run.last_active_at = datetime.now(UTC)
        event = await session.get(BunchingEvent, run.bunching_event_id)
        steps = await session.scalar(
            select(func.coalesce(func.max(AgentStep.step_index), 0)).where(
                AgentStep.run_id == run_id
            )
        )
        if event is None:
            return "event_not_found"
        return event, int(steps or 0)


async def _settle(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    final: dict[str, Any],
    steps: int,
) -> RunResult:
    """Record where the graph stopped: paused at an interrupt, or ended."""
    interrupts = final.get("__interrupt__") or []
    end_reason = final.get("end_reason")
    waiting: str | None = None
    if interrupts:
        waiting = str(interrupts[0].value["kind"])
        if waiting == "officer":
            escalated = (
                final.get("recommendation") is not None and final["recommendation"].is_near_tie
            )
            status = WorkflowStatus.ESCALATED if escalated else WorkflowStatus.AWAITING_APPROVAL
        else:
            status = WorkflowStatus.DISPATCHED
        event_status = BunchingEventStatus.HANDLING
    else:
        status, event_status = ENDED[end_reason or "not_bunched"]
    async with sessionmaker() as session, session.begin():
        run = await session.get(WorkflowRun, run_id)
        event = await session.get(BunchingEvent, event_id)
        if run is not None:
            run.status, run.waiting_for, run.end_reason = status, waiting, end_reason
            run.step_count = steps
            run.regeneration_round = int(final.get("round", 0))
            run.degraded = sorted(set(run.degraded or []) | set(final.get("degraded", [])))
            if waiting is None:
                run.finished_at = datetime.now(UTC)
        if event is not None and event.status in (
            BunchingEventStatus.HANDLING,
            BunchingEventStatus.OPEN,
        ):
            if (
                waiting is None
                and event_status == BunchingEventStatus.HANDLING
                and event.kind == BunchingEventKind.PREDICTED
            ):
                # "Do nothing" on a prediction: nothing to watch for (the world runner only
                # closes the events it detects); an actual bunch later raises its own event.
                event_status = BunchingEventStatus.DISMISSED
            event.status = event_status
            if event_status in (BunchingEventStatus.RESOLVED, BunchingEventStatus.DISMISSED):
                event.resolved_at = datetime.now(UTC)
    log.info(
        "workflow_paused" if waiting else "workflow_finished",
        status=status.value,
        end_reason=end_reason,
        waiting_for=waiting,
        steps=steps,
    )
    return RunResult(run_id, status.value, end_reason, final, waiting)


async def _failed(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    event_id: uuid.UUID,
    status: WorkflowStatus,
    reason: str,
    steps: int,
    error: str | None = None,
) -> RunResult:
    """The event goes back to OPEN so a later run can try again."""
    async with sessionmaker() as session, session.begin():
        run = await session.get(WorkflowRun, run_id)
        event = await session.get(BunchingEvent, event_id)
        if run is not None:
            run.status, run.end_reason, run.error = status, reason, error
            run.waiting_for, run.step_count, run.finished_at = None, steps, datetime.now(UTC)
        if event is not None and event.status == BunchingEventStatus.HANDLING:
            event.status = BunchingEventStatus.OPEN
    log.warning("workflow_failed", status=status.value, end_reason=reason, error=error)
    return RunResult(run_id, status.value, reason)


async def _drive(
    graph: Any, graph_input: Any, config: dict[str, Any], publisher: RunEventPublisher
) -> dict[str, Any]:
    """Run one segment through LangGraph's stream, feeding the AG-UI publisher (decision 10);
    returns the final state (the last `values` chunk, with `__interrupt__` if it paused)."""
    final: dict[str, Any] = {}
    async for mode, chunk in graph.astream(
        graph_input, config=config, stream_mode=["tasks", "updates", "values"]
    ):
        if mode == "values":
            final = chunk
        else:
            await publisher.on_chunk(mode, chunk)
    return final


async def _execute(
    run_id: uuid.UUID,
    event: BunchingEvent,
    graph_input: Any,
    first_step: int,
    harness: ToolHarness,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_queue: ArqRedis,
    checkpointer: BaseCheckpointSaver[Any],
    llm: LlmGateway | None = None,
) -> RunResult:
    budget = RunBudget.from_settings(run_id, settings)  # limits apply to each active segment
    budget.steps = first_step
    publisher = RunEventPublisher(job_queue, str(run_id))
    await publisher.start()
    failure: tuple[WorkflowStatus, str, str | None] | None = None
    final: dict[str, Any] = {}
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
            first_step=first_step,
            llm=llm,
        )
        graph = build_graph(deps, checkpointer)
        final = await asyncio.wait_for(
            _drive(graph, graph_input, _config(run_id, settings), publisher),
            timeout=settings.run_time_limit_seconds,
        )
    except TimeoutError:
        failure = (WorkflowStatus.TIMED_OUT, "run_time_limit", "the run hit its time limit")
    except BudgetExceeded as exc:
        failure = (WorkflowStatus.FAILED, f"budget:{exc.code}", str(exc))
    except HarnessError as exc:
        failure = (WorkflowStatus.FAILED, f"tools:{exc.kind}:{exc.tool}", str(exc))
    except RouteNotImported as exc:
        budget.steps = 0
        failure = (WorkflowStatus.FAILED, "route_not_imported", str(exc))
    if failure is not None:
        status, reason, error = failure
        error_detail = None if status == WorkflowStatus.TIMED_OUT else error
        result = await _failed(
            sessionmaker, run_id, event.id, status, reason, budget.steps, error_detail
        )
        await publisher.error(reason, error or reason)
        return result
    result = await _settle(sessionmaker, run_id, event.id, dict(final), budget.steps)
    await publisher.finish(result.waiting_for, result.end_reason)
    return result


async def start_workflow(
    event_id: uuid.UUID,
    harness: ToolHarness,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_queue: ArqRedis,
    checkpointer: BaseCheckpointSaver[Any],
    llm: LlmGateway | None = None,
) -> RunResult:
    claimed = await _claim_start(sessionmaker, event_id, abandoned_after(settings))
    if isinstance(claimed, str):
        log.info("workflow_skipped", event_id=str(event_id), reason=claimed)
        return RunResult(None, "skipped", claimed)
    run_id, event = claimed
    with structlog.contextvars.bound_contextvars(run_id=str(run_id), event_id=str(event_id)):
        log.info("workflow_started", route_id=event.route_id)
        initial: WorkflowState = {
            "run_id": str(run_id),
            "event_id": str(event_id),
            "route_id": event.route_id,
            "event_kind": event.kind.value,
            "leader_vehicle_id": str(event.leader_vehicle_id),
            "follower_vehicle_id": str(event.follower_vehicle_id),
            "round": 0,
        }
        if event.predicted_in_seconds is not None:
            initial["predicted_in_s"] = event.predicted_in_seconds
        return await _execute(
            run_id, event, initial, 0, harness, sessionmaker, settings, job_queue, checkpointer, llm
        )


async def resume_workflow(
    run_id: uuid.UUID,
    kind: str,
    value: dict[str, Any],
    harness: ToolHarness,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_queue: ArqRedis,
    checkpointer: BaseCheckpointSaver[Any],
    llm: LlmGateway | None = None,
) -> RunResult:
    """Continue a paused run with the officer's decision / driver's reply / outcome due."""
    claimed = await _claim_resume(sessionmaker, run_id, kind)
    if isinstance(claimed, str):
        log.info("workflow_resume_skipped", run_id=str(run_id), reason=claimed)
        return RunResult(None, "skipped", claimed)
    event, steps = claimed
    with structlog.contextvars.bound_contextvars(run_id=str(run_id), event_id=str(event.id)):
        log.info("workflow_resumed", kind=kind)
        return await _execute(
            run_id,
            event,
            Command(resume=value),
            steps,
            harness,
            sessionmaker,
            settings,
            job_queue,
            checkpointer,
            llm,
        )
