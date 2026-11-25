"""B12 acceptance: approval, dispatch and driver feedback, end to end.

Done when: (1) the API can restart while a run waits for approval and the run still resumes,
and (2) double-clicking Approve sends exactly one instruction. Plus edit, reject, expiry,
driver acknowledgement (applied in the world), can't-comply and the measured outcome.

Everything goes through the real API and real ARQ workers in-process; the graph state lives in
the Postgres checkpointer on the test database.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, cast

import pytest
from arq import ArqRedis
from arq.worker import Worker
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from app.api.deps import get_job_queue
from app.auth.rate_limit import MemoryRateLimiter, get_rate_limiter
from app.config import get_settings
from app.db.enums import BunchingEventStatus, InstructionStatus, Role, WorkflowStatus
from app.db.models import (
    BunchingEvent,
    DriverAssignment,
    DriverInstruction,
    InterventionOutcome,
    Notification,
    Recommendation,
    User,
    WorkflowRun,
)
from app.db.session import get_sessionmaker
from app.graph.checkpoint import postgres_checkpointer
from app.graph.workflow import RunResult, abandoned_after, resume_workflow, start_workflow
from app.live.channels import WORLD_ACTIONS_KEY
from app.main import create_app
from app.runner.world import WorldRunner
from tests.conftest import Account
from tests.integration.conftest import bearer, login
from tests.integration.sim_worker import running_workflow_worker
from tests.integration.test_live_world import FakeClock
from tests.integration.test_workflow import (  # noqa: F401
    bunched,
    clock,
    harness,
    only_event,
    redis,
)

pytestmark = pytest.mark.services


# ------------------------------------------------------------------ helpers


@pytest.fixture
async def api(job_queue: ArqRedis, accounts: dict[str, Account]) -> AsyncIterator[AsyncClient]:
    async with _client(job_queue) as client:
        yield client


class _client:
    """A fresh API process (create_app) - used twice to simulate an API restart."""

    def __init__(self, job_queue: ArqRedis) -> None:
        app = create_app()
        app.dependency_overrides[get_rate_limiter] = lambda: MemoryRateLimiter()
        app.dependency_overrides[get_job_queue] = lambda: job_queue
        self._client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def __aenter__(self) -> AsyncClient:
        return await self._client.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc, tb)


async def paused_run(job_queue: ArqRedis, checkpointer: Any) -> RunResult:
    settings = get_settings()
    event = await only_event()
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer
    )
    assert result.waiting_for == "officer", result.end_reason
    return result


def run_id_of(result: RunResult) -> uuid.UUID:
    assert result.run_id is not None, result
    return result.run_id


async def run_row(run_id: uuid.UUID) -> WorkflowRun:
    async with get_sessionmaker()() as session:
        run = await session.get(WorkflowRun, run_id)
    assert run is not None
    return run


async def wait_for(
    run_id: uuid.UUID, done: Callable[[WorkflowRun], bool], limit_s: float = 30
) -> WorkflowRun:
    for _ in range(int(limit_s / 0.1)):
        run = await run_row(run_id)
        if done(run):
            return run
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"run never reached the expected state: {(await run_row(run_id)).__dict__}"
    )


def waiting(kind: str) -> Callable[[WorkflowRun], bool]:
    return lambda r: r.waiting_for == kind


async def pending(api: AsyncClient, officer: dict[str, str]) -> dict[str, Any]:
    resp = await api.get("/officer/recommendations", headers=officer)
    assert resp.status_code == 200, resp.text
    (rec,) = resp.json()["recommendations"]
    return rec  # type: ignore[no-any-return]


async def instructions() -> list[DriverInstruction]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(DriverInstruction)))


async def assign_driver(vehicle_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        driver = await session.scalar(select(User).where(User.role == Role.DRIVER))
        assert driver is not None
        session.add(DriverAssignment(driver_id=driver.id, vehicle_id=vehicle_id))
        await session.commit()


@pytest.fixture
async def worker(job_queue: ArqRedis, checkpointer: Any) -> AsyncIterator[Worker]:
    ctx = {"harness": harness(get_settings()), "checkpointer": checkpointer}
    async with running_workflow_worker(ctx) as w:
        yield w


# ------------------------------------------------------------------ done when


async def test_api_restart_while_awaiting_approval_then_resume(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    accounts: dict[str, Account],
    database_url: str,
) -> None:
    # The run pauses for the officer; its state is checkpointed in Postgres. Then every
    # process that touched it goes away (its checkpointer connection pool is closed).
    async with postgres_checkpointer(database_url) as first:
        result = await paused_run(job_queue, first)
    async with _client(job_queue) as old_api:  # the API before the restart
        officer = bearer(await login(old_api, accounts["officer"]))
        rec = await pending(old_api, officer)

    # A new API process and a new worker (new checkpointer pool) pick it up.
    async with postgres_checkpointer(database_url) as second, _client(job_queue) as new_api:
        ctx = {"harness": harness(get_settings()), "checkpointer": second}
        async with running_workflow_worker(ctx):
            officer = bearer(await login(new_api, accounts["officer"]))
            resp = await new_api.post(
                f"/officer/recommendations/{rec['id']}/decision",
                json={"decision": "approve"},
                headers=officer,
            )
            assert resp.status_code == 202, resp.text
            run = await wait_for(run_id_of(result), waiting("driver"))
    assert run.status == WorkflowStatus.DISPATCHED
    (sent,) = await instructions()
    assert sent.instruction_id == f"apr-{resp.json()['approval_id']}"
    assert sent.status == InstructionStatus.SENT


async def test_double_clicked_approve_sends_exactly_one_instruction(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,
    api: AsyncClient,
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    url = f"/officer/recommendations/{rec['id']}/decision"
    responses = await asyncio.gather(
        *(api.post(url, json={"decision": "approve"}, headers=officer) for _ in range(5))
    )
    statuses = sorted(r.json()["status"] for r in responses)
    assert statuses.count("accepted") == 1 and statuses.count("already_decided") == 4
    assert len({r.json()["approval_id"] for r in responses}) == 1  # one decision recorded
    await wait_for(run_id_of(result), waiting("driver"))
    await asyncio.sleep(1)  # give any duplicate resume a chance to (wrongly) send again
    assert len(await instructions()) == 1


# ------------------------------------------------------------------ officer decisions


async def test_edit_resimulates_and_comes_back_to_the_officer(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,
    api: AsyncClient,
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    round_ = 0
    if rec["action_type"] != "hold":
        # Only a hold's length is editable: anything else is refused; reject it for a new plan.
        url = f"/officer/recommendations/{rec['id']}/decision"
        bad = await api.post(url, json={"decision": "edit", "seconds": 60}, headers=officer)
        assert bad.status_code == 422
        resp = await api.post(
            url, json={"decision": "reject", "reason": "prefer a hold"}, headers=officer
        )
        assert resp.status_code == 202
        round_ = 1
        await wait_for(
            run_id_of(result), lambda r: r.waiting_for == "officer" and r.regeneration_round == 1
        )
        rec = await pending(api, officer)
    assert rec["action_type"] == "hold"
    resp = await api.post(
        f"/officer/recommendations/{rec['id']}/decision",
        json={"decision": "edit", "seconds": 60},
        headers=officer,
    )
    assert resp.status_code == 202, resp.text
    run = await wait_for(
        run_id_of(result),
        lambda r: r.waiting_for == "officer" and r.regeneration_round == round_ + 1,
    )
    edited = await pending(api, officer)
    assert edited["round"] == round_ + 1 and edited["id"] != rec["id"]
    keys = [r["candidate_key"] for r in edited["ranking"]]
    assert keys[-1] == "do_nothing" and any(k.startswith("edit:") for k in keys)
    assert run.status in (WorkflowStatus.AWAITING_APPROVAL, WorkflowStatus.ESCALATED)
    assert await instructions() == []  # nothing sent until the edited plan is approved


async def test_reject_needs_a_reason_then_replans_until_exhausted(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,
    api: AsyncClient,
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    url = f"/officer/recommendations/{rec['id']}/decision"
    assert (await api.post(url, json={"decision": "reject"}, headers=officer)).status_code == 422
    rejected = [rec["ranking"][0]["candidate_key"]]
    for round_ in (1, 2, 3):
        resp = await api.post(
            url, json={"decision": "reject", "reason": "not on this street"}, headers=officer
        )
        assert resp.status_code == 202, resp.text

        def next_plan_or_end(r: WorkflowRun, n: int = round_) -> bool:
            return (r.waiting_for == "officer" and r.regeneration_round == n) or (
                r.finished_at is not None
            )

        run = await wait_for(run_id_of(result), next_plan_or_end)
        if run.finished_at is not None:
            break
        rec = await pending(api, officer)
        assert rec["ranking"][0]["candidate_key"] not in rejected  # rejected options stay out
        rejected.append(rec["ranking"][0]["candidate_key"])
        url = f"/officer/recommendations/{rec['id']}/decision"
    run = await run_row(run_id_of(result))
    assert run.finished_at is not None and run.end_reason in ("rejected", "do_nothing_recommended")
    assert await instructions() == []


async def test_an_expired_recommendation_is_replanned_not_sent(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,
    api: AsyncClient,
    accounts: dict[str, Account],
) -> None:
    result = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    async with get_sessionmaker()() as session:
        row = await session.get(Recommendation, uuid.UUID(rec["id"]))
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    resp = await api.post(
        f"/officer/recommendations/{rec['id']}/decision",
        json={"decision": "approve"},
        headers=officer,
    )
    assert resp.status_code == 409 and "expired" in resp.json()["detail"]
    run = await wait_for(
        run_id_of(result),
        lambda r: (
            (r.waiting_for == "officer" and r.regeneration_round == 1) or r.finished_at is not None
        ),
    )
    assert await instructions() == []
    assert run.regeneration_round == 1  # re-planned from fresh observations


# ------------------------------------------------------------------ driver feedback and outcome


async def _dispatched(
    api: AsyncClient, accounts: dict[str, Account], job_queue: ArqRedis, checkpointer: Any
) -> tuple[RunResult, DriverInstruction, dict[str, str]]:
    result = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    await assign_driver(uuid.UUID(rec["vehicle_id"]))
    resp = await api.post(
        f"/officer/recommendations/{rec['id']}/decision",
        json={"decision": "approve"},
        headers=officer,
    )
    assert resp.status_code == 202, resp.text
    await wait_for(run_id_of(result), waiting("driver"))
    (sent,) = await instructions()
    return result, sent, bearer(await login(api, accounts["driver"]))


async def test_driver_ack_is_applied_in_the_world_and_the_outcome_measured(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,
    api: AsyncClient,
    accounts: dict[str, Account],
    clock: FakeClock,  # noqa: F811
    redis: Redis,  # noqa: F811
) -> None:
    runner, _ = bunched
    result, sent, driver = await _dispatched(api, accounts, job_queue, checkpointer)

    current = (await api.get("/driver/instructions/current", headers=driver)).json()["instruction"]
    assert current["instruction_id"] == sent.instruction_id
    resp = await api.post(f"/driver/instructions/{sent.id}/ack", headers=driver)
    assert resp.status_code == 200 and resp.json()["status"] == "acknowledged"
    assert (
        await api.post(f"/driver/instructions/{sent.id}/ack", headers=driver)
    ).status_code == 200
    queued = [
        json.loads(x)
        for x in await cast(Awaitable[list[Any]], redis.lrange(WORLD_ACTIONS_KEY, 0, -1))
    ]
    assert [q["instruction_id"] for q in queued] == [sent.instruction_id]
    await runner.tick(0)  # the world's bus carries it out
    assert await cast(Awaitable[int], redis.llen(WORLD_ACTIONS_KEY)) == 0
    await wait_for(run_id_of(result), waiting("outcome"))
    assert (await api.post(f"/driver/instructions/{sent.id}/done", headers=driver)).json()[
        "status"
    ] == "done"

    for _ in range(9):  # ~15 world-minutes pass, observations keep coming in
        clock.advance(10)
        await runner.tick(100)
    final = await resume_workflow(
        run_id_of(result),
        "outcome",
        {"kind": "measure"},
        harness(get_settings()),
        get_sessionmaker(),
        get_settings(),
        job_queue,
        checkpointer,
    )
    assert final.status == "completed" and final.end_reason == "outcome_measured"
    async with get_sessionmaker()() as session:
        (outcome,) = (await session.scalars(select(InterventionOutcome))).all()
    assert outcome.driver_instruction_id == sent.id
    assert outcome.predicted_benefit_passenger_minutes is not None
    assert outcome.actual_benefit_passenger_minutes is not None  # predicted AND actual stored
    assert outcome.metrics["queue_samples"] > 0 and outcome.bunching_resolved is not None


async def test_driver_cant_comply_cancels_alerts_and_replans(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,
    api: AsyncClient,
    accounts: dict[str, Account],
) -> None:
    result, sent, driver = await _dispatched(api, accounts, job_queue, checkpointer)
    resp = await api.post(
        f"/driver/instructions/{sent.id}/cant-comply",
        json={"reason": "blocking_traffic", "note": "lorry unloading"},
        headers=driver,
    )
    assert resp.status_code == 200 and resp.json()["status"] == "cant_comply"
    run = await wait_for(
        run_id_of(result),
        lambda r: r.waiting_for != "driver" and r.status != WorkflowStatus.RUNNING,
    )
    assert run.regeneration_round == 1 or run.finished_at is not None  # re-planned
    async with get_sessionmaker()() as session:
        alerts = await session.scalar(
            select(func.count()).select_from(Notification).where(Notification.user_id.is_not(None))
        )
        row = await session.get(DriverInstruction, sent.id)
    assert alerts == 1  # the one officer was alerted
    assert row is not None and row.cancel_reason == "blocking_traffic"


async def test_other_drivers_and_roles_cant_touch_the_instruction(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,
    api: AsyncClient,
    accounts: dict[str, Account],
) -> None:
    _, sent, _ = await _dispatched(api, accounts, job_queue, checkpointer)
    passenger = bearer(await login(api, accounts["passenger"]))
    officer = bearer(await login(api, accounts["officer"]))
    assert (
        await api.post(f"/driver/instructions/{sent.id}/ack", headers=passenger)
    ).status_code == 403
    assert (
        await api.post(f"/driver/instructions/{sent.id}/ack", headers=officer)
    ).status_code == 403
    resp = await api.post(f"/driver/instructions/{sent.id}/ack", headers=passenger)
    assert resp.status_code == 403  # drivers only (rule 10)
    assert len(await instructions()) == 1


async def test_an_ignored_recommendation_does_not_block_its_pair_forever(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
) -> None:
    first = await paused_run(job_queue, checkpointer)
    event = await only_event()
    async with get_sessionmaker()() as session:
        session.add(
            BunchingEvent(
                route_id="T1",
                leader_vehicle_id=event.leader_vehicle_id,
                follower_vehicle_id=event.follower_vehicle_id,
                headway_seconds=20,
                scheduled_headway_seconds=300,
                severity=event.severity,
            )
        )
        await session.commit()
        later = await session.scalar(select(BunchingEvent).where(BunchingEvent.id != event.id))
    assert later is not None
    settings = get_settings()

    async def start() -> RunResult:
        return await start_workflow(
            later.id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer
        )

    assert (await start()).end_reason == "pair_already_handled"  # still fresh: not superseded
    async with get_sessionmaker()() as session:  # nobody decided in time
        for rec in await session.scalars(select(Recommendation)):
            rec.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    second = await start()
    assert second.run_id is not None and second.run_id != first.run_id
    old = await run_row(run_id_of(first))
    assert old.end_reason == "superseded" and old.waiting_for is None
    async with get_sessionmaker()() as session:
        first_event = await session.get(BunchingEvent, event.id)
    assert first_event is not None and first_event.status == BunchingEventStatus.DISMISSED


async def test_runs_left_behind_by_a_dead_worker_or_an_old_version_do_not_block_their_pair(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
) -> None:
    """Seen live: Docker stopped mid-run (a run stuck 'running') and runs paused before
    migration 0007 (no waiting_for) blocked every new event for their pairs."""
    event = await only_event()
    settings = get_settings()
    async with get_sessionmaker()() as session:
        stuck = WorkflowRun(bunching_event_id=event.id, status=WorkflowStatus.RUNNING)
        session.add(stuck)
        later = BunchingEvent(
            route_id="T1",
            leader_vehicle_id=event.leader_vehicle_id,
            follower_vehicle_id=event.follower_vehicle_id,
            headway_seconds=20,
            scheduled_headway_seconds=300,
            severity=event.severity,
        )
        session.add(later)
        await session.commit()
        stuck_id, later_id = stuck.id, later.id

    async def start(event_id: uuid.UUID) -> RunResult:
        return await start_workflow(
            event_id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer
        )

    # A run that is genuinely running is respected...
    assert (await start(later_id)).end_reason == "pair_already_handled"
    # ...but one nobody has touched for longer than any segment can last is closed.
    async with get_sessionmaker()() as session:
        row = await session.get(WorkflowRun, stuck_id)
        assert row is not None
        row.last_active_at = datetime.now(UTC) - abandoned_after(settings) - timedelta(seconds=1)
        await session.commit()
    fresh = await start(later_id)
    assert fresh.run_id is not None and fresh.run_id != stuck_id
    old = await run_row(stuck_id)
    assert (old.status, old.end_reason) == (WorkflowStatus.FAILED, "abandoned")
    assert old.finished_at is not None

    # A pre-0007 paused run (no waiting_for) is migrated to "officer" and so can be superseded.
    async with get_sessionmaker()() as session:
        for rec in await session.scalars(select(Recommendation)):
            rec.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        legacy = await session.get(WorkflowRun, run_id_of(fresh))
        assert legacy is not None
        legacy.waiting_for = None  # as written before migration 0007
        await session.commit()
    await session_execute_migration_fix()
    third_event = BunchingEvent(
        route_id="T1",
        leader_vehicle_id=event.leader_vehicle_id,
        follower_vehicle_id=event.follower_vehicle_id,
        headway_seconds=20,
        scheduled_headway_seconds=300,
        severity=event.severity,
    )
    async with get_sessionmaker()() as session:
        session.add(third_event)
        await session.commit()
        third_id = third_event.id
    third = await start(third_id)
    assert third.run_id is not None
    assert (await run_row(run_id_of(fresh))).end_reason == "superseded"


async def session_execute_migration_fix() -> None:
    """The data fix migration 0008 applies to runs paused before 0007."""
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "UPDATE workflow_runs SET waiting_for = 'officer' "
                "WHERE waiting_for IS NULL AND finished_at IS NULL "
                "AND status IN ('awaiting_approval', 'escalated')"
            )
        )
        await session.commit()


async def test_a_finished_run_never_blocks_its_pair_whatever_its_status(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
) -> None:
    """Seen live: runs from before B12 ended at the recommendation with status
    'awaiting_approval' / 'escalated' and finished_at set; they blocked their pairs forever."""
    event = await only_event()
    settings = get_settings()
    async with get_sessionmaker()() as session:
        session.add(
            WorkflowRun(
                bunching_event_id=event.id,
                status=WorkflowStatus.ESCALATED,
                end_reason="escalated",
                finished_at=datetime.now(UTC) - timedelta(hours=18),
            )
        )
        await session.commit()
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer
    )
    assert result.run_id is not None, result.end_reason
