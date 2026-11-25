"""B17 reliability, without services: the live hub, the world loop, MCP host checks and the
failure-injection connector."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from mcp.types import CallToolResult, TextContent
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.db.enums import Role
from app.harness.budget import RunBudget
from app.harness.client import Availability
from app.harness.errors import ToolResultInvalid, ToolUnavailable
from app.harness.faults import FaultPlan, FaultyConnector, redis_fault_source
from app.harness.harness import ToolHarness
from app.live import channels
from app.live.hub import QUEUE_SIZE, LiveHub
from app.live.schemas import LiveFrame
from app.runner import main as runner_main
from app.runner.route_loader import RouteNotImported
from app.runner.world import WorldRunner
from mcp_servers.avl_mcp.server import server as avl_server
from mcp_servers.common import transport_security


def frame(n: int) -> LiveFrame:
    return LiveFrame.model_validate(
        {
            "type": "frame",
            "route_id": "32B",
            "sim_time": f"08:00:{n:02d}",
            "sim_elapsed_s": float(n),
            "speed": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "vehicles": [],
            "queues": [],
            "headways": [],
            "events": [],
        }
    )


# ------------------------------------------------------------------ live hub


def test_a_slow_websocket_client_only_ever_holds_the_newest_frames() -> None:
    hub = LiveHub(redis=None)  # type: ignore[arg-type]
    slow = hub.subscribe(Role.OFFICER, None)  # never reads
    for n in range(10):
        hub.dispatch(frame(n).model_dump_json())
    assert slow.queue.qsize() == QUEUE_SIZE  # bounded: memory can't grow
    newest = [slow.queue.get_nowait().sim_time for _ in range(QUEUE_SIZE)]
    assert newest == ["08:00:08", "08:00:09"]  # the oldest were dropped


def test_a_malformed_frame_on_the_channel_is_dropped() -> None:
    hub = LiveHub(redis=None)  # type: ignore[arg-type]
    sub = hub.subscribe(Role.OFFICER, None)
    hub.dispatch(b"{not json")
    hub.dispatch(json.dumps({"type": "frame"}))  # valid JSON, wrong shape
    assert sub.queue.empty() and hub.last is None
    hub.dispatch(frame(1).model_dump_json())
    assert sub.queue.qsize() == 1  # later good frames still flow


# ------------------------------------------------------------------ world loop


class Stop(Exception):
    """Ends the infinite world loop in a test."""


class FakeRunner:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.ticks = 0

    async def setup(self) -> None:
        return None

    async def tick(self, dt: float) -> None:
        self.ticks += 1
        if self.ticks <= self.failures:
            raise OperationalError("SELECT 1", {}, Exception("postgres is down"))


async def run_loop(monkeypatch: pytest.MonkeyPatch, **patches: Any) -> list[float]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 6:
            raise Stop

    async def pool(*args: Any, **kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(runner_main, "create_pool", pool)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    for name, value in patches.items():
        monkeypatch.setattr(runner_main, name, value)
    with pytest.raises(Stop):
        await runner_main.world_loop(redis=None)  # type: ignore[arg-type]
    return slept


async def test_postgres_down_while_the_world_runs_backs_off_and_carries_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRunner(failures=4)

    async def route(*args: Any) -> object:
        return object()

    monkeypatch.setattr(WorldRunner, "from_settings", lambda *a, **k: fake)
    slept = await run_loop(monkeypatch, load_world_route=route)
    assert slept[:4] == [2, 4, 8, 16]  # exponential backoff while the database is down
    assert fake.ticks >= 5  # then it ticks normally again


async def test_route_not_imported_when_the_runner_starts_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(*args: Any) -> object:
        raise RouteNotImported("route 32B is not imported")

    slept = await run_loop(monkeypatch, load_world_route=missing)
    assert set(slept) == {runner_main.ROUTE_RETRY_SECONDS}  # waits and tries again


# ------------------------------------------------------------------ MCP servers


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def post_init(*header_sets: dict[str, str]) -> list[int]:
    """POST an MCP initialize with each set of extra headers (lifespan in this one task)."""
    app = avl_server.streamable_http_app(transport_security=transport_security("avl-mcp"))
    codes: list[int] = []
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8101") as c:
            for extra in header_sets:
                codes.append((await c.post("/mcp", json=INIT, headers=HEADERS | extra)).status_code)
    return codes


async def test_mcp_rejects_an_unexpected_host_header() -> None:
    ok, rebound = await post_init({}, {"Host": "evil.example"})
    assert ok == 200
    assert rebound in (400, 403, 421)


async def test_mcp_rejects_a_foreign_browser_origin() -> None:
    local, foreign = await post_init(
        {"Origin": "http://localhost:5173"}, {"Origin": "https://evil.example"}
    )
    assert local == 200
    assert foreign == 403


# ------------------------------------------------------------------ failure injection


class Healthy:
    """A connector whose servers always answer with valid positions."""

    def __init__(self) -> None:
        self.calls = 0

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> CallToolResult:
        self.calls += 1
        body = {
            "route_id": "32B",
            "server_time": datetime.now(UTC).isoformat(),
            "stale_after_seconds": 60,
            "vehicles": [],
            "missing": [],
        }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(body))], structured_content=body
        )

    async def list_tools(self, server: str, timeout_s: float) -> list[Any]:
        return []


def harness_with(plan: FaultPlan, inner: Healthy, slept: list[float]) -> ToolHarness:
    async def source() -> FaultPlan:
        return plan

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return ToolHarness(
        FaultyConnector(inner, source, sleep=sleep),
        timeout_s=2.0,
        attempts=2,
        availability=Availability(30),
        sleep=sleep,
    )


def budget() -> RunBudget:
    return RunBudget(uuid.uuid4(), 60, 10, 10)


async def call(h: ToolHarness) -> Any:
    return await h.call(budget(), "headway_monitor", "get_vehicle_positions", {"route_id": "32B"})


async def test_a_killed_server_looks_unreachable_and_trips_the_circuit() -> None:
    inner, slept = Healthy(), cast(list[float], [])
    h = harness_with(FaultPlan(down=["avl"]), inner, slept)
    with pytest.raises(ToolUnavailable):
        await call(h)
    assert inner.calls == 0 and h.availability.unavailable_servers() == ["avl"]


async def test_a_slowed_tool_times_out_past_the_limit_and_is_merely_slow_below_it() -> None:
    inner, slept = Healthy(), cast(list[float], [])
    with pytest.raises(ToolUnavailable):
        await call(harness_with(FaultPlan(slow={"get_vehicle_positions": 5.0}), inner, slept))
    assert slept[0] == 2.0  # waited the full timeout, then gave up (and retried, bounded)
    slept.clear()
    result = await call(harness_with(FaultPlan(slow={"get_vehicle_positions": 0.5}), inner, slept))
    assert result.route_id == "32B" and slept == [0.5]


async def test_bad_json_from_a_tool_fails_validation_and_is_never_retried() -> None:
    inner, slept = Healthy(), cast(list[float], [])
    with pytest.raises(ToolResultInvalid):
        await call(harness_with(FaultPlan(bad_json=["get_vehicle_positions"]), inner, slept))
    assert inner.calls == 1  # validation errors are not retried (rule 7)


async def test_no_faults_change_nothing_and_the_plan_reads_back_from_redis() -> None:
    inner, slept = Healthy(), cast(list[float], [])
    assert (await call(harness_with(FaultPlan(), inner, slept))).route_id == "32B"

    stored = FaultPlan(down=["demand"], slow={"get_headways": 3}, bad_json=["get_headways"])

    class FakeRedis:
        async def get(self, key: str) -> str | None:
            assert key == channels.FAULTS_KEY
            return stored.model_dump_json()

    assert await redis_fault_source(FakeRedis(), ttl_s=0)() == stored  # type: ignore[arg-type]
    assert stored.describe() == [
        "demand-mcp is down",
        "get_headways is slowed by 3 s",
        "get_headways returns bad JSON",
    ]

    class DownRedis:
        async def get(self, key: str) -> str | None:
            raise RedisConnectionError("redis is down")

    assert await redis_fault_source(DownRedis(), ttl_s=0)() == FaultPlan()  # type: ignore[arg-type]


def test_tests_use_their_own_pubsub_channels() -> None:
    """Redis pub/sub ignores database numbers: the tests must not share the dev stack's
    channels (seen: the dev world's frames leaked into a live-world test)."""
    assert channels.LIVE_CHANNEL.startswith("vazhisync-test:")
    assert channels.INSTRUCTION_CHANNEL.startswith("vazhisync-test:")
