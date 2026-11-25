"""B22 acceptance: a repeated scenario retrieves its earlier case and the recommendation card
shows it.

First run: the bunched T1 scenario is recommended, approved and dispatched, which records a
case (the situation as numbers, the action, the predicted benefit); its outcome is then
measured. Repeat: the same situation arises again; the new run retrieves that case (distance
~0) and the officer's recommendation card lists it with what it predicted and achieved.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from arq import ArqRedis
from arq.worker import Worker
from httpx import AsyncClient
from sqlalchemy import select

from app.agents.dispatch import _record_case
from app.config import get_settings
from app.db.enums import InstructionStatus, WorkflowStatus
from app.db.models import BunchingEvent, Case, DriverInstruction, WorkflowRun
from app.db.session import get_sessionmaker
from app.graph.workflow import start_workflow
from app.runner.world import WorldRunner
from tests.conftest import Account
from tests.integration.conftest import bearer, login
from tests.integration.test_approval import (  # noqa: F401
    api,
    paused_run,
    pending,
    run_id_of,
    wait_for,
    waiting,
    worker,
)
from tests.integration.test_workflow import (  # noqa: F401
    bunched,
    clock,
    harness,
    only_event,
    redis,
    state_of,
)

pytestmark = pytest.mark.services


async def the_case() -> Case:
    async with get_sessionmaker()() as session:
        (case,) = (await session.scalars(select(Case))).all()
    return case


async def test_a_repeated_scenario_retrieves_its_earlier_case_on_the_card(
    bunched: tuple[WorldRunner, list[uuid.UUID]],  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    worker: Worker,  # noqa: F811
    api: AsyncClient,  # noqa: F811
    accounts: dict[str, Account],
) -> None:
    # ---- first time: recommended, approved, dispatched -> a case is recorded
    first = await paused_run(job_queue, checkpointer)
    officer = bearer(await login(api, accounts["officer"]))
    rec = await pending(api, officer)
    assert rec["similar_cases"] == []  # nothing learned yet
    resp = await api.post(
        f"/officer/recommendations/{rec['id']}/decision",
        json={"decision": "approve"},
        headers=officer,
    )
    assert resp.status_code == 202, resp.text
    await wait_for(run_id_of(first), waiting("driver"))
    case = await the_case()
    assert case.run_id == run_id_of(first)
    assert case.action_type.value == rec["action_type"]
    assert case.predicted_benefit_passenger_minutes == rec["benefit_passenger_minutes"]
    assert len(case.features) == 9 and case.feature_values["gap_ratio"] < 0.5
    assert case.actual_benefit_passenger_minutes is None  # not measured yet

    # A replayed approval (the send returns already_sent) records no second case.
    await _record_case(
        cast(Any, SimpleNamespace(sessionmaker=get_sessionmaker(), run_id=str(case.run_id))),
        cast(
            Any,
            {
                "case_features": case.feature_values,
                "route_id": "T1",
                "recommendation": SimpleNamespace(
                    benefit_passenger_minutes=case.predicted_benefit_passenger_minutes
                ),
            },
        ),
        cast(Any, SimpleNamespace(action_type=case.action_type.value)),
        case.params,
        cast(Any, SimpleNamespace(id=case.driver_instruction_id)),
    )
    async with get_sessionmaker()() as session:
        assert len((await session.scalars(select(Case))).all()) == 1

    # ---- the intervention is over and measured (an earlier day: no cooldown left)
    async with get_sessionmaker()() as session:
        for instruction in await session.scalars(select(DriverInstruction)):
            instruction.status = InstructionStatus.DONE
            instruction.sent_at = datetime.now(UTC) - timedelta(days=1)
        run = await session.get(WorkflowRun, run_id_of(first))
        assert run is not None
        run.status, run.waiting_for = WorkflowStatus.COMPLETED, None
        run.end_reason, run.finished_at = "outcome_measured", datetime.now(UTC)
        stored = await session.get(Case, case.id)
        assert stored is not None
        stored.actual_benefit_passenger_minutes = 0.8 * case.predicted_benefit_passenger_minutes
        stored.bunching_resolved, stored.measured_at = True, datetime.now(UTC)
        event = await only_event_of(session, run.bunching_event_id)
        repeat = BunchingEvent(
            route_id="T1",
            leader_vehicle_id=event.leader_vehicle_id,
            follower_vehicle_id=event.follower_vehicle_id,
            headway_seconds=event.headway_seconds,
            scheduled_headway_seconds=event.scheduled_headway_seconds,
            severity=event.severity,
        )
        session.add(repeat)
        await session.commit()
        repeat_id = repeat.id

    # ---- the same situation again: the earlier case is retrieved...
    settings = get_settings()
    second = await start_workflow(
        repeat_id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer
    )
    state = state_of(second)
    (similar, *_) = state["similar_cases"]
    assert similar["case_id"] == str(case.id)
    assert similar["distance"] < 0.05  # the same situation
    assert similar["actual_benefit_passenger_minutes"] == pytest.approx(
        0.8 * case.predicted_benefit_passenger_minutes
    )

    # ...and the officer's recommendation card shows it.
    assert second.waiting_for == "officer", second.end_reason
    card = await pending(api, officer)
    assert card["run_id"] == str(run_id_of(second))
    shown = card["similar_cases"][0]
    assert shown["case_id"] == str(case.id)
    assert shown["action_type"] == case.action_type.value
    assert shown["predicted_benefit_passenger_minutes"] == case.predicted_benefit_passenger_minutes
    assert shown["actual_benefit_passenger_minutes"] is not None
    assert card["confidence"] == "normal"  # it delivered 80 % of its prediction


async def only_event_of(session: Any, event_id: uuid.UUID | None) -> BunchingEvent:
    event = await session.get(BunchingEvent, event_id)
    assert event is not None
    return event  # type: ignore[no-any-return]
