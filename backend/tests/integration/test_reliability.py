"""B17 reliability with real services: the world under Redis outages, pruning, bad signals,
queue and tool outages in the workflow, re-validation before sending, the driver timeout,
failure injection per scenario, and the status messages an officer sees."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arq import ArqRedis
from arq.worker import Worker
from httpx import AsyncClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from app.config import get_settings
from app.db.enums import (
    InstructionStatus,
    ScenarioSource,
    ScenarioStatus,
    WorkflowStatus,
)
from app.db.models import (
    BunchingEvent,
    DriverInstruction,
    ScenarioRecord,
    StopQueue,
    VehiclePosition,
    WorkflowRun,
)
from app.db.session import get_sessionmaker
from app.graph.workflow import start_workflow
from app.harness.faults import FaultyConnector, redis_fault_source
from app.harness.harness import ToolHarness
from app.live.channels import FAULTS_KEY, SCENARIO_KEY
from app.live.schemas import WorldScenario
from app.runner.world import WorldRunner
from app.workers.workflows import driver_ack_timeout, early_warning
from mcp_servers.demand_mcp import server as demand_server
from mcp_servers.schemas import InstructionSent
from scenario_gen.schema import ScenarioPlan
from simulator import Observations, VehicleObservation, from_observations
from tests.conftest import Account
from tests.integration.conftest import bearer, login
from tests.integration.test_approval import (  # noqa: F401
    api,
    instructions,
    paused_run,
    pending,
    run_id_of,
    run_row,
    wait_for,
    waiting,
    worker,
)
from tests.integration.test_live_world import FakeClock, _bunched_pair, _runner, _tick, route
from tests.integration.test_workflow import (  # noqa: F401
    InMemoryConnector,
    bunched,
    clock,
    harness,
    only_event,
    redis,
    state_of,
)

pytestmark = pytest.mark.services
__all__ = ["route"]  # fixture used by name


def dead_redis() -> Redis:
    client: Redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    return client


# ------------------------------------------------------------------ the world


async def test_redis_down_while_the_world_runs(route: Any) -> None:
    world_clock = FakeClock()
    runner = _runner(route, dead_redis(), world_clock)
    _bunched_pair(runner, route)
    await runner.setup()
    frame = await _tick(runner, world_clock, 3)  # no exception: publishing is best effort
    assert frame.vehicles  # the world kept moving
    async with get_sessionmaker()() as session:
        positions = await session.scalar(select(func.count()).select_from(VehiclePosition))
    assert positions and positions > 0  # and kept writing to Postgres


async def test_live_data_tables_are_pruned(route: Any, redis: Redis) -> None:  # noqa: F811
    world_clock = FakeClock()
    runner = _runner(route, redis, world_clock)
    _bunched_pair(runner, route)
    await runner.setup()
    await _tick(runner, world_clock, 2)
    old = world_clock() - timedelta(hours=7)  # retention is 6 h
    async with get_sessionmaker()() as session:
        vehicle = next(iter(runner.vehicle_ids.values()))
        session.add(
            VehiclePosition(
                vehicle_id=vehicle, recorded_at=old, geom="SRID=4326;POINT(80.27 13.08)",
                direction_id=0, distance_along_m=10.0,
            )
        )  # fmt: skip
        session.add(
            StopQueue(route_id="T1", stop_id=route.stop_ids[0][1], recorded_at=old,
                      sim_time_s=0, direction_id=0, waiting_count=1)
        )  # fmt: skip
        await session.commit()
    runner._last_prune = None
    await _tick(runner, world_clock, 1)
    async with get_sessionmaker()() as session:
        stale_positions = await session.scalar(
            select(func.count())
            .select_from(VehiclePosition)
            .where(VehiclePosition.recorded_at < world_clock() - timedelta(hours=6))
        )
        stale_queues = await session.scalar(
            select(func.count())
            .select_from(StopQueue)
            .where(StopQueue.recorded_at < world_clock() - timedelta(hours=6))
        )
    assert stale_positions == 0 and stale_queues == 0


async def test_a_malformed_scenario_signal_is_ignored(route: Any, redis: Redis) -> None:  # noqa: F811
    world_clock = FakeClock()
    runner = _runner(route, redis, world_clock)
    await runner.setup()
    before = await _tick(runner, world_clock, 2)
    await redis.set(SCENARIO_KEY, b'{"token": 42, "plan": "not a plan"')
    after = await _tick(runner, world_clock, 2)
    assert after.sim_elapsed_s > before.sim_elapsed_s  # no reset: the world carried on
    await redis.set(SCENARIO_KEY, WorldScenario(token="t2").model_dump_json())
    reset = await _tick(runner, world_clock, 1)
    assert reset.sim_elapsed_s < after.sim_elapsed_s  # a valid signal still works


async def test_an_acknowledged_action_no_longer_valid_is_rejected_by_the_world(
    route: Any,
    redis: Redis,  # noqa: F811
) -> None:
    world_clock = FakeClock()
    runner = _runner(route, redis, world_clock)
    _bunched_pair(runner, route)
    await runner.setup()
    behind = route.stop_ids[0][0]  # the first stop: T1-02 passed it long ago
    assert not runner.apply_instruction(
        {"fleet_number": "T1-02", "action_type": "skip_stop", "params": {"stop_ids": [behind]}}
    )
    assert not runner.apply_instruction(
        {"fleet_number": "T1-99", "action_type": "hold", "params": {"seconds": 60}}
    )
    assert runner.apply_instruction(
        {"fleet_number": "T1-02", "action_type": "hold", "params": {"seconds": 60}}
    )  # a valid one is applied


async def test_an_instruction_published_while_redis_is_down_is_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demand_server, "_redis", dead_redis())
    sent = InstructionSent(
        id=uuid.uuid4(), instruction_id="apr-x", approval_id=uuid.uuid4(),
        vehicle_id=uuid.uuid4(), fleet_number="T1-02", driver_id=None,
        action={"type": "hold", "seconds": 60}, status="sent", sent_at=datetime.now(UTC),
    )  # fmt: skip
    await demand_server._publish(sent)  # logged, not raised: the row is the source of truth


# ------------------------------------------------------------------ the workflow


class QueueDown:
    """The simulation queue is unreachable; everything else works."""

    def __init__(self, real: ArqRedis) -> None:
        self._real = real

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("simulation queue unreachable")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


async def test_simulation_queue_unavailable(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    job_queue: ArqRedis,
    checkpointer: Any,
) -> None:
    settings = get_settings()
    event = await only_event()
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings,
        QueueDown(job_queue), checkpointer,  # type: ignore[arg-type]
    )  # fmt: skip
    outcomes = state_of(result)["sim_outcomes"]
    assert outcomes and all(o.status == "failed" for o in outcomes)
    assert all("queue unavailable" in (o.error or "") for o in outcomes)
    assert result.end_reason == "escalated"  # no baseline: the officer decides, nothing is sent
    assert await instructions() == []


async def test_the_situation_changed_between_approval_and_sending(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,  # noqa: F811
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    runner, _ = bunched
    result = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    # The buses separate before the approval arrives.
    runner.sim = from_observations(
        runner.route.model,
        Observations(
            time_s=runner.sim.state.time_s,
            vehicles=(
                VehicleObservation("T1-01", 0, 4200.0, load=40),
                VehicleObservation("T1-02", 0, 2100.0, load=10),
                VehicleObservation("T1-03", 0, 300.0, load=20),
                VehicleObservation("T1-04", 1, 3000.0, load=20),
            ),
        ),
        runner.sim.config,
        seed=1,
    )
    for _ in range(3):
        runner_clock: Any = runner._clock
        runner_clock.advance(10)
        await runner.tick(10)
    resp = await api.post(
        f"/officer/recommendations/{rec['id']}/decision",
        json={"decision": "approve"},
        headers=officer,
    )
    assert resp.status_code == 202
    run = await wait_for(run_id_of(result), lambda r: r.finished_at is not None)
    assert run.end_reason == "not_bunched"  # dispatch re-validated with fresh data
    assert await instructions() == []  # nothing sent


async def test_a_driver_who_never_acknowledges(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,  # noqa: F811
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    await api.post(
        f"/officer/recommendations/{rec['id']}/decision",
        json={"decision": "approve"},
        headers=officer,
    )
    await wait_for(run_id_of(result), waiting("driver"))
    (sent,) = await instructions()

    ctx = {"harness": harness(get_settings()), "checkpointer": checkpointer, "redis": job_queue}
    outcome = await driver_ack_timeout(ctx, str(run_id_of(result)), sent.instruction_id)

    async with get_sessionmaker()() as session:
        expired = await session.get(DriverInstruction, sent.id)
    assert expired is not None and expired.status == InstructionStatus.EXPIRED
    run = await run_row(run_id_of(result))
    assert run.waiting_for != "driver"  # the run moved on: re-planned (or ended)
    assert outcome != "acknowledged"
    again = await driver_ack_timeout(ctx, str(run_id_of(result)), sent.instruction_id)
    assert again == "acknowledged"  # a late duplicate timeout does nothing


async def test_avl_down_during_an_early_warning_sweep(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    job_queue: ArqRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_ROUTE_ID", "T1")
    get_settings.cache_clear()
    down = ToolHarness.from_settings(get_settings(), get_sessionmaker(), InMemoryConnector(["avl"]))
    assert await early_warning({"harness": down, "redis": job_queue}) == 0
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(BunchingEvent)) == 1


async def test_route_not_imported_when_the_worker_starts(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    job_queue: ArqRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_ROUTE_ID", "NOPE")
    get_settings.cache_clear()
    ctx: dict[str, Any] = {"harness": harness(get_settings()), "redis": job_queue}
    assert await early_warning(ctx) == 0  # skipped, logged; the worker keeps serving jobs
    assert "world_route" not in ctx


# ------------------------------------------------------------------ failure injection + status


async def ready_scenario() -> uuid.UUID:
    plan = ScenarioPlan(
        name="Quiet test day", summary="A plain plan for failure injection tests.",
        start_time="08:00", duration_min=30, demand_multiplier=1.0,
    )  # fmt: skip
    async with get_sessionmaker()() as session:
        record = ScenarioRecord(
            route_id="T1", source=ScenarioSource.GENERATED, description="test scenario",
            status=ScenarioStatus.READY, seed=1, plan=plan.model_dump(mode="json"),
        )  # fmt: skip
        session.add(record)
        await session.commit()
        return record.id


async def test_failure_injection_per_scenario_and_the_status_messages(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
    redis: Redis,  # noqa: F811
) -> None:
    officer = bearer(await login(api, accounts["officer"]))
    scenario = await ready_scenario()
    resp = await api.post(
        f"/lab/scenarios/{scenario}/apply",
        json={"faults": {"down": ["demand"], "slow": {"get_headways": 0.2}}},
        headers=officer,
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["faults"] == ["demand-mcp is down", "get_headways is slowed by 0.2 s"]
    assert json.loads(await redis.get(FAULTS_KEY))["down"] == ["demand"]

    # The worker's harness injects the scenario's faults (as app/workers/main.py wires it).
    settings = get_settings()
    faulty = ToolHarness.from_settings(
        settings,
        get_sessionmaker(),
        FaultyConnector(InMemoryConnector(), redis_fault_source(redis, ttl_s=0)),
    )
    event = await only_event()
    result = await start_workflow(
        event.id, faulty, get_sessionmaker(), settings, job_queue, checkpointer
    )
    assert state_of(result)["degraded"] == ["get_boarding_estimates"]  # ran without demand

    status = (await api.get("/officer/status", headers=officer)).json()
    assert not status["healthy"]
    assert any("Failure injection is on: demand-mcp is down" in m for m in status["messages"])
    assert any("demand data unavailable" in m for m in status["messages"])
    if result.waiting_for == "officer":
        card = await pending(api, officer)
        assert any("degraded mode" in m for m in card["status_messages"])
    runs = (await api.get("/officer/runs", headers=officer)).json()["runs"]
    assert any("degraded mode" in m for m in runs[0]["status_messages"])

    # Toggled off on the running world (no restart): no more injected failures.
    off = await api.put("/lab/world/faults", json={}, headers=officer)
    assert off.status_code == 200 and await redis.get(FAULTS_KEY) is None
    status = (await api.get("/officer/status", headers=officer)).json()
    assert status["faults"] == []

    # Bad JSON from a tool the run can do without: degraded, not failed.
    await api.put(
        "/lab/world/faults", json={"bad_json": ["get_boarding_estimates"]}, headers=officer
    )
    async with get_sessionmaker()() as session:
        first = await session.get(BunchingEvent, event.id)
        assert first is not None
        again = BunchingEvent(
            route_id="T1", leader_vehicle_id=first.leader_vehicle_id,
            follower_vehicle_id=first.follower_vehicle_id, headway_seconds=20,
            scheduled_headway_seconds=300, severity=first.severity,
        )  # fmt: skip
        session.add(again)
        await session.commit()
        again_id = again.id
    await _close(run_id_of(result))
    second = await start_workflow(
        again_id, faulty, get_sessionmaker(), settings, job_queue, checkpointer
    )
    assert state_of(second)["degraded"] == ["get_boarding_estimates"]

    # A killed avl-mcp: the run can't see the buses, so it fails cleanly and says why.
    await api.put("/lab/world/faults", json={"down": ["avl"]}, headers=officer)
    await _close(run_id_of(second))
    async with get_sessionmaker()() as session:
        third = BunchingEvent(
            route_id="T1", leader_vehicle_id=first.leader_vehicle_id,
            follower_vehicle_id=first.follower_vehicle_id, headway_seconds=20,
            scheduled_headway_seconds=300, severity=first.severity,
        )  # fmt: skip
        session.add(third)
        await session.commit()
        third_id = third.id
    failed = await start_workflow(
        third_id, faulty, get_sessionmaker(), settings, job_queue, checkpointer
    )
    assert failed.status == WorkflowStatus.FAILED.value
    assert failed.end_reason == "tools:unavailable:get_vehicle_positions"
    status = (await api.get("/officer/status", headers=officer)).json()
    assert any("avl-mcp down" in m for m in status["messages"])


async def _close(run_id: uuid.UUID) -> None:
    """Finish a run so the next event for the same pair can start one."""
    async with get_sessionmaker()() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is not None and run.finished_at is None:
            run.status, run.waiting_for = WorkflowStatus.COMPLETED, None
            run.end_reason, run.finished_at = "test_closed", datetime.now(UTC)
            await session.commit()
