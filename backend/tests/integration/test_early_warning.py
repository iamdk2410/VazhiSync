"""B21 end to end: the early-warning sweep reads the MCP servers, raises a predicted event,
the workflow acts on a pair that hasn't bunched yet, and the same dedupe applies.

The predictor itself is proven on a golden scenario in tests/unit/test_early_warning.py; here
its answer is fixed so the plumbing is tested deterministically on the small T1 route.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arq import ArqRedis
from arq.worker import Worker
from sqlalchemy import select

from app.agents import early_warning as ew
from app.agents.early_warning import Prediction, early_warning_sweep
from app.config import get_settings
from app.db.enums import (
    BunchingEventKind,
    BunchingEventStatus,
    BunchingSeverity,
    WorkflowStatus,
)
from app.db.models import BunchingEvent, WorkflowRun
from app.db.session import get_sessionmaker
from app.graph.workflow import start_workflow
from app.runner.route_loader import load_world_route
from app.runner.world import WorldRunner
from app.workers.workflows import retry_open_events
from tests.integration.test_workflow import (  # noqa: F401
    bunched,
    clock,
    harness,
    redis,
    state_of,
)

pytestmark = pytest.mark.services


async def predicted_events() -> list[BunchingEvent]:
    async with get_sessionmaker()() as session:
        return list(
            await session.scalars(
                select(BunchingEvent).where(BunchingEvent.kind == BunchingEventKind.PREDICTED)
            )
        )


@pytest.fixture
def predicts_02_03(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The look-ahead says: T1-03 catches up with T1-02 in 9 minutes. Records its inputs."""
    calls: list[dict[str, Any]] = []

    def fake(*args: Any, **kwargs: Any) -> list[Prediction]:
        calls.append({"current": args[4], **kwargs})
        return [Prediction(0, "T1-02", "T1-03", 540.0, 3, 3)]

    monkeypatch.setattr(ew, "predict_bunching", fake)
    return calls


async def test_a_prediction_raises_an_event_and_the_workflow_acts_before_the_bunch(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    predicts_02_03: list[dict[str, Any]],
) -> None:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        route = await load_world_route(session, "T1")
    queued: list[uuid.UUID] = []

    async def enqueue(event_id: uuid.UUID) -> None:
        queued.append(event_id)

    raised = await early_warning_sweep(
        harness(settings), get_sessionmaker(), settings, route, enqueue
    )

    (event,) = await predicted_events()
    assert raised == queued == [event.id]
    assert event.status == BunchingEventStatus.OPEN and event.predicted_in_seconds == 540.0
    # The look-ahead got what avl-mcp reported: the pair it watches is not bunched now.
    current = {
        (p.leader_fleet_number, p.follower_fleet_number): p for p in predicts_02_03[0]["current"]
    }
    assert current[("T1-02", "T1-03")].status in ("ok", "closing")
    assert predicts_02_03[0]["horizon_s"] == settings.early_warning_horizon_minutes * 60

    # Same dedupe as actual events: no second event while this one is open.
    again = await early_warning_sweep(
        harness(settings), get_sessionmaker(), settings, route, enqueue
    )
    assert again == [] and len(await predicted_events()) == 1

    # The workflow runs on it although the pair isn't bunched (that's the point).
    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer
    )
    state = state_of(result)
    assert state["event_kind"] == "predicted" and state["predicted_in_s"] == 540.0
    assert result.end_reason in {"recommended", "escalated", "do_nothing_recommended"}
    assert state["snapshot"].pair.status != "bunched"
    holds = [c for c in state["candidates"] if c.action_type == "hold"]
    assert holds and all(30 <= c.params["seconds"] <= 180 for c in holds)
    if result.end_reason == "do_nothing_recommended":  # nothing to watch: closed, not stuck
        async with get_sessionmaker()() as session:
            closed = await session.get(BunchingEvent, event.id)
        assert closed is not None and closed.status == BunchingEventStatus.DISMISSED

    # Once the run is done with it, the cool-down stops an immediate repeat prediction.
    async with get_sessionmaker()() as session:
        row = await session.get(BunchingEvent, event.id)
        assert row is not None
        row.status = BunchingEventStatus.RESOLVED
        await session.commit()
    assert (
        await early_warning_sweep(harness(settings), get_sessionmaker(), settings, route, enqueue)
        == []
    )


