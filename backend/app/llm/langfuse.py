"""Best-effort Langfuse traces (decision 5: nice to have, never required).

One trace per workflow run (trace id = run id). Agent steps are spans, and the tool calls and
LLM generations made inside a step are its children (B18; see app/observability/tracing.py),
so Langfuse shows the run as a timeline: agents, tools and model calls together.

Everything is posted to the public ingestion API in the background with a short timeout; a
failure is logged and dropped, and after one the tracer pauses for a while instead of timing
out on every call. `llm_usage`, `agent_steps` and `tool_calls` stay the source of truth for
reports. Off unless real keys are set.
"""

import asyncio
import base64
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.config import Settings, get_settings
from app.observability.tracing import current_step

log = structlog.get_logger()

PAUSE_AFTER_FAILURE_S = 60.0


class LangfuseTracer:
    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        timeout_s: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._url = host.rstrip("/") + "/api/public/ingestion"
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._timeout = timeout_s
        self._transport = transport
        self._clock = clock
        self._paused_until = 0.0
        self._pending: set[asyncio.Task[None]] = set()
        self._traces: set[str] = set()

    @classmethod
    def from_settings(cls, settings: Settings) -> "LangfuseTracer | None":
        key = settings.langfuse_secret_key
        secret = key.get_secret_value() if key else ""
        public = settings.langfuse_public_key
        if not public or not secret or "change-me" in public or "change-me" in secret:
            return None
        return cls(settings.langfuse_host, public, secret)

    # ------------------------------------------------------------------ observations

    def generation(
        self,
        *,
        run_id: str | None,
        agent: str,
        model: str,
        level: int,
        messages: list[dict[str, str]],
        output: str | None,
        input_tokens: int,
        output_tokens: int,
        started: datetime,
        error: str | None,
    ) -> None:
        body: dict[str, Any] = {
            "name": agent,
            "model": model,
            "input": messages,
            "output": output,
            "startTime": started.isoformat(),
            "endTime": datetime.now(UTC).isoformat(),
            "usage": {"input": input_tokens, "output": output_tokens, "unit": "TOKENS"},
            "metadata": {"fallback_level": level},
            "level": "ERROR" if error else "DEFAULT",
            "statusMessage": error,
        }
        self._observe("generation-create", run_id, agent, body)

    def span(
        self,
        *,
        run_id: str | None,
        name: str,
        started: datetime,
        ended: datetime | None = None,
        input: Any = None,
        output: Any = None,
        error: str | None = None,
        observation_id: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """An agent step (with its own deterministic id) or a tool call inside one."""
        body: dict[str, Any] = {
            "name": name,
            "startTime": started.isoformat(),
            "endTime": (ended or datetime.now(UTC)).isoformat(),
            "input": input,
            "output": output,
            "level": "ERROR" if error else "DEFAULT",
            "statusMessage": error,
            "metadata": metadata or {},
        }
        if observation_id:
            body["id"] = observation_id
        if parent_id:
            body["parentObservationId"] = parent_id
        self._observe("span-create", run_id, name, body, link_to_step=parent_id is None)

    def _observe(
        self,
        kind: str,
        run_id: str | None,
        name: str,
        body: dict[str, Any],
        link_to_step: bool = True,
    ) -> None:
        if self._clock() < self._paused_until:
            return
        trace_id = run_id or str(uuid.uuid4())
        body = {"id": str(uuid.uuid4()), **body, "traceId": trace_id}
        step = current_step()
        if link_to_step and step is not None and step.run_id == trace_id:
            body.setdefault("parentObservationId", step.observation_id)
        now = datetime.now(UTC).isoformat()
        batch: list[dict[str, Any]] = []
        if trace_id not in self._traces:  # one trace-create per run is enough
            self._traces.add(trace_id)
            trace = {"id": trace_id, "name": "workflow_run" if run_id else name}
            batch.append(
                {"id": str(uuid.uuid4()), "timestamp": now, "type": "trace-create", "body": trace}
            )
        batch.append({"id": str(uuid.uuid4()), "timestamp": now, "type": kind, "body": body})
        task = asyncio.create_task(self._send(batch))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, batch: list[dict[str, Any]]) -> None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as c:
                resp = await c.post(self._url, json={"batch": batch}, headers=self._headers)
            if resp.status_code >= 300:
                log.warning("langfuse_rejected", status=resp.status_code)
        except httpx.HTTPError as exc:
            if self._clock() >= self._paused_until:
                log.warning("langfuse_unreachable", error=repr(exc), paused_s=PAUSE_AFTER_FAILURE_S)
            self._paused_until = self._clock() + PAUSE_AFTER_FAILURE_S

    async def flush(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


_shared: dict[str, LangfuseTracer | None] = {}


def get_tracer() -> LangfuseTracer | None:
    """The process-wide tracer (None when Langfuse isn't configured)."""
    if "tracer" not in _shared:
        _shared["tracer"] = LangfuseTracer.from_settings(get_settings())
    return _shared["tracer"]


def set_tracer(tracer: LangfuseTracer | None) -> None:
    """For tests: use this tracer (or none) everywhere."""
    _shared["tracer"] = tracer
