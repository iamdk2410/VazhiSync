"""ToolHarness: the one way agents call tools (rule 12).

    gate (allowlist) -> budget -> pre-hooks -> call (timeout, transient-only retries,
    circuit breaker) -> result validation -> post-hooks

Every step is logged with the run_id (rule 9). Failures are typed (app.harness.errors) so
the workflow can route on them; `call_optional` turns an unavailable, non-critical tool into
an explicit degraded result instead of a crash.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.harness.budget import RunBudget
from app.harness.client import Availability, Connector, MCPConnector, Transient
from app.harness.errors import (
    HarnessError,
    HookRejected,
    ToolFailed,
    ToolNotAllowed,
    ToolResultInvalid,
    ToolUnavailable,
)
from app.harness.hooks import PostHook, PreHook, SendGuard, ToolCall
from app.harness.specs import AGENT_TOOLS, TOOLS, ToolSpec
from app.harness.telemetry import ToolCallEvent, ToolTelemetry
from mcp_servers.errors import parse_tool_error
from mcp_servers.schemas import VehiclePositions

log = structlog.get_logger()

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Degraded:
    tool: str
    kind: str
    code: str | None
    message: str


@dataclass(frozen=True)
class OptionalResult:
    """A non-critical tool's result, or why the workflow continues without it."""

    value: BaseModel | None
    degraded: Degraded | None = None


def _text(raw: CallToolResult) -> str:
    return " ".join(c.text for c in raw.content if isinstance(c, TextContent))


