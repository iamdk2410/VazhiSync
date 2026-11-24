"""B8 harness logic with a fake MCP connector: gate, budgets, transient-only retries, the
availability circuit, result validation, degraded optional calls, hooks and discovery."""

import uuid
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent, Tool
from structlog.testing import capture_logs

from app.harness.budget import RunBudget
from app.harness.client import Availability, Transient, is_transient
from app.harness.errors import (
    BudgetExceeded,
    HookRejected,
    ToolFailed,
    ToolNotAllowed,
    ToolResultInvalid,
    ToolUnavailable,
)
from app.harness.harness import ToolHarness
from app.harness.hooks import ToolCall
from app.harness.specs import AGENT_TOOLS
from mcp_servers.schemas import Headways, ToolErrorBody

NOW = "2026-11-24T08:00:00Z"
HEADWAYS = {
    "route_id": "32B",
    "server_time": NOW,
    "bunching_ratio": 0.5,
    "closing_ratio": 0.75,
    "stale_after_seconds": 60,
    "pairs": [],
    "not_in_service": [],
    "missing": [],
}
ESTIMATES = {"route_id": "32B", "server_time": NOW, "horizon_minutes": 10, "stops": []}
POSITIONS = {
    "route_id": "32B",
    "server_time": NOW,
    "stale_after_seconds": 60,
    "vehicles": [],
    "missing": [],
}

DEFAULTS = {
    "get_headways": HEADWAYS,
    "get_vehicle_positions": POSITIONS,
    "get_boarding_estimates": ESTIMATES,
}


def ok(content: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text="{}")], structured_content=content)


def error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


class FakeConnector:
    """Replays scripted outcomes per tool: a CallToolResult, or an exception to raise."""

    def __init__(self, script: dict[str, list[Any]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, str, dict[str, Any], float]] = []
        self.down: set[str] = set()

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> CallToolResult:
        self.calls.append((server, tool, arguments, timeout_s))
        if server in self.down:
            raise Transient(f"{server}: connection refused")
        outcomes = self.script.get(tool, [])
        outcome = outcomes.pop(0) if outcomes else ok(DEFAULTS[tool])
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def list_tools(self, server: str, timeout_s: float) -> list[Tool]:
        if server in self.down:
            raise Transient("down")
        names = {
            "avl": ["get_vehicle_positions", "get_headways"],
            "demand": ["get_boarding_estimates"],
        }
        return [Tool(name=n, input_schema={"type": "object"}) for n in names[server]]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def no_sleep(_: float) -> None:
    return None


def make(connector: FakeConnector, clock: Clock | None = None, **kwargs: Any) -> ToolHarness:
    return ToolHarness(
        connector,
        timeout_s=5,
        attempts=3,
        availability=Availability(30, clock=clock or Clock()),
        sleep=no_sleep,
        **kwargs,
    )


def budget(clock: Clock | None = None, calls: int = 5, limit_s: float = 60) -> RunBudget:
    return RunBudget(
        uuid.uuid4(),
        time_limit_s=limit_s,
        max_tool_calls_per_agent=calls,
        max_steps=4,
        clock=clock or Clock(),
    )


# ------------------------------------------------------------------ gate


async def test_result_is_validated_into_the_tool_model() -> None:
    result = await make(FakeConnector()).call(budget(), "headway_monitor", "get_headways")
    assert isinstance(result, Headways) and result.route_id == "32B"


@pytest.mark.parametrize(
    ("agent", "tool"),
    [
        ("headway_monitor", "send_driver_instruction"),  # rule 5
        ("action_generator", "send_driver_instruction"),
        ("impact_simulator", "send_driver_instruction"),
        ("control_advisor", "get_headways"),
        ("dispatch", "get_boarding_estimates"),
        ("intruder", "get_headways"),
        ("headway_monitor", "drop_tables"),
    ],
)
async def test_the_gate_refuses_tools_outside_the_allowlist(agent: str, tool: str) -> None:
    connector = FakeConnector()
    with capture_logs() as logs, pytest.raises(ToolNotAllowed):
        await make(connector).call(budget(), agent, tool)
    assert connector.calls == []  # never left the process
    assert any(e["event"] == "tool_blocked" for e in logs)


