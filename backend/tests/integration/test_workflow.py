"""B10 acceptance: one bunching event goes all the way through the graph to a ranked
recommendation, and every step appears in agent_steps.

Real pieces end to end: the B6 runner detects the bunch from its own GPS reports; the harness
(B8) calls the B7 MCP servers (in-memory MCP transport); the what-if model is built from those
tool results only; the recommendation is stored. Plus the other routes: stale data, a pair that
is no longer adjacent, dedupe, timeouts, budgets, degraded demand data and the retry sweep.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arq import ArqRedis
from arq.worker import Worker
from mcp import Client
from mcp.types import CallToolResult, Tool
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.enums import BunchingEventStatus, StepStatus, WorkflowStatus
from app.db.models import (
    AgentStep,
    BunchingEvent,
    CandidateAction,
    Recommendation,
    WorkflowRun,
)
from app.db.session import get_sessionmaker
from app.graph.workflow import start_workflow
from app.gtfs.import_gtfs import prepare
from app.gtfs.writer import write_route
from app.harness.client import Transient
from app.harness.harness import ToolHarness
from app.runner.route_loader import load_world_route
from app.runner.world import WorldRunner
from app.workers.workflows import retry_open_events
from mcp_servers import observations
from mcp_servers.avl_mcp.server import server as avl_server
from mcp_servers.demand_mcp.server import server as demand_server
from simulator import Observations, VehicleObservation, from_observations
from tests.gtfs_fixtures import write_feed, write_route_config
from tests.integration.test_live_world import FakeClock

pytestmark = pytest.mark.services


class InMemoryConnector:
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


@pytest.fixture
async def redis(database_url: str) -> Any:
    client: Redis = Redis.from_url(get_settings().redis_url)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(observations, "utc_now", clock)
    observations._route_cache.clear()
    return clock


@pytest.fixture
async def bunched(
    db: None, tmp_path: Path, redis: Redis, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> tuple[WorldRunner, list[uuid.UUID]]:
    """T1 with buses 01 and 02 100 m apart mid-route (bunched) and 03 far behind; the runner
    writes observations and opens the bunching event itself."""
    monkeypatch.setenv("WORLD_ROUTE_ID", "T1")
    get_settings.cache_clear()
    routes_dir = tmp_path / "routes"
    write_route_config(routes_dir)
    imported = prepare("T1", write_feed(tmp_path / "feed.zip"), routes_dir)
    async with get_sessionmaker()() as session:
        await write_route(session, imported)
        route = await load_world_route(session, "T1")
    queued: list[uuid.UUID] = []

    async def enqueue(event_id: uuid.UUID) -> None:
        queued.append(event_id)

    runner = WorldRunner(
        route,
        get_sessionmaker(),
        redis,
        seed=1,
        speed=1,
        stale_after_s=60,
        bunching_ratio=0.5,
        closing_ratio=0.75,
        queue_every_s=10,
        retention=timedelta(hours=6),
        clock=clock,
        enqueue=enqueue,
    )
    obs = Observations(
        time_s=8 * 3600,
        vehicles=(
            VehicleObservation("T1-01", 0, 3000.0, load=40),
            VehicleObservation("T1-02", 0, 2900.0, load=10),
            VehicleObservation("T1-03", 0, 900.0, load=20),
            VehicleObservation("T1-04", 1, 3000.0, load=20),
        ),
    )
    runner.sim = from_observations(route.model, obs, runner.sim.config, seed=1)
    await runner.setup()
    for _ in range(8):
        clock.advance(10)
        await runner.tick(10)
    return runner, queued


def harness(settings: Settings, down: Sequence[str] = ()) -> ToolHarness:
    return ToolHarness.from_settings(settings, get_sessionmaker(), InMemoryConnector(down))


def state_of(result: Any) -> dict[str, Any]:
    assert result.state is not None, result
    state: dict[str, Any] = result.state
    return state


def summary_of(step: AgentStep) -> dict[str, Any]:
    assert step.output_summary is not None, step.node
    return step.output_summary


async def only_event() -> BunchingEvent:
    async with get_sessionmaker()() as session:
        (event,) = (await session.scalars(select(BunchingEvent))).all()
        return event


async def steps(run_id: uuid.UUID) -> list[AgentStep]:
    async with get_sessionmaker()() as session:
        return list(
            await session.scalars(
                select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.step_index)
            )
        )


# ------------------------------------------------------------------ the happy path


async def test_bunching_event_to_ranked_recommendation(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
) -> None:
    _, queued = bunched
    event = await only_event()
    assert queued == [event.id]  # the runner queued the workflow when it opened the event
    settings = get_settings()

    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )

    assert result.run_id is not None
    assert result.end_reason in {"recommended", "escalated", "do_nothing_recommended"}
    recommendation = state_of(result)["recommendation"]
    for r in recommendation.ranking:
        print(
            f"  {r.candidate_key:28} benefit {r.benefit_passenger_minutes:7.1f}  "
            f"wait {r.passenger_wait_minutes:7.1f}  bunching {r.bunching_count:.1f}  "
            f"{r.rejected_reason or ''}"
        )
    print(" ", result.end_reason, "|", recommendation.explanation)
    assert recommendation.ranking[-1].candidate_key == "do_nothing"
    keys = [r.candidate_key for r in recommendation.ranking]
    assert (
        len(keys) >= 3 and keys[0] != "do_nothing"
    ) or result.end_reason == "do_nothing_recommended"

    # Every step is in agent_steps, in order, with routing decisions.
    recorded = await steps(result.run_id)
    nodes = [s.node for s in recorded]
    assert nodes[:3] == ["headway_monitor", "action_generator", "impact_simulator"]
    assert nodes[-1] == "control_advisor"
    n_candidates = len(state_of(result)["candidates"])
    assert nodes.count("simulate") == n_candidates >= 3
    assert [s.step_index for s in recorded] == list(range(1, len(recorded) + 1))
    assert all(s.status == StepStatus.OK for s in recorded)
    assert recorded[0].routing_decision == "generate"
    assert summary_of(recorded[0])["pair"]["follower"] == "T1-02"
    assert summary_of(recorded[-1])["recommendation"]["candidate"] == recommendation.candidate_key

    async with get_sessionmaker()() as session:
        run = await session.get(WorkflowRun, result.run_id)
        rec = await session.scalar(
            select(Recommendation).where(Recommendation.run_id == result.run_id)
        )
        candidates = (
            await session.scalars(
                select(CandidateAction).where(CandidateAction.run_id == result.run_id)
            )
        ).all()
        event_after = await session.get(BunchingEvent, event.id)
    assert run is not None and run.step_count == len(recorded) and run.finished_at is not None
    assert run.status in {
        WorkflowStatus.AWAITING_APPROVAL,
        WorkflowStatus.ESCALATED,
        WorkflowStatus.COMPLETED,
    }
    assert rec is not None and rec.explanation and rec.ranking  # stored for the officer (B12/B14)
    assert len(candidates) == n_candidates
    assert event_after is not None and event_after.status == BunchingEventStatus.HANDLING

    # The hold candidate targets the follower and respects the rulebook.
    hold = next(c for c in state_of(result)["candidates"] if c.action_type == "hold")
    assert hold.fleet_number == "T1-02" and 30 <= hold.params["seconds"] <= 180


async def test_same_pair_is_handled_once(
    bunched: tuple[WorldRunner, list[uuid.UUID]], sim_worker: Worker, job_queue: ArqRedis
) -> None:
    event = await only_event()
    settings = get_settings()
    first = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )
    assert first.run_id is not None
    again = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )
    assert again.run_id is None and again.end_reason == "event_not_open"

    # A second event for the same pair while the first run is active: deduplicated.
    async with get_sessionmaker()() as session:
        dup = BunchingEvent(
            route_id="T1",
            leader_vehicle_id=event.leader_vehicle_id,
            follower_vehicle_id=event.follower_vehicle_id,
            headway_seconds=20,
            scheduled_headway_seconds=300,
            severity=event.severity,
        )
        session.add(dup)
        await session.commit()
    if first.status in {"awaiting_approval", "escalated"}:
        third = await start_workflow(
            dup.id, harness(settings), get_sessionmaker(), settings, job_queue
        )
        assert third.run_id is None and third.end_reason == "pair_already_handled"


# ------------------------------------------------------------------ other routes


async def test_stale_data_flags_and_ends_without_candidates(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
    clock: FakeClock,
) -> None:
    event = await only_event()
    clock.advance(61)  # no GPS for a minute
    settings = get_settings()
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )
    assert result.end_reason == "stale_data"
    recorded = await steps(result.run_id)  # type: ignore[arg-type]
    assert [s.node for s in recorded] == ["headway_monitor"]
    assert recorded[0].routing_decision == "stale"
    assert (await only_event()).status == BunchingEventStatus.OPEN  # retried later


async def test_pair_no_longer_adjacent_ends_and_resolves(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
) -> None:
    runner, _ = bunched
    async with get_sessionmaker()() as session:  # 01 and 03 have 02 between them
        event = BunchingEvent(
            route_id="T1",
            leader_vehicle_id=runner.vehicle_ids["T1-01"],
            follower_vehicle_id=runner.vehicle_ids["T1-03"],
            headway_seconds=20,
            scheduled_headway_seconds=300,
            severity="bunched",
        )
        session.add(event)
        await session.commit()
    settings = get_settings()
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )
    assert result.end_reason == "not_bunched"
    async with get_sessionmaker()() as session:
        assert (await session.get(BunchingEvent, event.id)).status == BunchingEventStatus.RESOLVED  # type: ignore[union-attr]


async def test_demand_data_down_runs_degraded(
    bunched: tuple[WorldRunner, list[uuid.UUID]], sim_worker: Worker, job_queue: ArqRedis
) -> None:
    event = await only_event()
    settings = get_settings()
    result = await start_workflow(
        event.id, harness(settings, down=["demand"]), get_sessionmaker(), settings, job_queue
    )
    assert result.end_reason in {"recommended", "escalated", "do_nothing_recommended"}
    kinds = {c.action_type for c in state_of(result)["candidates"]}
    assert "skip_stop" not in kinds  # needs demand data
    generator = next(s for s in await steps(result.run_id) if s.node == "action_generator")  # type: ignore[arg-type]
    assert summary_of(generator)["degraded"] == ["get_boarding_estimates"]


async def test_simulation_timeouts_escalate(
    bunched: tuple[WorldRunner, list[uuid.UUID]], sim_worker: Worker, job_queue: ArqRedis
) -> None:
    event = await only_event()
    settings = get_settings().model_copy(update={"sim_timeout_seconds": 0.0001})
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )
    assert result.end_reason == "escalated"  # no baseline: the officer decides
    outcomes = state_of(result)["sim_outcomes"]
    assert outcomes and all(o.status == "timed_out" for o in outcomes)


async def test_step_budget_fails_the_run_cleanly(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
) -> None:
    event = await only_event()
    settings = get_settings().model_copy(update={"max_graph_steps": 2})
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue
    )
    assert result.status == "failed" and result.end_reason == "budget:steps"
    assert (await only_event()).status == BunchingEventStatus.OPEN


async def test_avl_down_fails_the_run_and_reopens_the_event(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
) -> None:
    event = await only_event()
    settings = get_settings()
    result = await start_workflow(
        event.id, harness(settings, down=["avl"]), get_sessionmaker(), settings, job_queue
    )
    assert result.status == "failed" and result.end_reason.startswith("tools:unavailable")  # type: ignore[union-attr]
    assert (await only_event()).status == BunchingEventStatus.OPEN


async def test_open_events_without_a_run_are_retried(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
) -> None:
    event = await only_event()

    class Queue:
        def __init__(self) -> None:
            self.jobs: list[tuple[str, str]] = []

        async def enqueue_job(self, name: str, event_id: str, **kwargs: Any) -> None:
            self.jobs.append((name, event_id))

    async def detected(ago: timedelta) -> None:  # real time: the sweep uses the wall clock
        async with get_sessionmaker()() as session:
            e = await session.get(BunchingEvent, event.id)
            assert e is not None
            e.detected_at = datetime.now(UTC) - ago
            await session.commit()

    queue = Queue()
    await detected(timedelta(seconds=5))
    assert await retry_open_events({"redis": queue}) == 0  # just opened: too soon
    await detected(timedelta(minutes=5))
    assert await retry_open_events({"redis": queue}) == 1
    assert queue.jobs == [("run_workflow", str(event.id))]
