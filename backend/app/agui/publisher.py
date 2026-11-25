"""Worker side: turn LangGraph's stream into AG-UI events, as the run executes (decision 10).

Each worker segment (the start, or a resume) is one AG-UI run on the thread `run_id`:
RUN_STARTED, STATE_SNAPSHOT, then STEP_STARTED / STEP_FINISHED from the `tasks` stream,
STATE_DELTA from the `updates` stream, the explanation as TEXT_MESSAGE_*, and finally
RUN_FINISHED (with an interrupt outcome when the officer must decide) or RUN_ERROR.

Every batch of events is appended to the run's Redis Stream and, in the same Lua call, the
hash holding the latest RunView and the stream id it includes is updated, so an officer who
attaches later gets a consistent snapshot and continues from exactly that id. Publishing is
best effort: a Redis failure is logged and never fails the run.
"""

import asyncio
import json
import uuid
from typing import Any

import structlog
from ag_ui.core import (
    BaseEvent,
    Interrupt,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.agui import patch
from app.agui.view import RunView, StepView, fold, step_name
from app.live.channels import AGUI_MAXLEN, AGUI_TTL_SECONDS, agui_stream_key, agui_view_key
from app.officer.decisions import OfficerDecisionPayload

log = structlog.get_logger()

WORDS_PER_CHUNK = 4
CHUNK_DELAY_S = 0.02  # paces the explanation text; ~0.3 s for a 3-sentence explanation

# KEYS: stream, view hash. ARGV: ttl, maxlen, view json, meta json, n, event json * n.
# Appends the events and records the view + the last id atomically (attach consistency).
_PUBLISH = """
local n = tonumber(ARGV[5])
local last = ''
for i = 1, n do
  last = redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*', 'e', ARGV[5 + i])
end
redis.call('HSET', KEYS[2], 'view', ARGV[3], 'last_id', last, 'meta', ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return last
"""


def event_json(event: BaseEvent) -> str:
    return event.model_dump_json(by_alias=True, exclude_none=True)


def officer_interrupt(view: RunView) -> Interrupt | None:
    """The approval pause as an AG-UI interrupt: resolved with approve / edit / reject."""
    rec = view.recommendation
    if rec is None or rec.recommendation_id is None:
        return None
    return Interrupt(
        id=rec.recommendation_id,
        reason="officer_approval",
        message=rec.explanation,
        response_schema=OfficerDecisionPayload.model_json_schema(),
        expires_at=rec.expires_at,
        metadata={
            "round": view.round,
            "candidate_key": rec.candidate_key,
            "near_tie": rec.near_tie,
            "benefit_passenger_minutes": rec.benefit_passenger_minutes,
        },
    )


class RunEventPublisher:
    def __init__(
        self,
        redis: Redis | None,
        run_id: str,
        *,
        chunk_delay_s: float = CHUNK_DELAY_S,
    ) -> None:
        self._redis = redis
        self.run_id = run_id
        self.segment_id = str(uuid.uuid4())
        self.view = RunView(run_id=run_id)
        self._chunk_delay_s = chunk_delay_s
        self._steps: dict[str, int] = {}  # task id -> index in view.steps
        self._open_message: dict[str, str] | None = None
        self._finish: str | None = None
        self.failures = 0

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """A new segment: continue the view the previous segment left (resume), or a new one."""
        previous = await self._load_view()
        if previous is not None:
            self.view = previous
        self.view.status, self.view.waiting_for = "running", None
        await self._emit(
            [
                RunStartedEvent(thread_id=self.run_id, run_id=self.segment_id),
                StateSnapshotEvent(snapshot=self.view.doc()),
            ]
        )

    async def on_chunk(self, mode: str, chunk: Any) -> None:
        """One item of `graph.astream(stream_mode=["tasks", "updates", "values"])`."""
        try:
            if mode == "tasks":
                await self._on_task(chunk)
            elif mode == "updates":
                await self._on_updates(chunk)
        except Exception as exc:
            self.failures += 1
            log.warning("agui_chunk_failed", run_id=self.run_id, mode=mode, error=repr(exc))

    async def finish(self, waiting_for: str | None, end_reason: str | None) -> None:
        new = self.view.model_copy(deep=True)
        new.waiting_for = waiting_for
        new.status = "paused" if waiting_for else "finished"
        if end_reason is not None:
            new.end_reason = end_reason
        ops = self._replace(new)
        interrupt = officer_interrupt(self.view) if waiting_for == "officer" else None
        if interrupt is not None:
            finished = RunFinishedEvent(
                thread_id=self.run_id,
                run_id=self.segment_id,
                outcome=RunFinishedInterruptOutcome(type="interrupt", interrupts=[interrupt]),
            )
        else:
            result: dict[str, Any] = {"status": self.view.status}
            if end_reason:
                result["end_reason"] = end_reason
            if waiting_for:
                result["waiting_for"] = waiting_for
            finished = RunFinishedEvent(
                thread_id=self.run_id, run_id=self.segment_id, result=result
            )
        self._finish = event_json(finished)
        await self._emit([*self._delta(ops), finished])

    async def error(self, code: str, message: str) -> None:
        new = self.view.model_copy(deep=True)
        new.status, new.waiting_for, new.end_reason = "failed", None, code
        ops = self._replace(new)
        failed = RunErrorEvent(message=message[:500], code=code)
        self._finish = event_json(failed)
        await self._emit([*self._delta(ops), failed])

    # ------------------------------------------------------------------ chunks

    async def _on_task(self, task: dict[str, Any]) -> None:
        task_id, name = str(task["id"]), str(task["name"])
        if "triggers" in task:  # started
            new = self.view.model_copy(deep=True)
            new.steps.append(
                StepView(id=task_id, name=step_name(name, task.get("input")), status="running")
            )
            self._steps[task_id] = len(new.steps) - 1
            label = new.steps[-1].name
            ops = self._replace(new)
            await self._emit([StepStartedEvent(step_name=label), *self._delta(ops)])
            return
        index = self._steps.get(task_id)
        if index is None:
            return
        new = self.view.model_copy(deep=True)
        step = new.steps[index]
        result = task.get("result") or {}
        if task.get("error"):
            step.status = "error"
        elif task.get("interrupts"):
            step.status, step.routing = "paused", "interrupt"
        else:
            step.status = "done"
            step.routing = result.get("route") if isinstance(result, dict) else None
        ops = self._replace(new)
        await self._emit([StepFinishedEvent(step_name=step.name), *self._delta(ops)])

    async def _on_updates(self, updates: dict[str, Any]) -> None:
        for node, update in updates.items():
            if node == "__interrupt__" or not isinstance(update, dict):
                continue
            ops = self._replace(fold(self.view, node, update))
            if ops:
                await self._emit(self._delta(ops))
            rec = update.get("recommendation")
            if node == "control_advisor" and rec is not None and rec.explanation:
                await self._stream_text(rec.explanation)

    async def _stream_text(self, text: str) -> None:
        """The validated explanation (rule 2: checked before anyone sees it), in word chunks."""
        message_id = str(uuid.uuid4())
        words = text.split(" ")
        self._open_message = {"id": message_id, "text": ""}
        await self._emit([TextMessageStartEvent(message_id=message_id, role="assistant")])
        for i in range(0, len(words), WORDS_PER_CHUNK):
            piece = " ".join(words[i : i + WORDS_PER_CHUNK])
            delta = piece if i == 0 else " " + piece
            self._open_message["text"] += delta
            await self._emit([TextMessageContentEvent(message_id=message_id, delta=delta)])
            if self._chunk_delay_s:
                await asyncio.sleep(self._chunk_delay_s)
        self._open_message = None
        await self._emit([TextMessageEndEvent(message_id=message_id)])

    # ------------------------------------------------------------------ plumbing

    def _replace(self, new: RunView) -> list[patch.Op]:
        ops = patch.diff(self.view.doc(), new.doc())
        self.view = new
        return ops

    @staticmethod
    def _delta(ops: list[patch.Op]) -> list[BaseEvent]:
        return [StateDeltaEvent.model_validate({"delta": ops})] if ops else []

    def _meta(self) -> str:
        return json.dumps(
            {
                "segment_id": self.segment_id,
                "segment_state": "finished" if self._finish else "running",
                "finish": self._finish,
                "open_message": self._open_message,
            }
        )

    async def _emit(self, events: list[BaseEvent]) -> None:
        if self._redis is None or not events:
            return
        args = [str(AGUI_TTL_SECONDS), str(AGUI_MAXLEN), json.dumps(self.view.doc()), self._meta()]
        payloads = [event_json(e) for e in events]
        try:
            await self._redis.eval(  # type: ignore[misc]  # redis-py stubs: sync|async union
                _PUBLISH,
                2,
                agui_stream_key(self.run_id),
                agui_view_key(self.run_id),
                *args,
                str(len(payloads)),
                *payloads,
            )
        except RedisError as exc:
            self.failures += 1
            log.warning("agui_publish_failed", run_id=self.run_id, error=repr(exc))

    async def _load_view(self) -> RunView | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.hget(agui_view_key(self.run_id), "view")  # type: ignore[misc]
        except RedisError as exc:
            log.warning("agui_view_unavailable", run_id=self.run_id, error=repr(exc))
            return None
        if raw is None:
            return None
        try:
            return RunView.model_validate_json(raw)
        except ValueError:
            return None
