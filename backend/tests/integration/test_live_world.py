"""B6 acceptance: buses move in real time, dropout buses stop updating (and go stale), bunching
raises exactly one event, and the WebSocket gives each role only its own slice.

Runs the real runner against the test database and Redis (database 15), with a fake clock so
staleness can be tested without waiting a minute.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from redis.asyncio import Redis
from sqlalchemy import func, select
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from app.auth.security import create_access_token
from app.config import GpsDropout, get_settings
from app.db.enums import Role
from app.db.models import BunchingEvent, DriverAssignment, StopQueue, User, VehiclePosition
from app.db.session import get_sessionmaker
from app.gtfs.import_gtfs import prepare
from app.gtfs.writer import write_route
from app.live.channels import BUNCHING_CHANNEL, LAST_FRAME_KEY, SPEED_KEY
from app.live.schemas import BunchingRaised, LiveFrame
from app.main import create_app
from app.runner.route_loader import WorldRoute, load_world_route
from app.runner.world import WorldRunner
from simulator import Observations, VehicleObservation, from_observations
from tests.conftest import Account
from tests.gtfs_fixtures import write_feed, write_route_config

pytestmark = pytest.mark.services


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
async def redis(database_url: str) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(get_settings().redis_url)
    try:
        await client.ping()
    except OSError as exc:
        pytest.skip(f"Redis not available: {exc!r}")
    await client.flushdb()  # database 15: test data only (tests/conftest.py)
    yield client
    await client.aclose()


@pytest.fixture
async def route(db: None, tmp_path: Path) -> WorldRoute:
    """The synthetic 16-stop T1 route, imported into the test database."""
    routes_dir = tmp_path / "routes"
    write_route_config(routes_dir)
    imported = prepare("T1", write_feed(tmp_path / "feed.zip"), routes_dir)
    async with get_sessionmaker()() as session:
        await write_route(session, imported)
        return await load_world_route(session, "T1")


def _runner(
    route: WorldRoute, redis: Redis, clock: FakeClock, dropouts: list[GpsDropout] | None = None
) -> WorldRunner:
    return WorldRunner(
        route,
        get_sessionmaker(),
        redis,
        seed=1,
        speed=10,
        stale_after_s=60,
        bunching_ratio=0.5,
        closing_ratio=0.75,
        queue_every_s=10,
        retention=timedelta(hours=6),
        dropouts=dropouts or [],
        clock=clock,
    )


async def _tick(runner: WorldRunner, clock: FakeClock, n: int, dt: float = 2.0) -> LiveFrame:
    frame = None
    for _ in range(n):
        clock.advance(dt)
        frame = await runner.tick(dt)
    assert frame is not None
    return frame


async def _positions(fleet_number: str, runner: WorldRunner) -> list[datetime]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(
            select(VehiclePosition.recorded_at)
            .where(VehiclePosition.vehicle_id == runner.vehicle_ids[fleet_number])
            .order_by(VehiclePosition.recorded_at)
        )
        return list(rows)


async def test_buses_move_and_dropout_buses_stop_updating(route: WorldRoute, redis: Redis) -> None:
    clock = FakeClock()
    runner = _runner(
        route, redis, clock, [GpsDropout(bus_id="T1-02", start_s=60, duration_s=10_000)]
    )
    await runner.setup()
    first = await _tick(runner, clock, 1)
    last = await _tick(runner, clock, 9)  # 20 s real at 10x = 200 simulated seconds
    assert last.sim_elapsed_s == 200

    # Buses visibly move: positions change between frames, and every fix is on the route.
    before = {v.fleet_number: (v.direction_id, v.distance_m) for v in first.vehicles}
    after = {v.fleet_number: (v.direction_id, v.distance_m) for v in last.vehicles}
    moved = [b for b in after if after[b] != before[b]]
    assert len(moved) >= len(after) - 3  # a few may be in their terminal layover
    assert all(80.2 < v.lon < 80.4 and 13.0 < v.lat < 13.2 for v in last.vehicles)

    # Moving buses wrote a fix every tick; the dropout bus stopped at 60 simulated seconds.
    assert len(await _positions("T1-01", runner)) == 10
    silent = await _positions("T1-02", runner)
    assert len(silent) == 2  # fixes at 20 s and 40 s, nothing from 60 s on
    t102 = next(v for v in last.vehicles if v.fleet_number == "T1-02")
    assert t102.last_fix_at == silent[-1]
    assert not t102.is_stale  # 16 s old: not stale yet

    clock.advance(60)  # a minute with no GPS from T1-02
    later = await runner.tick(0)
    t102 = next(v for v in later.vehicles if v.fleet_number == "T1-02")
    assert t102.is_stale and t102.age_seconds > 60
    assert not next(v for v in later.vehicles if v.fleet_number == "T1-01").is_stale

    async with get_sessionmaker()() as session:
        queue_rows = await session.scalar(select(func.count()).select_from(StopQueue))
    assert queue_rows == 3 * 32  # at 2 s, 12 s and 80 s (every >= 10 s real), 32 stops each
    cached = await redis.get(LAST_FRAME_KEY)
    assert cached is not None and LiveFrame.model_validate_json(cached).route_id == "T1"


async def test_same_seed_gives_the_same_world(route: WorldRoute, redis: Redis) -> None:
    runs = []
    for _ in range(2):
        clock = FakeClock()
        runner = _runner(route, redis, clock)
        await runner.setup()
        frame = await _tick(runner, clock, 15)
        runs.append([(v.fleet_number, v.distance_m, v.load) for v in frame.vehicles])
    assert runs[0] == runs[1]


async def test_speed_is_read_from_redis_each_tick(route: WorldRoute, redis: Redis) -> None:
    clock = FakeClock()
    runner = _runner(route, redis, clock)
    await runner.setup()
    await redis.set(SPEED_KEY, "5")
    assert (await _tick(runner, clock, 1)).sim_elapsed_s == 10
    await redis.set(SPEED_KEY, "bogus")  # ignored: keeps the last valid speed
    assert (await _tick(runner, clock, 1)).sim_elapsed_s == 20


def _bunched_pair(runner: WorldRunner, route: WorldRoute) -> None:
    """Replace the world with three buses, two of them 100 m apart mid-route."""
    obs = Observations(
        time_s=8 * 3600,
        vehicles=(
            VehicleObservation("T1-01", 0, 3000.0, load=20),
            VehicleObservation("T1-02", 0, 2900.0, load=5),
            VehicleObservation("T1-03", 0, 900.0, load=10),
        ),
    )
    runner.sim = from_observations(route.model, obs, runner.sim.config, seed=1)


async def test_bunching_raises_exactly_one_event(route: WorldRoute, redis: Redis) -> None:
    clock = FakeClock()
    runner = _runner(route, redis, clock)
    _bunched_pair(runner, route)
    await runner.setup()
    async with redis.pubsub() as pubsub:
        await pubsub.subscribe(BUNCHING_CHANNEL)
        await pubsub.get_message(timeout=1)  # subscribe confirmation
        frame = await _tick(runner, clock, 3)
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
        assert message is not None
        raised = BunchingRaised.model_validate_json(message["data"])
        assert await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5) is None

    assert raised.leader_vehicle_id == runner.vehicle_ids["T1-01"]
    assert raised.follower_vehicle_id == runner.vehicle_ids["T1-02"]
    assert len(frame.events) == 1 and frame.events[0].event_id == raised.event_id

    # A restarted runner picks up the open event instead of raising a duplicate.
    restarted = _runner(route, redis, clock)
    _bunched_pair(restarted, route)
    await restarted.setup()
    await _tick(restarted, clock, 2)
    async with get_sessionmaker()() as session:
        events = (await session.scalars(select(BunchingEvent))).all()
    assert [e.id for e in events] == [raised.event_id]
    assert events[0].headway_seconds < 0.5 * events[0].scheduled_headway_seconds


# ------------------------------------------------------------------ WebSocket


@pytest.fixture
async def live_url(redis: Redis) -> AsyncIterator[str]:
    """The real API, served by uvicorn in this event loop (lifespan starts the LiveHub)."""
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=0, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/ws/live"
    server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)


async def _token(email: str) -> str:
    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    return create_access_token(user.id, user.role)[0]


async def _open(url: str, token: str) -> tuple[ClientConnection, dict[str, Any]]:
    ws = await connect(url)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
    assert ready["type"] == "ready"
    return ws, ready


async def _next_frame(
    ws: ClientConnection, check: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    for _ in range(20):
        data: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if data["type"] == "frame" and check(data):
            return data
    raise AssertionError("no matching frame")


async def test_each_role_gets_only_its_slice(
    route: WorldRoute, redis: Redis, accounts: dict[str, Account], live_url: str
) -> None:
    clock = FakeClock()
    runner = _runner(route, redis, clock)
    await runner.setup()
    own_bus = runner.vehicle_ids["T1-03"]
    async with get_sessionmaker()() as session:
        driver = await session.scalar(select(User).where(User.role == Role.DRIVER))
        assert driver is not None
        session.add(DriverAssignment(driver_id=driver.id, vehicle_id=own_bus))
        await session.commit()

    officer, o_ready = await _open(live_url, await _token(accounts["officer"].email))
    driver_ws, d_ready = await _open(live_url, await _token(accounts["driver"].email))
    passenger, p_ready = await _open(live_url, await _token(accounts["passenger"].email))
    assert (o_ready["role"], d_ready["role"], p_ready["role"]) == ("officer", "driver", "passenger")
    assert d_ready["vehicle_id"] == str(own_bus)
    try:
        await asyncio.sleep(0.2)  # let the hub's subscriptions settle
        await _tick(runner, clock, 2)
        has_buses: Callable[[dict[str, Any]], bool] = lambda f: len(f["vehicles"]) > 0  # noqa: E731
        o = await _next_frame(officer, has_buses)
        d = await _next_frame(driver_ws, has_buses)
        p = await _next_frame(passenger, has_buses)
    finally:
        for ws in (officer, driver_ws, passenger):
            await ws.close()

    assert len(o["vehicles"]) == len(runner.vehicle_ids)
    assert all(v["load"] is not None for v in o["vehicles"]) and o["queues"]
    assert [v["vehicle_id"] for v in d["vehicles"]] == [str(own_bus)]
    assert d["queues"] == [] and d["events"] == []
    assert len(p["vehicles"]) == len(runner.vehicle_ids)
    assert all(v["load"] is None and v["crowding"] for v in p["vehicles"])
    assert p["queues"] == [] and p["headways"] == [] and p["events"] == []


@pytest.mark.parametrize(
    "message",
    [
        None,  # says nothing: times out
        {"type": "auth", "token": "not-a-jwt"},
        {"type": "hello"},
        "not json",
    ],
)
async def test_unauthenticated_sockets_are_closed(live_url: str, message: object) -> None:
    ws = await connect(live_url)
    if message is not None:
        await ws.send(message if isinstance(message, str) else json.dumps(message))
    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), timeout=8)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401


async def test_token_for_a_deactivated_user_is_refused(
    live_url: str, accounts: dict[str, Account]
) -> None:
    token = await _token(accounts["passenger"].email)
    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.email == accounts["passenger"].email))
        assert user is not None
        user.is_active = False
        await session.commit()
    ws = await connect(live_url)
    await ws.send(json.dumps({"type": "auth", "token": token}))
    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), timeout=5)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401


async def test_officer_can_set_the_world_speed(
    live_url: str, redis: Redis, accounts: dict[str, Account]
) -> None:
    import httpx

    base = live_url.replace("ws://", "http://").removesuffix("/ws/live")
    async with httpx.AsyncClient(base_url=base) as client:
        officer = {"Authorization": f"Bearer {await _token(accounts['officer'].email)}"}
        passenger = {"Authorization": f"Bearer {await _token(accounts['passenger'].email)}"}
        assert (
            await client.put("/officer/world/speed", json={"speed": 5}, headers=officer)
        ).status_code == 200
        assert await redis.get(SPEED_KEY) == b"5"
        assert (
            await client.put("/officer/world/speed", json={"speed": 7}, headers=officer)
        ).status_code == 422
        assert (
            await client.put("/officer/world/speed", json={"speed": 1}, headers=passenger)
        ).status_code == 403
        status_resp = await client.get("/officer/world", headers=officer)
        assert status_resp.status_code == 200 and status_resp.json()["speed"] == 5
