"""Database-level guarantees that later modules rely on (rules 3 and 4, B12)."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import create_user
from app.db.enums import ActionType, ApprovalDecision, CandidateSource, Role
from app.db.models import (
    Approval,
    CandidateAction,
    DriverAssignment,
    DriverInstruction,
    Recommendation,
    Vehicle,
    WorkflowRun,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.services


async def _approval(session: AsyncSession) -> tuple[Approval, Vehicle, Recommendation]:
    officer = await create_user(
        session,
        email=f"o{uuid.uuid4().hex[:6]}@t.io",
        password="password-123",
        role=Role.OFFICER,
        display_name="O",
    )
    bus = Vehicle(fleet_number=f"TN{uuid.uuid4().hex[:6]}", route_id="R21G")
    run = WorkflowRun()
    session.add_all([bus, run])
    await session.flush()
    cand = CandidateAction(
        run_id=run.id,
        action_type=ActionType.HOLD,
        vehicle_id=bus.id,
        params={"hold_seconds": 60},
        source=CandidateSource.RULE,
    )
    session.add(cand)
    await session.flush()
    rec = Recommendation(
        run_id=run.id,
        candidate_id=cand.id,
        ranking=[],
        benefit_passenger_minutes=12.5,
        explanation="test",
    )
    session.add(rec)
    await session.flush()
    approval = Approval(
        recommendation_id=rec.id, officer_id=officer.id, decision=ApprovalDecision.APPROVED
    )
    session.add(approval)
    await session.commit()
    return approval, bus, rec


async def test_instruction_id_is_an_idempotency_key(db: None) -> None:
    async with get_sessionmaker()() as session:
        approval, bus, _ = await _approval(session)
        for _ in range(2):
            session.add(
                DriverInstruction(
                    instruction_id="instr-1",
                    approval_id=approval.id,
                    vehicle_id=bus.id,
                    action_type=ActionType.HOLD,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_one_decision_per_recommendation(db: None) -> None:
    async with get_sessionmaker()() as session:
        approval, _, rec = await _approval(session)
        session.add(
            Approval(
                recommendation_id=rec.id,
                officer_id=approval.officer_id,
                decision=ApprovalDecision.APPROVED,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_rejection_needs_a_reason(db: None) -> None:
    async with get_sessionmaker()() as session:
        approval, _, rec = await _approval(session)
        await session.delete(approval)
        await session.flush()
        session.add(
            Approval(
                recommendation_id=rec.id,
                officer_id=approval.officer_id,
                decision=ApprovalDecision.REJECTED,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_vehicle_has_one_open_driver_assignment(db: None) -> None:
    async with get_sessionmaker()() as session:
        bus = Vehicle(fleet_number="TN-ONE", route_id="R21G")
        session.add(bus)
        drivers = [
            await create_user(
                session,
                email=f"d{i}@t.io",
                password="password-123",
                role=Role.DRIVER,
                display_name="D",
            )
            for i in range(2)
        ]
        await session.flush()
        for d in drivers:
            session.add(DriverAssignment(driver_id=d.id, vehicle_id=bus.id))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_unknown_enum_value_rejected_by_database(db: None) -> None:
    async with get_sessionmaker()() as session:
        run = WorkflowRun()
        session.add(run)
        await session.flush()
        from sqlalchemy import text

        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE workflow_runs SET status = 'exploded' WHERE id = :id"),
                {"id": run.id},
            )
