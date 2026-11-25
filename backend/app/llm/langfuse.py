"""Best-effort Langfuse traces for LLM calls (decision 5: nice to have, never required).

Posts to the public ingestion API in the background with a short timeout; any failure is
logged and dropped. `llm_usage` stays the source of truth. Off unless real keys are set.
"""

import asyncio
import base64
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from app.config import Settings

log = structlog.get_logger()


class LangfuseTracer:
    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        timeout_s: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = host.rstrip("/") + "/api/public/ingestion"
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._timeout = timeout_s
        self._transport = transport
        self._pending: set[asyncio.Task[None]] = set()

    @classmethod
    def from_settings(cls, settings: Settings) -> "LangfuseTracer | None":
        key = settings.langfuse_secret_key
        secret = key.get_secret_value() if key else ""
        public = settings.langfuse_public_key
        if not public or not secret or "change-me" in public or "change-me" in secret:
            return None
        return cls(settings.langfuse_host, public, secret)

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
        trace_id = run_id or str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        body: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "traceId": trace_id,
            "name": agent,
            "model": model,
            "input": messages,
            "output": output,
            "startTime": started.isoformat(),
            "endTime": now,
            "usage": {"input": input_tokens, "output": output_tokens, "unit": "TOKENS"},
            "metadata": {"fallback_level": level},
            "level": "ERROR" if error else "DEFAULT",
            "statusMessage": error,
        }
        trace = {"id": trace_id, "name": "workflow_run" if run_id else agent}
        batch = [
            {"id": str(uuid.uuid4()), "timestamp": now, "type": "trace-create", "body": trace},
            {"id": str(uuid.uuid4()), "timestamp": now, "type": "generation-create", "body": body},
        ]
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
            log.warning("langfuse_unreachable", error=repr(exc))

    async def flush(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