class ToolHarness:
    def __init__(
        self,
        connector: Connector,
        *,
        timeout_s: float,
        attempts: int,
        availability: Availability,
        pre_hooks: Sequence[PreHook] = (),
        post_hooks: Sequence[PostHook] = (),
        agent_tools: Mapping[str, frozenset[str]] | None = None,
        sleep: Sleep = asyncio.sleep,
        telemetry: Callable[[ToolCallEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._connector = connector
        self._timeout_s = timeout_s
        self._attempts = attempts
        self.availability = availability
        self.pre_hooks = list(pre_hooks)
        self.post_hooks = list(post_hooks)
        self._agent_tools: Mapping[str, frozenset[str]] = (
            agent_tools if agent_tools is not None else {k: v for k, v in AGENT_TOOLS.items()}
        )
        self._sleep = sleep
        self._telemetry = telemetry  # B18: one tool_calls row + trace span per call

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        connector: Connector | None = None,
    ) -> "ToolHarness":
        harness = cls(
            connector
            or MCPConnector({"avl": settings.avl_mcp_url, "demand": settings.demand_mcp_url}),
            timeout_s=settings.tool_timeout_seconds,
            attempts=settings.tool_attempts,
            availability=Availability(settings.tool_unavailable_cooldown_seconds),
            telemetry=ToolTelemetry(sessionmaker),
        )
        harness.pre_hooks.append(SendGuard(sessionmaker, harness.current_positions))
        return harness

    # ------------------------------------------------------------------ discovery

    async def discover(self) -> dict[str, list[str]]:
        """List each server's tools at startup (logged); a down server is marked unavailable."""
        found: dict[str, list[str]] = {}
        for server in sorted({spec.server for spec in TOOLS.values()}):
            try:
                tools = await self._connector.list_tools(server, self._timeout_s)
            except Transient as exc:
                self.availability.failed(server, "*", str(exc))
                found[server] = []
                continue
            self.availability.succeeded(server)
            found[server] = sorted(t.name for t in tools)
            expected = sorted(s.name for s in TOOLS.values() if s.server == server)
            log.info("tools_discovered", server=server, tools=found[server])
            if missing := sorted(set(expected) - set(found[server])):
                log.warning("tools_missing", server=server, missing=missing)
        return found

    # ------------------------------------------------------------------ agent calls

    async def call(
        self, budget: RunBudget, agent: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> BaseModel:
        arguments = dict(arguments or {})
        spec = TOOLS.get(tool)
        # run_id on every line (rule 9): bound explicitly, and in contextvars for nested logs.
        clog = log.bind(run_id=str(budget.run_id), agent=agent, tool=tool)
        with structlog.contextvars.bound_contextvars(
            run_id=str(budget.run_id), agent=agent, tool=tool
        ):
            started, started_at = time.monotonic(), datetime.now(UTC)
            stats = {"attempts": 0}
            outcome: tuple[str, str | None] = ("ok", None)
            try:
                if spec is None or tool not in self._agent_tools.get(agent, frozenset()):
                    clog.warning("tool_blocked", reason="not in the agent's allowlist")
                    raise ToolNotAllowed(
                        f"{agent} may not call {tool}", tool=tool, code="not_allowed"
                    )
                budget.charge_tool_call(agent, tool)
                call = ToolCall(budget.run_id, agent, tool, arguments)
                for hook in self.pre_hooks:
                    await hook(call)
                result = await self._invoke(
                    spec, arguments, min(self._timeout_s, budget.remaining_s), stats
                )
                for post in self.post_hooks:
                    await post(call, result)
            except HarnessError as exc:
                outcome = (exc.kind, exc.code)
                event = "tool_rejected" if isinstance(exc, HookRejected) else "tool_call_failed"
                clog.warning(event, kind=exc.kind, code=exc.code, error=str(exc)[:300])
                raise
            finally:
                duration_ms = round((time.monotonic() - started) * 1000)
                if self._telemetry is not None:
                    await self._telemetry(
                        ToolCallEvent(
                            run_id=budget.run_id,
                            agent=agent,
                            tool=tool,
                            server=spec.server if spec else None,
                            status=outcome[0],
                            error_code=outcome[1],
                            attempts=stats["attempts"],
                            duration_ms=duration_ms,
                            started_at=started_at,
                            arguments=arguments,
                        )
                    )
            clog.info("tool_call", duration_ms=duration_ms, attempts=stats["attempts"])
            return result

    async def call_optional(
        self, budget: RunBudget, agent: str, tool: str, arguments: dict[str, Any] | None = None
    ) -> OptionalResult:
        """For tools the workflow can do without (e.g. demand data): unavailability or a bad
        result degrades gracefully instead of failing the run. Gate, hook and budget refusals
        still raise — those are never "optional"."""
        try:
            return OptionalResult(await self.call(budget, agent, tool, arguments))
        except (ToolUnavailable, ToolResultInvalid) as exc:
            log.warning(
                "running_degraded", run_id=str(budget.run_id), agent=agent, tool=tool,
                kind=exc.kind, code=exc.code,
            )  # fmt: skip
            return OptionalResult(None, Degraded(tool, exc.kind, exc.code, str(exc)))

    async def current_positions(self) -> VehiclePositions:
        """Infrastructure read for hooks (not an agent call: no gate or budget)."""
        result = await self._invoke(TOOLS["get_vehicle_positions"], {}, self._timeout_s)
        if not isinstance(result, VehiclePositions):
            raise ToolResultInvalid("unexpected positions type", tool="get_vehicle_positions")
        return result

    # ------------------------------------------------------------------ transport

    async def _invoke(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        timeout_s: float,
        stats: dict[str, int] | None = None,
    ) -> BaseModel:
        if self.availability.is_open(spec.server):
            raise ToolUnavailable(
                f"{spec.server} is unavailable (cooling down)", tool=spec.name, code="circuit_open"
            )
        last: Transient | None = None
        for attempt in range(1, self._attempts + 1):
            if stats is not None:
                stats["attempts"] = attempt
            try:
                raw = await self._connector.call(spec.server, spec.name, arguments, timeout_s)
            except Transient as exc:  # rule 7: only transient errors are retried
                last = exc
                log.info("tool_retry", tool=spec.name, attempt=attempt, error=str(exc)[:200])
                if attempt < self._attempts:
                    await self._sleep(0.5 * 2 ** (attempt - 1))
                continue
            self.availability.succeeded(spec.server)
            return self._validate(spec, raw)
        self.availability.failed(spec.server, spec.name, str(last))
        raise ToolUnavailable(
            f"{spec.server} unavailable: {last}", tool=spec.name, code="unavailable"
        )

    def _validate(self, spec: ToolSpec, raw: CallToolResult) -> BaseModel:
        """Post-hook #0 (always on): structured errors become ToolFailed with the server's code;
        results must match the tool's output model (rule 6). Never retried."""
        if raw.is_error:
            text = _text(raw)
            body = parse_tool_error(text)
            if body is not None:
                raise ToolFailed(body.message, tool=spec.name, code=body.code)
            raise ToolFailed(text[:300] or "tool error", tool=spec.name, code="tool_error")
        if raw.structured_content is None:
            raise ToolResultInvalid("no structured result", tool=spec.name, code="no_structure")
        try:
            return spec.output.model_validate(raw.structured_content)
        except ValidationError as exc:
            raise ToolResultInvalid(
                f"result does not match {spec.output.__name__}: {exc.error_count()} errors",
                tool=spec.name, code="schema",
            ) from None  # fmt: skip