def test_only_dispatch_may_send() -> None:
    senders = [a for a, tools in AGENT_TOOLS.items() if "send_driver_instruction" in tools]
    assert senders == ["dispatch"]


# ------------------------------------------------------------------ budgets


async def test_per_agent_tool_call_budget() -> None:
    harness, run = make(FakeConnector()), budget(calls=2)
    await harness.call(run, "headway_monitor", "get_headways")
    await harness.call(run, "headway_monitor", "get_headways")
    with pytest.raises(BudgetExceeded) as err:
        await harness.call(run, "headway_monitor", "get_headways")
    assert err.value.code == "agent_tool_calls"
    await harness.call(run, "action_generator", "get_headways")  # another agent's budget


async def test_run_time_limit_and_step_cap() -> None:
    clock = Clock()
    run = budget(clock, limit_s=60)
    connector = FakeConnector()
    await make(connector).call(run, "headway_monitor", "get_headways")
    assert connector.calls[0][3] == 5  # per-call timeout: min(tool timeout, time left)
    clock.t += 58
    await make(connector).call(run, "headway_monitor", "get_headways")
    assert connector.calls[1][3] == pytest.approx(2)  # only 2 s of the run left
    clock.t += 3
    with pytest.raises(BudgetExceeded) as err:
        await make(connector).call(run, "headway_monitor", "get_headways")
    assert err.value.code == "run_time"

    steps = budget()
    for node in ("a", "b", "c", "d"):
        steps.charge_step(node)
    with pytest.raises(BudgetExceeded):
        steps.charge_step("e")


# ------------------------------------------------------------------ retries and availability


async def test_transient_errors_are_retried_then_succeed() -> None:
    connector = FakeConnector(
        {"get_headways": [Transient("blip"), Transient("blip"), ok(HEADWAYS)]}
    )
    await make(connector).call(budget(), "headway_monitor", "get_headways")
    assert len(connector.calls) == 3


async def test_server_down_is_marked_unavailable_and_then_fails_fast() -> None:
    clock = Clock()
    connector = FakeConnector()
    connector.down.add("demand")
    harness = make(connector, clock)
    with capture_logs() as logs:
        with pytest.raises(ToolUnavailable) as err:
            await harness.call(budget(), "action_generator", "get_boarding_estimates")
        assert err.value.code == "unavailable" and len(connector.calls) == 3  # bounded
        with pytest.raises(ToolUnavailable) as again:
            await harness.call(budget(), "action_generator", "get_boarding_estimates")
        assert again.value.code == "circuit_open" and len(connector.calls) == 3  # no new call
        await harness.call(budget(), "headway_monitor", "get_headways")  # avl unaffected
    unavailable = [e for e in logs if e["event"] == "tool_unavailable"]
    assert len(unavailable) == 1 and unavailable[0]["server"] == "demand"  # logged once

    connector.down.clear()  # server back; after the cooldown one probe gets through
    clock.t += 31
    with capture_logs() as logs:
        await harness.call(budget(), "action_generator", "get_boarding_estimates")
    assert any(e["event"] == "tool_available_again" for e in logs)


async def test_non_transient_errors_are_not_retried() -> None:
    connector = FakeConnector({"get_headways": [ValueError("bug")]})
    with pytest.raises(ValueError):
        await make(connector).call(budget(), "headway_monitor", "get_headways")
    assert len(connector.calls) == 1


async def test_unavailable_optional_tool_degrades_gracefully() -> None:
    connector = FakeConnector()
    connector.down.add("demand")
    harness = make(connector)
    with capture_logs() as logs:
        result = await harness.call_optional(budget(), "impact_simulator", "get_boarding_estimates")
    assert result.value is None and result.degraded is not None
    assert result.degraded.kind == "unavailable"
    assert any(e["event"] == "running_degraded" for e in logs)
    with pytest.raises(ToolNotAllowed):  # refusals are never "optional"
        await harness.call_optional(budget(), "dispatch", "get_boarding_estimates")


