"""B7 acceptance: both MCP servers list their tools with schemas, every tool can be called, and
send_driver_instruction refuses unapproved, mismatched, duplicate, stale and impossible requests.

The servers are called through an in-memory MCP client (the real protocol, no HTTP), over
observations written by the real world runner (B6), with a fake clock for staleness.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.config import get_settings
from app.db.enums import ActionType, ApprovalDecision, CandidateSource, Role
from app.db.models import (
    Approval,
    CandidateAction,
    DriverAssignment,
    DriverInstruction,
    Recommendation,
    User,
    WorkflowRun,
)
from app.db.session import get_sessionmaker
from app.gtfs.import_gtfs import prepare
from app.gtfs.writer import write_route
from app.live.channels import INSTRUCTION_CHANNEL
from app.runner.route_loader import WorldRoute, load_world_route
from app.runner.world import WorldRunner
from mcp_servers import observations
from mcp_servers.avl_mcp.server import server as avl
from mcp_servers.demand_mcp.server import server as demand
from mcp_servers.errors import parse_tool_error
from mcp_servers.schemas import (
    BoardingEstimates,
    Headways,
    InstructionSent,
    ToolErrorBody,
    VehiclePositions,
)
from simulator import Observations, VehicleObservation, from_observations
from tests.conftest import Account
from tests.gtfs_fixtures import write_feed, write_route_config
from tests.integration.test_live_world import FakeClock

pytestmark = pytest.mark.services


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def redis(database_url: str) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(get_settings().redis_url)
    await client.flushdb()  # database 15: test data only
    yield client
    await client.aclose()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(observations, "utc_now", clock)
    observations._route_cache.clear()
    return clock


@pytest.fixture
async def world(
    db: None, tmp_path: Path, redis: Redis, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> WorldRunner:
    """T1 imported, three buses placed precisely (01 and 02 bunched mid-route, 03 behind),
    02's GPS out from the start, and a few ticks of observations written."""
    monkeypatch.setenv("WORLD_ROUTE_ID", "T1")
    get_settings.cache_clear()
    routes_dir = tmp_path / "routes"
    write_route_config(routes_dir)
    imported = prepare("T1", write_feed(tmp_path / "feed.zip"), routes_dir)
    async with get_sessionmaker()() as session:
        await write_route(session, imported)
        route: WorldRoute = await load_world_route(session, "T1")
    runner = WorldRunner(
        route, get_sessionmaker(), redis, seed=1, speed=1, stale_after_s=60,
        bunching_ratio=0.5, closing_ratio=0.75, queue_every_s=10,
        retention=timedelta(hours=6), clock=clock,
    )  # fmt: skip
    obs = Observations(
        time_s=8 * 3600,
        vehicles=(
            VehicleObservation("T1-01", 0, 3000.0, load=20),
            VehicleObservation("T1-02", 0, 2950.0, load=5),
            VehicleObservation("T1-03", 0, 1000.0, load=10),
        ),
    )
    runner.sim = from_observations(route.model, obs, runner.sim.config, seed=1)
    runner._dropouts = (  # T1-02 never reports
        type("D", (), {"bus_id": "T1-02", "start_s": 0, "duration_s": 10_000})(),
    )
    await runner.setup()
    for _ in range(8):  # 80 s of world time, queue counts every 10 s
        clock.advance(10)
        await runner.tick(10)
    return runner


async def call(server: Any, tool: str, **arguments: Any) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, arguments)


def error_of(result: Any) -> ToolErrorBody:
    assert result.is_error, result
    body = parse_tool_error(result.content[0].text)
    assert body is not None, result.content[0].text
    return body


# ------------------------------------------------------------------ discovery


async def test_both_servers_list_their_tools_with_schemas() -> None:
    async with Client(avl) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        templates = (await client.list_resource_templates()).resource_templates
    assert set(tools) == {"get_vehicle_positions", "get_headways"}
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.annotations.idempotent_hint is True
        assert tool.output_schema is not None  # structured, Pydantic-typed results
    assert "is_stale" in json.dumps(tools["get_vehicle_positions"].output_schema)
    assert [t.uri_template for t in templates] == ["route://{route_id}/shape"]

    async with Client(demand) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_boarding_estimates", "send_driver_instruction"}
    send = tools["send_driver_instruction"]
    assert send.annotations is not None
    assert send.annotations.read_only_hint is False and send.annotations.idempotent_hint is True
    assert set(send.input_schema["required"]) == {
        "instruction_id", "approval_id", "vehicle_id", "action",
    }  # fmt: skip
    assert {"hold", "skip_stop", "short_turn"} <= set(json.dumps(send.input_schema).split('"'))