async def test_a_finished_runs_handling_event_does_not_block_a_prediction(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    predicts_02_03: list[dict[str, Any]],
) -> None:
    runner, _ = bunched
    settings = get_settings()
    async with get_sessionmaker()() as session:
        route = await load_world_route(session, "T1")
        leftover = BunchingEvent(
            route_id="T1",
            leader_vehicle_id=runner.vehicle_ids["T1-02"],
            follower_vehicle_id=runner.vehicle_ids["T1-03"],
            headway_seconds=100,
            scheduled_headway_seconds=300,
            severity=BunchingSeverity.BUNCHED,
            status=BunchingEventStatus.HANDLING,
        )
        session.add(leftover)
        await session.flush()
        session.add(
            WorkflowRun(
                bunching_event_id=leftover.id,
                status=WorkflowStatus.COMPLETED,
                end_reason="do_nothing_recommended",
                finished_at=datetime.now(UTC),
            )
        )
        await session.commit()

    async def enqueue(event_id: uuid.UUID) -> None:
        return None

    raised = await early_warning_sweep(
        harness(settings), get_sessionmaker(), settings, route, enqueue
    )
    assert len(raised) == 1


async def test_predictions_are_not_the_runners_and_go_stale_instead_of_retrying(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    job_queue: ArqRedis,
    predicts_02_03: list[dict[str, Any]],
) -> None:
    runner, _ = bunched
    settings = get_settings()
    async with get_sessionmaker()() as session:
        route = await load_world_route(session, "T1")

    async def enqueue(event_id: uuid.UUID) -> None:
        return None

    (event_id,) = await early_warning_sweep(
        harness(settings), get_sessionmaker(), settings, route, enqueue
    )

    # A restarted runner reloads its own open events, not the prediction (it would resolve it
    # at once: the pair's headway is fine now).
    runner.tracker.open_events.clear()
    await runner.setup()
    assert event_id not in runner.tracker.open_events.values()

    # Nobody acted on it in time: the retry sweep dismisses it instead of retrying it.
    async with get_sessionmaker()() as session:
        row = await session.get(BunchingEvent, event_id)
        assert row is not None
        row.detected_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()
    retried = await retry_open_events({"redis": job_queue})
    (event,) = await predicted_events()
    assert event.status == BunchingEventStatus.DISMISSED
    jobs = [j.args for j in await job_queue.queued_jobs()]
    assert all(str(event_id) not in [str(a) for a in args] for args in jobs)
    assert retried >= 0


async def test_an_ignored_recommendation_does_not_block_predictions_for_its_pair(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    predicts_02_03: list[dict[str, Any]],
) -> None:
    """Like the workflow's claim: a run waiting on an expired recommendation is stale."""
    runner, _ = bunched
    settings = get_settings()
    async with get_sessionmaker()() as session:
        route = await load_world_route(session, "T1")
        old = BunchingEvent(
            route_id="T1",
            leader_vehicle_id=runner.vehicle_ids["T1-02"],
            follower_vehicle_id=runner.vehicle_ids["T1-03"],
            headway_seconds=100,
            scheduled_headway_seconds=300,
            severity=BunchingSeverity.BUNCHED,
            status=BunchingEventStatus.HANDLING,
        )
        session.add(old)
        await session.flush()
        waiting = WorkflowRun(
            bunching_event_id=old.id,
            status=WorkflowStatus.AWAITING_APPROVAL,
            waiting_for="officer",
        )
        session.add(waiting)
        await session.commit()

    async def enqueue(event_id: uuid.UUID) -> None:
        return None

    async def sweep() -> list[uuid.UUID]:
        return await early_warning_sweep(
            harness(settings), get_sessionmaker(), settings, route, enqueue
        )

    async with get_sessionmaker()() as session:  # a fresh recommendation: the pair is taken
        await _recommend(session, waiting.id, expires_in=timedelta(seconds=60))
    assert await sweep() == []
    async with get_sessionmaker()() as session:  # nobody decided in time
        await _recommend(session, waiting.id, expires_in=timedelta(seconds=-1), replace=True)
    assert len(await sweep()) == 1


async def _recommend(
    session: Any, run_id: uuid.UUID, expires_in: timedelta, replace: bool = False
) -> None:
    from app.db.enums import ActionType, CandidateSource
    from app.db.models import CandidateAction, Recommendation

    if replace:
        for rec in await session.scalars(select(Recommendation)):
            rec.expires_at = datetime.now(UTC) + expires_in
        await session.commit()
        return
    cand = CandidateAction(
        run_id=run_id, action_type=ActionType.DO_NOTHING, params={},
        source=CandidateSource.RULE, rationale="test",
    )  # fmt: skip
    session.add(cand)
    await session.flush()
    session.add(
        Recommendation(
            run_id=run_id,
            candidate_id=cand.id,
            ranking=[],
            benefit_passenger_minutes=0,
            explanation="test",
            expires_at=datetime.now(UTC) + expires_in,
        )
    )
    await session.commit()
