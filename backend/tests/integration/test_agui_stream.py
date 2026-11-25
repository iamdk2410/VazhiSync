"""AG-UI run stream end to end (decision 10): the worker publishes while the graph runs, the
officer attaches (also midway), reconnects with Last-Event-ID, resolves the approval interrupt
through AG-UI, and old runs are rebuilt from the database."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from arq import ArqRedis
from arq.worker import Worker
from httpx import AsyncClient
from sqlalchemy import select

from app.agui import patch, stream
from app.agui.publisher import RunEventPublisher
from app.config import get_settings
from app.db.models import DriverInstruction, Recommendation
from app.db.session import get_sessionmaker
from app.graph.workflow import start_workflow
from app.live.channels import agui_stream_key, agui_view_key
from app.runner.world import WorldRunner
from tests.conftest import Account
from tests.integration.conftest import bearer, login
from tests.integration.test_approval import (  # noqa: F401
    api,
    paused_run,
    run_id_of,
    worker,
)
from tests.integration.test_workflow import (  # noqa: F401
    bunched,
    clock,
    harness,
    only_event,
    redis,
)

pytestmark = pytest.mark.services
Frame = tuple[str | None, dict[str, Any]]


def parse_sse(body: str) -> list[Frame]:
    frames: list[Frame] = []
    for block in body.split("\n\n"):
        event_id, data = None, None
        for line in block.splitlines():
            if line.startswith("id: "):
                event_id = line[4:]
            elif line.startswith("data: "):
                data = line[6:]
        if data is not None:
            frames.append((event_id, json.loads(data)))
    return frames


async def collect(frames: AsyncIterator[str]) -> list[Frame]:
    return parse_sse("".join([f async for f in frames]))


async def never() -> bool:
    return False


async def stored_events(queue: ArqRedis, run_id: uuid.UUID) -> list[Frame]:
    entries = await queue.xrange(agui_stream_key(str(run_id)))
    return [(eid.decode(), json.loads(fields[b"e"])) for eid, fields in entries]


def fold_deltas(snapshot: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    doc = snapshot
    for e in events:
        if e["type"] == "STATE_DELTA":
            doc = patch.apply(doc, e["delta"])
    return doc


async def stored_view(queue: ArqRedis, run_id: uuid.UUID) -> dict[str, Any]:
    raw = await queue.hget(agui_view_key(str(run_id)), "view")  # type: ignore[misc]
    assert raw is not None
    view: dict[str, Any] = json.loads(raw)
    return view


async def open_rec_id(run_id: uuid.UUID) -> str:
    async with get_sessionmaker()() as session:
        rec = await session.scalar(
            select(Recommendation)
            .where(Recommendation.run_id == run_id)
            .order_by(Recommendation.created_at.desc())
        )
    assert rec is not None
    return str(rec.id)


# ------------------------------------------------------------------ the stream


async def test_a_run_is_published_as_ag_ui_and_attach_rehydrates_it(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    run_id = run_id_of(result)
    events = [e for _, e in await stored_events(job_queue, run_id)]
    types = [e["type"] for e in events]
    assert types[:2] == ["RUN_STARTED", "STATE_SNAPSHOT"] and types[-1] == "RUN_FINISHED"
    assert events[0]["threadId"] == str(run_id)

    steps = [e["stepName"] for e in events if e["type"] == "STEP_STARTED"]
    candidates = result.state["candidates"] if result.state else []
    assert steps[:3] == ["headway_monitor", "action_generator", "impact_simulator"]
    assert sorted(s for s in steps if s.startswith("simulate:")) == sorted(
        f"simulate:{c.key}" for c in candidates
    )
    assert steps[-2:] == ["control_advisor", "await_officer"]
    assert types.count("STEP_FINISHED") == len(steps)
    text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == result.state["recommendation"].explanation if result.state else False

    # The approval pause is the AG-UI interrupt, keyed by the recommendation.
    (interrupt,) = events[-1]["outcome"]["interrupts"]
    assert interrupt["id"] == await open_rec_id(run_id)
    assert interrupt["reason"] == "officer_approval" and interrupt["expiresAt"]

    # Snapshot + every delta == the stored view == what an officer attaching now gets.
    folded = fold_deltas(events[1]["snapshot"], events[2:])
    assert folded == await stored_view(job_queue, run_id)
    officer = bearer(await login(api, accounts["officer"]))
    resp = await api.get(f"/officer/runs/{run_id}/events", headers=officer)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(resp.text)
    assert [e["type"] for _, e in frames] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
    assert frames[0][1]["runId"] == events[0]["runId"]  # the same segment
    assert frames[1][1]["snapshot"] == folded
    assert frames[2][1]["outcome"]["interrupts"][0]["id"] == interrupt["id"]
    assert all(eid for eid, _ in frames)  # every frame carries a stream id

    listing = await api.get("/officer/runs?active=true", headers=officer)
    assert [r["run_id"] for r in listing.json()["runs"]] == [str(run_id)]
    assert listing.json()["runs"][0]["waiting_for"] == "officer"

    driver = bearer(await login(api, accounts["driver"]))
    assert (await api.get(f"/officer/runs/{run_id}/events", headers=driver)).status_code == 403
    missing = await api.get(f"/officer/runs/{uuid.uuid4()}/events", headers=officer)
    assert missing.status_code == 404


async def test_attach_midway_then_reconnect_with_last_event_id(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RunEventPublisher.on_chunk

    async def slower(self: RunEventPublisher, mode: str, chunk: Any) -> None:
        await original(self, mode, chunk)
        await asyncio.sleep(0.05)  # give the officer time to attach mid-run

    monkeypatch.setattr(RunEventPublisher, "on_chunk", slower)
    settings = get_settings()
    event = await only_event()
    run = asyncio.create_task(
        start_workflow(
            event.id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer
        )
    )
    pos = None
    for _ in range(400):
        pos = await stream.position(job_queue, await _run_id_of_event(event.id))
        if pos is not None and any(s.status == "done" for s in pos.view.steps):
            break
        await asyncio.sleep(0.02)
    assert pos is not None and not pos.finished  # attaching while it runs
    run_id = uuid.UUID(pos.view.run_id)

    attached = await collect(stream.attach(job_queue, get_sessionmaker(), run_id, never))
    await run
    assert [e["type"] for _, e in attached][:2] == ["RUN_STARTED", "STATE_SNAPSHOT"]
    assert attached[-1][1]["type"] == "RUN_FINISHED"
    folded = fold_deltas(attached[1][1]["snapshot"], [e for _, e in attached[2:]])
    assert folded == await stored_view(job_queue, run_id)  # rehydrated, then kept in sync
    if any(e["type"] == "TEXT_MESSAGE_CONTENT" for _, e in attached):
        assert any(e["type"] == "TEXT_MESSAGE_START" for _, e in attached)

    # A dropped connection resumes after the last id it saw: no gaps, no duplicates.
    live = [f for f in attached[2:] if f[0] != attached[1][0]]
    assert len(live) > 4
    cut = live[len(live) // 2][0]
    rest = await collect(stream.attach(job_queue, get_sessionmaker(), run_id, never, cut))
    expected = [f for f in await stored_events(job_queue, run_id) if _after(f[0], cut)]
    assert rest == expected and rest[-1][1]["type"] == "RUN_FINISHED"


def _after(entry_id: str | None, cut: str | None) -> bool:
    assert entry_id is not None and cut is not None
    a, b = (tuple(int(x) for x in i.split("-")) for i in (entry_id, cut))
    return a > b


async def _run_id_of_event(event_id: uuid.UUID) -> str:
    from app.db.models import WorkflowRun

    async with get_sessionmaker()() as session:
        run = await session.scalar(
            select(WorkflowRun).where(WorkflowRun.bunching_event_id == event_id)
        )
    return str(run.id) if run else str(uuid.uuid4())


# ------------------------------------------------------------------ resolving the interrupt


def run_input(run_id: uuid.UUID, interrupt_id: str, **entry: Any) -> dict[str, Any]:
    return {
        "threadId": str(run_id),
        "runId": f"console-{uuid.uuid4()}",
        "messages": [],
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
        "resume": [{"interruptId": interrupt_id, "status": "resolved", **entry}],
    }


async def test_the_officer_resolves_the_interrupt_through_ag_ui(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,  # noqa: F811
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    run_id = run_id_of(result)
    first_segment = (await stored_events(job_queue, run_id))[0][1]["runId"]
    rec_id = await open_rec_id(run_id)
    officer = bearer(await login(api, accounts["officer"]))
    url = f"/officer/runs/{run_id}/agui"

    wrong = await api.post(
        url,
        json=run_input(run_id, str(uuid.uuid4()), payload={"decision": "approve"}),
        headers=officer,
    )
    assert wrong.status_code == 409
    no_reason = await api.post(
        url, json=run_input(run_id, rec_id, payload={"decision": "reject"}), headers=officer
    )
    assert no_reason.status_code == 422
    driver = bearer(await login(api, accounts["driver"]))
    as_driver = await api.post(
        url, json=run_input(run_id, rec_id, payload={"decision": "approve"}), headers=driver
    )
    assert as_driver.status_code == 403

    resp = await api.post(
        url, json=run_input(run_id, rec_id, payload={"decision": "approve"}), headers=officer
    )
    assert resp.status_code == 200, resp.text
    frames = [e for _, e in parse_sse(resp.text)]
    types = [e["type"] for e in frames]
    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
    assert frames[0]["threadId"] == str(run_id) and frames[0]["runId"] != first_segment
    steps = [e["stepName"] for e in frames if e["type"] == "STEP_STARTED"]
    assert steps[:2] == ["await_officer", "dispatch"]
    finished = frames[-1]
    sent = await _instructions()
    if finished.get("result", {}).get("waiting_for") == "driver":
        assert len(sent) == 1  # dispatched exactly once
    else:  # the pair recovered before sending: dispatch re-validated and ended the run
        assert sent == []

    again = await api.post(
        url, json=run_input(run_id, rec_id, payload={"decision": "approve"}), headers=officer
    )
    assert again.status_code == 200  # already decided: just shows where the run is now
    assert parse_sse(again.text)[0][1]["type"] == "RUN_STARTED"
    assert len(await _instructions()) == len(sent)


async def _instructions() -> list[DriverInstruction]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(DriverInstruction)))


async def test_a_run_without_a_stream_is_rebuilt_from_the_database(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    run_id = run_id_of(result)
    live_view = await stored_view(job_queue, run_id)
    await job_queue.delete(agui_stream_key(str(run_id)), agui_view_key(str(run_id)))

    officer = bearer(await login(api, accounts["officer"]))
    resp = await api.get(f"/officer/runs/{run_id}/events", headers=officer)
    frames = [e for _, e in parse_sse(resp.text)]
    assert [e["type"] for e in frames] == ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]
    assert frames[0]["runId"] == f"db-{run_id}"
    snap = frames[1]["snapshot"]
    assert snap["waiting_for"] == "officer" and snap["status"] == "paused"
    assert sorted(c["key"] for c in snap["candidates"]) == sorted(
        c["key"] for c in live_view["candidates"]
    )  # the keys rebuilt from the database match the workflow's
    assert snap["recommendation"]["candidate_key"] == live_view["recommendation"]["candidate_key"]
    assert [s["name"] for s in snap["steps"]][:3] == [
        "headway_monitor", "action_generator", "impact_simulator",
    ]  # fmt: skip
    assert frames[2]["outcome"]["interrupts"][0]["id"] == await open_rec_id(run_id)
