"""MCP client for the harness: the official `mcp` SDK client over streamable HTTP.

(`langchain-mcp-adapters` 0.3.1 imports symbols removed in MCP SDK v2, which the B7 servers use,
so the harness talks to the servers with the SDK client directly; see docs/decisions.md.)

A fresh session per call: a restarted server is picked up on the next call, and nothing holds
a half-dead connection. Transport problems surface as one exception type, `Transient`.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Tool

log = structlog.get_logger()

# Exception class names (anywhere in the MRO) that mean "the network or server had a moment".
_TRANSIENT_NAMES = {
    "TransportError", "NetworkError", "TimeoutException", "ConnectError", "ConnectTimeout",
    "ReadTimeout", "WriteTimeout", "PoolTimeout", "ReadError", "WriteError", "RemoteProtocolError",
    "ClosedResourceError", "BrokenResourceError", "EndOfStream",
}  # fmt: skip


class Transient(Exception):
    """A connection error or timeout talking to an MCP server (safe to retry)."""


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def is_transient(exc: BaseException) -> bool:
    for leaf in _leaves(exc):
        if isinstance(leaf, TimeoutError | ConnectionError | OSError):
            return True
        if isinstance(leaf, MCPError) and "timed out" in str(leaf).lower():
            return True
        if any(cls.__name__ in _TRANSIENT_NAMES for cls in type(leaf).__mro__):
            return True
    return False


class Connector(Protocol):
    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> CallToolResult: ...

    async def list_tools(self, server: str, timeout_s: float) -> list[Tool]: ...


class MCPConnector:
    def __init__(self, urls: Mapping[str, str]) -> None:
        self._urls = dict(urls)

    def _client(self, server: str, timeout_s: float) -> Client:
        return Client(self._urls[server], read_timeout_seconds=timeout_s)

    async def call(
        self, server: str, tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> CallToolResult:
        try:
            async with self._client(server, timeout_s) as client:
                return await client.call_tool(tool, arguments, read_timeout_seconds=timeout_s)
        except BaseException as exc:
            if is_transient(exc):
                raise Transient(f"{server}: {_leaves(exc)[0]!r}") from exc
            raise

    async def list_tools(self, server: str, timeout_s: float) -> list[Tool]:
        try:
            async with self._client(server, timeout_s) as client:
                return list((await client.list_tools()).tools)
        except BaseException as exc:
            if is_transient(exc):
                raise Transient(f"{server}: {_leaves(exc)[0]!r}") from exc
            raise


@dataclass
class _State:
    open_until: float = 0.0
    unavailable: bool = False


class Availability:
    """Per-server circuit breaker: after a server fails a call (retries exhausted) it is marked
    unavailable for a cooldown, so later calls fail fast instead of piling up timeouts. After
    the cooldown one call is let through to probe it."""

    def __init__(self, cooldown_s: float, clock: Any = time.monotonic) -> None:
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._state: dict[str, _State] = {}

    def is_open(self, server: str) -> bool:
        state = self._state.get(server)
        return bool(state and state.unavailable and self._clock() < state.open_until)

    def failed(self, server: str, tool: str, reason: str) -> None:
        state = self._state.setdefault(server, _State())
        if not state.unavailable:
            log.warning(
                "tool_unavailable", server=server, tool=tool, reason=reason,
                cooldown_s=self._cooldown_s,
            )  # fmt: skip
        state.unavailable = True
        state.open_until = self._clock() + self._cooldown_s

    def succeeded(self, server: str) -> None:
        state = self._state.get(server)
        if state and state.unavailable:
            log.info("tool_available_again", server=server)
        self._state[server] = _State()

    def unavailable_servers(self) -> list[str]:
        return sorted(s for s in self._state if self.is_open(s))
