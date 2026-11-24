import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from arq import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_job_queue, get_redis
from app.api.schemas import (
    ActionParams,
    AssignmentOut,
    AssignmentResponse,
    CantComplyRequest,
    CurrentInstructionResponse,
    InstructionOut,
)
from app.auth.deps import require_role
from app.db.enums import InstructionStatus, NotificationKind, Role
from app.db.models import DriverAssignment, DriverInstruction, Notification, User, Vehicle
from app.db.session import get_session
from app.graph.control import apply_in_world, queue_resume, run_of_instruction, world_time_s
from app.live.channels import ALERT_CHANNEL

log = structlog.get_logger()

router = APIRouter(prefix="/driver", tags=["driver"])

Driver = Annotated[User, Depends(require_role(Role.DRIVER))]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/assignment", response_model=AssignmentResponse)
async def current_assignment(
    driver: Driver, session: Annotated[AsyncSession, Depends(get_session)]
) -> AssignmentResponse:
    """The bus this driver is currently assigned to (only their own)."""
    row = (
        await session.execute(
            select(DriverAssignment, Vehicle)
            .join(Vehicle, Vehicle.id == DriverAssignment.vehicle_id)
            .where(DriverAssignment.driver_id == driver.id, DriverAssignment.ends_at.is_(None))
        )
    ).first()
    if row is None:
        return AssignmentResponse(assignment=None)
    assignment, vehicle = row
    return AssignmentResponse(
        assignment=AssignmentOut(
            vehicle_id=vehicle.id,
            fleet_number=vehicle.fleet_number,
            route_id=vehicle.route_id,
            starts_at=assignment.starts_at,
        )
    )


# ------------------------------------------------------------------ instructions (B12)


def _out(row: DriverInstruction) -> InstructionOut:
    return InstructionOut(
        id=row.id,
        instruction_id=row.instruction_id,
        vehicle_id=row.vehicle_id,
        action_type=row.action_type,
        params=ActionParams.model_validate(row.params),
        status=row.status,
        sent_at=row.sent_at,
        acknowledged_at=row.acknowledged_at,
        done_at=row.done_at,
        cancel_reason=row.cancel_reason,
    )


async def _own_vehicle(session: AsyncSession, driver: User) -> uuid.UUID | None:
    vehicle: uuid.UUID | None = await session.scalar(
        select(DriverAssignment.vehicle_id).where(
            DriverAssignment.driver_id == driver.id, DriverAssignment.ends_at.is_(None)
        )
    )
    return vehicle


async def _own_instruction(
    session: AsyncSession, driver: User, instruction_id: uuid.UUID
) -> DriverInstruction:
    """The instruction, locked, if it is for this driver's bus (rule 10); else 404."""
    row = await session.scalar(
        select(DriverInstruction).where(DriverInstruction.id == instruction_id).with_for_update()
    )
    vehicle = await _own_vehicle(session, driver)
    if row is None or (row.driver_id != driver.id and row.vehicle_id != vehicle):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Instruction not found")
    return row


@router.get("/instructions/current", response_model=CurrentInstructionResponse)
async def current_instruction(driver: Driver, session: Session) -> CurrentInstructionResponse:
    """The instruction this driver should act on now (sent or acknowledged), if any."""
    vehicle = await _own_vehicle(session, driver)
    row = await session.scalar(
        select(DriverInstruction)
        .where(
            DriverInstruction.vehicle_id == vehicle,
            DriverInstruction.status.in_([InstructionStatus.SENT, InstructionStatus.ACKNOWLEDGED]),
        )
        .order_by(DriverInstruction.sent_at.desc())
        .limit(1)
    )
    return CurrentInstructionResponse(instruction=_out(row) if row else None)


@router.post("/instructions/{instruction_id}/ack", response_model=InstructionOut)
async def acknowledge(
    instruction_id: uuid.UUID,
    driver: Driver,
    session: Session,
    redis: Annotated[Redis, Depends(get_redis)],
    queue: Annotated[ArqRedis, Depends(get_job_queue)],
) -> InstructionOut:
    """The driver will do it: the world carries it out and the outcome is measured later.
    Acknowledging twice is harmless."""
    row = await _own_instruction(session, driver, instruction_id)
    if row.status == InstructionStatus.ACKNOWLEDGED:
        return _out(row)
    if row.status != InstructionStatus.SENT:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Instruction is {row.status}")
    row.status, row.acknowledged_at = InstructionStatus.ACKNOWLEDGED, datetime.now(UTC)
    run_id = await run_of_instruction(session, row)
    await session.commit()
    await apply_in_world(redis, session, row)
    if run_id is not None:
        value = {
            "kind": "ack",
            "instruction_id": row.instruction_id,
            "world_time_s": await world_time_s(redis),
        }
        await queue_resume(queue, run_id, "driver", value, f"{row.instruction_id}:ack")
    log.info(
        "instruction_acknowledged", instruction_id=row.instruction_id, driver_id=str(driver.id)
    )
    return _out(row)


@router.post("/instructions/{instruction_id}/done", response_model=InstructionOut)
async def done(instruction_id: uuid.UUID, driver: Driver, session: Session) -> InstructionOut:
    row = await _own_instruction(session, driver, instruction_id)
    if row.status == InstructionStatus.DONE:
        return _out(row)
    if row.status != InstructionStatus.ACKNOWLEDGED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Acknowledge the instruction first")
    row.status, row.done_at = InstructionStatus.DONE, datetime.now(UTC)
    await session.commit()
    return _out(row)


@router.post("/instructions/{instruction_id}/cant-comply", response_model=InstructionOut)
async def cant_comply(
    instruction_id: uuid.UUID,
    body: CantComplyRequest,
    driver: Driver,
    session: Session,
    redis: Annotated[Redis, Depends(get_redis)],
    queue: Annotated[ArqRedis, Depends(get_job_queue)],
) -> InstructionOut:
    """The driver cannot carry it out (blocking traffic / bus full / other): the instruction
    is cancelled, officers are alerted and the workflow re-plans."""
    row = await _own_instruction(session, driver, instruction_id)
    if row.status == InstructionStatus.CANT_COMPLY:
        return _out(row)
    if row.status not in (InstructionStatus.SENT, InstructionStatus.ACKNOWLEDGED):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Instruction is {row.status}")
    row.status = InstructionStatus.CANT_COMPLY
    row.cancel_reason, row.cancel_note = body.reason.value, body.note
    run_id = await run_of_instruction(session, row)
    vehicle = await session.get(Vehicle, row.vehicle_id)
    fleet = vehicle.fleet_number if vehicle else "?"
    officers = (await session.scalars(select(User.id).where(User.role == Role.OFFICER))).all()
    title = f"{fleet} cannot comply: {body.reason.value.replace('_', ' ')}"
    session.add_all(
        Notification(
            user_id=officer_id,
            driver_instruction_id=row.id,
            kind=NotificationKind.SERVICE,
            title=title,
            body=body.note or "The workflow is re-planning with fresh data.",
        )
        for officer_id in officers
    )
    await session.commit()
    try:
        await redis.publish(ALERT_CHANNEL, json.dumps({"type": "alert", "title": title}))
    except RedisError as exc:
        log.warning("alert_publish_failed", error=repr(exc))
    if run_id is not None:
        value = {
            "kind": "cant_comply",
            "instruction_id": row.instruction_id,
            "reason": body.reason.value,
        }
        await queue_resume(queue, run_id, "driver", value, f"{row.instruction_id}:cant")
    log.info("instruction_cant_comply", instruction_id=row.instruction_id, reason=body.reason.value)
    return _out(row)