# ------------------------------------------------------------------ avl-mcp


async def test_vehicle_positions_with_server_side_staleness(
    world: WorldRunner, clock: FakeClock
) -> None:
    result = await call(avl, "get_vehicle_positions", route_id="T1")
    positions = VehiclePositions.model_validate(result.structured_content)
    assert {v.fleet_number for v in positions.vehicles} == {"T1-01", "T1-03"}
    assert positions.missing == ["T1-02"]  # its GPS never reported
    first = next(v for v in positions.vehicles if v.fleet_number == "T1-01")
    assert first.direction_id == 0 and not first.is_stale and first.age_seconds == 0
    assert first.world_time_s == 8 * 3600 + 80  # the world's clock, for the what-if model
    assert first.distance_m is not None and first.distance_m > 3000

    clock.advance(61)  # no new GPS for a minute: every fix goes stale, computed on request
    stale = VehiclePositions.model_validate(
        (await call(avl, "get_vehicle_positions", route_id="T1")).structured_content
    )
    assert all(v.is_stale and v.age_seconds == 61 for v in stale.vehicles)


async def test_headways_and_data_quality(world: WorldRunner, clock: FakeClock) -> None:
    headways = Headways.model_validate((await call(avl, "get_headways")).structured_content)
    assert headways.route_id == "T1"  # defaults to the live world's route
    (pair,) = headways.pairs  # T1-02 is silent: only 01 and 03 are paired
    assert (pair.leader_fleet_number, pair.follower_fleet_number) == ("T1-01", "T1-03")
    assert pair.status == "ok" and pair.data_quality == "ok"
    clock.advance(61)
    stale = Headways.model_validate((await call(avl, "get_headways")).structured_content)
    assert stale.pairs[0].status == "stale" and stale.pairs[0].data_quality == "stale"


async def test_route_shape_resource(world: WorldRunner) -> None:
    async with Client(avl) as client:
        result = await client.read_resource("route://T1/shape")
        shape = json.loads(result.contents[0].text)  # type: ignore[union-attr]
        assert shape["id"] == "T1" and len(shape["directions"]) == 2
        assert shape["directions"][0]["shape"]["geometry"]["type"] == "LineString"
        with pytest.raises(Exception, match="unknown route"):
            await client.read_resource("route://NOPE/shape")


async def test_unknown_route_is_a_structured_error(world: WorldRunner) -> None:
    error = error_of(await call(avl, "get_vehicle_positions", route_id="NOPE"))
    assert error.code == "route_not_found"
    bad = await call(avl, "get_vehicle_positions", route_id="../etc")
    assert bad.is_error  # input validation, before any query


# ------------------------------------------------------------------ demand-mcp


async def test_boarding_estimates(world: WorldRunner, clock: FakeClock) -> None:
    result = await call(demand, "get_boarding_estimates", direction_id=0, horizon_minutes=10)
    estimates = BoardingEstimates.model_validate(result.structured_content)
    assert len(estimates.stops) == 15  # 16 stops, nobody boards at the last
    assert all(s.direction_id == 0 and s.data_quality == "ok" for s in estimates.stops)
    assert all(
        s.waiting_now is not None and s.arrivals_per_min is not None for s in estimates.stops
    )
    for s in estimates.stops:
        assert s.expected_boardings == pytest.approx(
            (s.waiting_now or 0) + (s.arrivals_per_min or 0) * 10, abs=0.11
        )
    clock.advance(61)
    stale = BoardingEstimates.model_validate(
        (await call(demand, "get_boarding_estimates")).structured_content
    )
    assert len(stale.stops) == 30 and {s.data_quality for s in stale.stops} == {"stale"}
    bad = await call(demand, "get_boarding_estimates", horizon_minutes=500)
    assert bad.is_error


# ------------------------------------------------------------------ send_driver_instruction


