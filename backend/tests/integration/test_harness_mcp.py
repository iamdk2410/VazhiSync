"""B8 acceptance: the harness against real MCP servers over HTTP.

Killing demand-mcp produces a logged "tool unavailable" and the system degrades instead of
crashing; the gate and the send pre-hook refuse unapproved or stale sends before they reach
the server. Uses the B7 test world (T1 route, runner-written observations, fake clock).
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, Tool
from sqlalchemy import func, select
from structlog.testing import capture_logs

from app.config import get_settings
from app.db.enums import ActionType
from app.db.models import DriverInstruction
from app.db.session import get_sessionmaker
from app.harness.budget import RunBudget
from app.harness.client import MCPConnector
from app.harness.errors import HookRejected, ToolNotAllowed, ToolUnavailable
from app.harness.harness import ToolHarness
from app.runner.world import WorldRunner
from mcp_servers.avl_mcp.server import server as avl_server
from mcp_servers.demand_mcp.server import server as demand_server
from mcp_servers.schemas import BoardingEstimates, InstructionSent, VehiclePositions
from tests.conftest import Account
from tests.integration.test_live_world import FakeClock
from tests.integration.test_mcp_servers import approve, clock, redis, world  # noqa: F401

pytestmark = pytest.mark.services


class CountingConnector:
    """The real connector, counting what actually leaves the process."""

    def __init__(self, inner: MCPConnector) -> None:
        self.inner = inner
        self.sent: list[str] = []

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> CallToolResult:
        self.sent.append(tool)
        return await self.inner.call(server, tool, arguments, timeout_s)

    async def list_tools(self, server: str, timeout_s: float) -> list[Tool]:
        return await self.inner.list_tools(server, timeout_s)


class Server:
    def __init__(self, mcp_server: Any) -> None:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*"]
        )
        app = mcp_server.streamable_http_app(transport_security=security)
        self.uvicorn = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> str:
        self.task = asyncio.create_task(self.uvicorn.serve())
        for _ in range(200):
            if self.uvicorn.started:
                break
            await asyncio.sleep(0.05)
        port = self.uvicorn.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        self.uvicorn.should_exit = True
        if self.task:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self.task, timeout=10)


@pytest.fixture
async def servers(world: WorldRunner) -> AsyncIterator[tuple[Server, str, Server, str]]:  # noqa: F811
    avl, demand = Server(avl_server), Server(demand_server)
    avl_url, demand_url = await avl.start(), await demand.start()
    yield avl, avl_url, demand, demand_url
    await demand.stop()
    await avl.stop()


def _budget() -> RunBudget:
    return RunBudget.from_settings(uuid.uuid4(), get_settings())


async def _instructions() -> int:
    async with get_sessionmaker()() as session:
        return await session.scalar(select(func.count()).select_from(DriverInstruction)) or 0


async def test_killing_demand_mcp_degrades_gracefully(
    world: WorldRunner,  # noqa: F811
    servers: tuple[Server, str, Server, str],
    accounts: dict[str, Account],
) -> None:
    _, avl_url, demand, demand_url = servers
    connector = CountingConnector(MCPConnector({"avl": avl_url, "demand": demand_url}))
    harness = ToolHarness.from_settings(get_settings(), get_sessionmaker(), connector)

    # Startup discovery over HTTP, logged.
    with capture_logs() as logs:
        found = await harness.discover()
    assert found["avl"] == ["get_headways", "get_vehicle_positions"]
    assert found["demand"] == ["get_boarding_estimates", "send_driver_instruction"]
    assert sum(e["event"] == "tools_discovered" for e in logs) == 2

    # Healthy: an approved hold goes all the way through, gate -> hook -> server.
    run = _budget()
    estimates = await harness.call(run, "impact_simulator", "get_boarding_estimates", {})
    assert isinstance(estimates, BoardingEstimates) and estimates.route_id == "T1"
    approval = await approve(world, "T1-01", ActionType.HOLD, {"seconds": 60})
    sent = await harness.call(
        run,
        "dispatch",
        "send_driver_instruction",
        {
            "instruction_id": "harness-001",
            "approval_id": str(approval),
            "vehicle_id": str(world.vehicle_ids["T1-01"]),
            "action": {"type": "hold", "seconds": 60},
        },
    )
    assert isinstance(sent, InstructionSent) and sent.status == "sent"

    # Kill demand-mcp.
    await demand.stop()
    run = _budget()
    with capture_logs() as logs:
        with pytest.raises(ToolUnavailable):
            await harness.call(run, "action_generator", "get_boarding_estimates", {})
        degraded = await harness.call_optional(
            run, "impact_simulator", "get_boarding_estimates", {}
        )
        positions = await harness.call(run, "headway_monitor", "get_vehicle_positions", {})
    assert next(e for e in logs if e["event"] == "tool_unavailable")["server"] == "demand"
    assert degraded.value is None and degraded.degraded is not None  # no crash: degraded mode
    assert any(e["event"] == "running_degraded" for e in logs)
    assert isinstance(positions, VehiclePositions) and positions.vehicles  # avl-mcp unaffected
    assert harness.availability.unavailable_servers() == ["demand"]


async def test_unapproved_or_stale_sends_never_leave_the_process(
    world: WorldRunner,  # noqa: F811
    servers: tuple[Server, str, Server, str],
    clock: FakeClock,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    _, avl_url, _, demand_url = servers
    connector = CountingConnector(MCPConnector({"avl": avl_url, "demand": demand_url}))
    harness = ToolHarness.from_settings(get_settings(), get_sessionmaker(), connector)
    vehicle = str(world.vehicle_ids["T1-01"])
    hold = {"type": "hold", "seconds": 60}

    def args(
        approval: object, action: dict[str, Any] = hold, key: str | None = None
    ) -> dict[str, Any]:
        return {
            "instruction_id": key or f"h-{uuid.uuid4().hex[:10]}",
            "approval_id": str(approval),
            "vehicle_id": vehicle,
            "action": action,
        }

    # The gate: only the dispatch step may send at all.
    with pytest.raises(ToolNotAllowed):
        await harness.call(
            _budget(), "action_generator", "send_driver_instruction", args(uuid.uuid4())
        )

    # The pre-hook, before the server: no approval, a different action, a reused approval.
    cases: list[tuple[dict[str, Any], str]] = [(args(uuid.uuid4()), "not_approved")]
    approval = await approve(world, "T1-01", ActionType.HOLD, {"seconds": 60})
    cases.append((args(approval, {"type": "hold", "seconds": 300}), "approval_mismatch"))
    for arguments, code in cases:
        with pytest.raises(HookRejected) as err:
            await harness.call(_budget(), "dispatch", "send_driver_instruction", arguments)
        assert err.value.code == code
    assert "send_driver_instruction" not in connector.sent

    await harness.call(
        _budget(), "dispatch", "send_driver_instruction", args(approval, key="h-first-01")
    )
    with pytest.raises(HookRejected) as err:
        await harness.call(_budget(), "dispatch", "send_driver_instruction", args(approval))
    assert err.value.code == "approval_already_used"
    assert connector.sent.count("send_driver_instruction") == 1

    # Stale at send time (checked through avl-mcp, server-side staleness).
    fresh = await approve(world, "T1-03", ActionType.HOLD, {"seconds": 60})
    clock.advance(61)
    with pytest.raises(HookRejected) as err:
        await harness.call(
            _budget(),
            "dispatch",
            "send_driver_instruction",
            args(fresh) | {"vehicle_id": str(world.vehicle_ids["T1-03"])},
        )
    assert err.value.code == "stale_vehicle"
    assert connector.sent.count("send_driver_instruction") == 1
    assert await _instructions() == 1


async def test_send_fails_closed_when_freshness_cannot_be_checked(
    world: WorldRunner,  # noqa: F811
    servers: tuple[Server, str, Server, str],
    accounts: dict[str, Account],
) -> None:
    avl, _, _, demand_url = servers
    await avl.stop()  # avl-mcp down: GPS freshness can't be verified
    harness = ToolHarness.from_settings(
        get_settings(),
        get_sessionmaker(),
        MCPConnector({"avl": "http://127.0.0.1:9/mcp", "demand": demand_url}),
    )
    approval = await approve(world, "T1-01", ActionType.HOLD, {"seconds": 60})
    with pytest.raises(HookRejected) as err:
        await harness.call(
            _budget(),
            "dispatch",
            "send_driver_instruction",
            {
                "instruction_id": "h-closed-01",
                "approval_id": str(approval),
                "vehicle_id": str(world.vehicle_ids["T1-01"]),
                "action": {"type": "hold", "seconds": 60},
            },
        )
    assert err.value.code == "freshness_unknown"
    assert await _instructions() == 0