# ------------------------------------------------------------------ result validation


async def test_structured_tool_errors_keep_their_code() -> None:
    body = ToolErrorBody(code="stale_vehicle", message="GPS is 75 s old").model_dump_json()
    connector = FakeConnector(
        {"get_headways": [error(f"Error executing tool get_headways: {body}")]}
    )
    with pytest.raises(ToolFailed) as err:
        await make(connector).call(budget(), "headway_monitor", "get_headways")
    assert err.value.code == "stale_vehicle" and len(connector.calls) == 1  # not retried


@pytest.mark.parametrize(
    ("outcome", "expected", "code"),
    [
        (error("Error executing tool get_headways: 1 validation error"), ToolFailed, "tool_error"),
        (ok({"route_id": "32B"}), ToolResultInvalid, "schema"),
        (
            CallToolResult(content=[TextContent(type="text", text="hi")]),
            ToolResultInvalid,
            "no_structure",
        ),
    ],
)
async def test_bad_results_are_typed_failures(
    outcome: CallToolResult, expected: type, code: str
) -> None:
    with pytest.raises(expected) as err:
        await make(FakeConnector({"get_headways": [outcome]})).call(
            budget(), "headway_monitor", "get_headways"
        )
    assert err.value.code == code  # type: ignore[attr-defined]


# ------------------------------------------------------------------ hooks


async def test_pre_hooks_can_refuse_and_post_hooks_see_results() -> None:
    seen: list[tuple[ToolCall, Any]] = []

    async def refuse_dispatch(call: ToolCall) -> None:
        if call.agent == "dispatch":
            raise HookRejected("nope", tool=call.tool, code="test")

    async def record(call: ToolCall, result: Any) -> None:
        seen.append((call, result))

    connector = FakeConnector()
    harness = make(connector, pre_hooks=[refuse_dispatch], post_hooks=[record])
    await harness.call(budget(), "headway_monitor", "get_headways", {"route_id": "32B"})
    assert seen[0][0].arguments == {"route_id": "32B"} and isinstance(seen[0][1], Headways)
    with capture_logs() as logs, pytest.raises(HookRejected):
        await harness.call(budget(), "dispatch", "send_driver_instruction", {})
    assert len(connector.calls) == 1  # the refused send never left the process
    assert any(e["event"] == "tool_rejected" and e["code"] == "test" for e in logs)


async def test_every_call_is_logged_with_the_run_id() -> None:
    run = budget()
    with capture_logs() as logs:
        await make(FakeConnector()).call(run, "headway_monitor", "get_headways")
    (entry,) = [e for e in logs if e["event"] == "tool_call"]
    assert entry["run_id"] == str(run.run_id) and entry["agent"] == "headway_monitor"


# ------------------------------------------------------------------ discovery and errors


async def test_discovery_logs_tools_and_marks_down_servers() -> None:
    connector = FakeConnector()
    connector.down.add("demand")
    harness = make(connector)
    with capture_logs() as logs:
        found = await harness.discover()
    assert found == {"avl": ["get_headways", "get_vehicle_positions"], "demand": []}
    assert any(e["event"] == "tools_discovered" and e["server"] == "avl" for e in logs)
    assert any(e["event"] == "tool_unavailable" and e["server"] == "demand" for e in logs)
    assert harness.availability.unavailable_servers() == ["demand"]


def test_transient_classification() -> None:
    class ConnectError(Exception):  # stands in for httpx2.ConnectError
        pass

    assert is_transient(ExceptionGroup("x", [ConnectError("refused")]))
    assert is_transient(TimeoutError())
    assert is_transient(ConnectionRefusedError())
    assert not is_transient(ValueError("bug"))
    assert not is_transient(ExceptionGroup("x", [KeyError("k")]))