async def approve(
    runner: WorldRunner,
    fleet: str,
    action: ActionType,
    params: dict[str, Any],
    decision: ApprovalDecision = ApprovalDecision.APPROVED,
    edited: dict[str, Any] | None = None,
) -> uuid.UUID:
    """The workflow rows B10-B12 will create: run -> candidate -> recommendation -> approval."""
    async with get_sessionmaker()() as session:
        officer = await session.scalar(select(User).where(User.role == Role.OFFICER))
        assert officer is not None
        run = WorkflowRun()
        session.add(run)
        await session.flush()
        candidate = CandidateAction(
            run_id=run.id, action_type=action, vehicle_id=runner.vehicle_ids[fleet],
            params=params, source=CandidateSource.RULE,
        )  # fmt: skip
        session.add(candidate)
        await session.flush()
        rec = Recommendation(
            run_id=run.id, candidate_id=candidate.id, ranking=[],
            benefit_passenger_minutes=100, explanation="test",
        )  # fmt: skip
        session.add(rec)
        await session.flush()
        approval = Approval(
            recommendation_id=rec.id, officer_id=officer.id, decision=decision,
            edited_params=edited, reason="no" if decision == ApprovalDecision.REJECTED else None,
        )  # fmt: skip
        session.add(approval)
        await session.commit()
        return approval.id


async def send(runner: WorldRunner, approval_id: uuid.UUID, fleet: str, **action: Any) -> Any:
    return await call(
        demand,
        "send_driver_instruction",
        instruction_id=action.pop("instruction_id", f"ins-{uuid.uuid4().hex[:12]}"),
        approval_id=str(approval_id),
        vehicle_id=str(runner.vehicle_ids[fleet]),
        action=action,
    )


async def instruction_count() -> int:
    async with get_sessionmaker()() as session:
        return await session.scalar(select(func.count()).select_from(DriverInstruction)) or 0


async def test_approved_hold_is_sent_once_and_replays_are_safe(
    world: WorldRunner, accounts: dict[str, Account], redis: Redis
) -> None:
    async with get_sessionmaker()() as session:
        driver = await session.scalar(select(User).where(User.role == Role.DRIVER))
        assert driver is not None
        session.add(DriverAssignment(driver_id=driver.id, vehicle_id=world.vehicle_ids["T1-01"]))
        await session.commit()
    approval = await approve(world, "T1-01", ActionType.HOLD, {"seconds": 60})

    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(INSTRUCTION_CHANNEL)
        await pubsub.get_message(timeout=1)
        result = await send(
            world, approval, "T1-01", type="hold", seconds=60, instruction_id="ins-0001-a"
        )
        sent = InstructionSent.model_validate(result.structured_content)
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
    assert sent.status == "sent" and sent.driver_id == driver.id and sent.fleet_number == "T1-01"
    assert message is not None and json.loads(message["data"])["instruction_id"] == "ins-0001-a"

    # Replay (e.g. a retry after a timeout): the original comes back, nothing is resent.
    again = await send(
        world, approval, "T1-01", type="hold", seconds=60, instruction_id="ins-0001-a"
    )
    replay = InstructionSent.model_validate(again.structured_content)
    assert replay.status == "already_sent" and replay.id == sent.id
    # The same key for a different instruction is refused.
    conflict = await send(
        world, approval, "T1-01", type="hold", seconds=90, instruction_id="ins-0001-a"
    )
    assert error_of(conflict).code == "instruction_id_conflict"
    # A new key can't reuse the approval (a double-clicked Approve).
    double = await send(world, approval, "T1-01", type="hold", seconds=60)
    assert error_of(double).code == "approval_already_used"
    assert await instruction_count() == 1


