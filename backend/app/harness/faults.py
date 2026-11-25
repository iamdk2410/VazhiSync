"""Failure injection (B17): make a scenario go wrong on purpose, to watch the system cope.

A `FaultPlan` can kill an MCP server, slow a tool down or make a tool return bad JSON. It is
set with a scenario (`POST /lab/scenarios/{id}/apply`) or toggled on the running world
(`PUT /lab/world/faults`), stored in Redis, and applied by `FaultyConnector`: the worker's
harness connector, so the advisor experiences exactly what a real outage looks like (a dead
server is unreachable, a slow tool times out, bad JSON fails validation) and every safeguard
downstream - retries, the availability circuit, degraded mode, validation - is exercised.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import structlog
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.harness.client import Connector, Transient
from app.harness.specs import ServerName, ToolName
from app.live.channels import FAULTS_KEY

log = structlog.get_logger()

FaultSource = Callable[[], Awaitable["FaultPlan"]]
SERVER_NAMES = {"avl": "avl-mcp", "demand": "demand-mcp"}


class FaultPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    down: list[ServerName] = Field(default_factory=list)  # servers that don't answer at all
    slow: dict[ToolName, Annotated[float, Field(gt=0, le=60)]] = Field(default_factory=dict)
    bad_json: list[ToolName] = Field(default_factory=list)  # tools whose results are garbage

    @property
    def active(self) -> bool:
        return bool(self.down or self.slow or self.bad_json)

    def describe(self) -> list[str]:
        out = [f"{SERVER_NAMES.get(s, s)} is down" for s in self.down]
        out += [f"{tool} is slowed by {secs:g} s" for tool, secs in sorted(self.slow.items())]
        out += [f"{tool} returns bad JSON" for tool in self.bad_json]
        return out


NO_FAULTS = FaultPlan()


def redis_fault_source(redis: Redis, ttl_s: float = 2.0) -> FaultSource:
    """Read the plan from Redis, cached for a moment (one Redis read per couple of seconds,
    not per tool call). A missing or unreadable plan means no faults."""
    cached: dict[str, Any] = {"plan": NO_FAULTS, "at": float("-inf")}

    async def source() -> FaultPlan:
        if time.monotonic() - cached["at"] < ttl_s:
            plan: FaultPlan = cached["plan"]
            return plan
        try:
            raw = await redis.get(FAULTS_KEY)
            plan = FaultPlan.model_validate_json(raw) if raw else NO_FAULTS
        except (RedisError, ValidationError) as exc:
            log.warning("faults_unreadable", error=repr(exc)[:200])
            plan = NO_FAULTS
        cached.update(plan=plan, at=time.monotonic())
        return plan

    return source


class FaultyConnector:
    """Wraps the real connector and injects the current plan's faults."""

    def __init__(
        self,
        inner: Connector,
        source: FaultSource,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._inner = inner
        self._source = source
        self._sleep = sleep

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> CallToolResult:
        plan = await self._source()
        if server in plan.down:
            raise Transient(f"{server}: connection refused (fault injection)")
        delay = plan.slow.get(tool)  # type: ignore[call-overload]
        if delay:
            await self._sleep(min(delay, timeout_s))
            if delay >= timeout_s:
                raise Transient(
                    f"{server}: {tool} timed out after {timeout_s:g} s (fault injection)"
                )
        result = await self._inner.call(server, tool, arguments, timeout_s)
        if tool in plan.bad_json:
            return CallToolResult(
                content=[TextContent(type="text", text='{"vehicles": [{"fleet_numb')],
                structured_content={"garbage": True, "note": json.dumps("fault injection")},
            )
        return result

    async def list_tools(self, server: str, timeout_s: float) -> list[Tool]:
        if server in (await self._source()).down:
            raise Transient(f"{server}: connection refused (fault injection)")
        return await self._inner.list_tools(server, timeout_s)