async def test_unapproved_and_mismatched_requests_are_refused(
    world: WorldRunner, accounts: dict[str, Account]
) -> None:
    rejected = await approve(
        world, "T1-01", ActionType.HOLD, {"seconds": 60}, ApprovalDecision.REJECTED
    )
    assert (
        error_of(await send(world, rejected, "T1-01", type="hold", seconds=60)).code
        == "not_approved"
    )
    missing = error_of(await send(world, uuid.uuid4(), "T1-01", type="hold", seconds=60))
    assert missing.code == "approval_not_found"

    approval = await approve(world, "T1-01", ActionType.HOLD, {"seconds": 60})
    mismatches: list[tuple[str, dict[str, Any]]] = [
        ("T1-01", {"type": "hold", "seconds": 120}),  # more than approved
        ("T1-03", {"type": "hold", "seconds": 60}),  # another bus
        ("T1-01", {"type": "skip_stop", "stop_ids": ["S9"]}),  # another action
    ]
    for fleet, action in mismatches:
        assert error_of(await send(world, approval, fleet, **action)).code == "approval_mismatch"

    # An officer's edit is what gets approved.
    edited = await approve(
        world, "T1-01", ActionType.HOLD, {"seconds": 60}, ApprovalDecision.EDITED, {"seconds": 90}
    )
    assert (
        error_of(await send(world, edited, "T1-01", type="hold", seconds=60)).code
        == "approval_mismatch"
    )
    ok = await send(world, edited, "T1-01", type="hold", seconds=90)
    assert InstructionSent.model_validate(ok.structured_content).status == "sent"
    assert await instruction_count() == 1


async def test_stale_or_silent_buses_are_never_instructed(
    world: WorldRunner, clock: FakeClock, accounts: dict[str, Account]
) -> None:
    silent = await approve(world, "T1-02", ActionType.HOLD, {"seconds": 60})
    assert (
        error_of(await send(world, silent, "T1-02", type="hold", seconds=60)).code == "no_position"
    )

    approval = await approve(world, "T1-01", ActionType.HOLD, {"seconds": 60})
    clock.advance(61)  # the approval is fine; the GPS isn't, at send time
    error = error_of(await send(world, approval, "T1-01", type="hold", seconds=60))
    assert error.code == "stale_vehicle" and error.details["age_seconds"] == 61
    assert await instruction_count() == 0


async def test_action_must_be_valid_for_the_bus_position(
    world: WorldRunner, accounts: dict[str, Account]
) -> None:
    # T1-01 is ~3000 m into direction 0 (stops every 400 m): S7 is behind it, S15 the terminal.
    impossible: list[tuple[dict[str, Any], ActionType]] = [
        ({"stop_ids": ["S5"]}, ActionType.SKIP_STOP),
        ({"stop_ids": ["S15"]}, ActionType.SKIP_STOP),
        ({"stop_ids": ["S9", "S9"]}, ActionType.SKIP_STOP),
        ({"at_stop_id": "S3"}, ActionType.SHORT_TURN),
        ({"at_stop_id": "NOWHERE"}, ActionType.SHORT_TURN),
    ]
    for params, action in impossible:
        approval = await approve(world, "T1-01", action, params)
        result = await send(world, approval, "T1-01", type=action.value, **params)
        assert error_of(result).code == "invalid_action", params

    skip = await approve(world, "T1-01", ActionType.SKIP_STOP, {"stop_ids": ["S9", "S10"]})
    assert not (await send(world, skip, "T1-01", type="skip_stop", stop_ids=["S9", "S10"])).is_error
    turn = await approve(world, "T1-03", ActionType.SHORT_TURN, {"at_stop_id": "S8"})
    assert not (await send(world, turn, "T1-03", type="short_turn", at_stop_id="S8")).is_error


async def test_concurrent_sends_for_one_approval_send_exactly_once(
    world: WorldRunner, accounts: dict[str, Account]
) -> None:
    approval = await approve(world, "T1-01", ActionType.HOLD, {"seconds": 60})
    results = await asyncio.gather(
        *(send(world, approval, "T1-01", type="hold", seconds=60) for _ in range(5))
    )
    assert sum(not r.is_error for r in results) == 1
    assert {error_of(r).code for r in results if r.is_error} == {"approval_already_used"}
    assert await instruction_count() == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"instruction_id": "short", "action": {"type": "hold", "seconds": 60}},
        {"action": {"type": "hold", "seconds": 0}},
        {"action": {"type": "hold", "seconds": 60, "extra": 1}},
        {"action": {"type": "do_nothing"}},
        {"action": {"type": "skip_stop", "stop_ids": []}},
    ],
)
async def test_malformed_requests_fail_validation(
    world: WorldRunner, arguments: dict[str, Any]
) -> None:
    base = {
        "instruction_id": "ins-valid-01",
        "approval_id": str(uuid.uuid4()),
        "vehicle_id": str(world.vehicle_ids["T1-01"]),
    }
    result = await call(demand, "send_driver_instruction", **(base | arguments))
    assert result.is_error
    assert await instruction_count() == 0
