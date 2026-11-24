"""`send_driver_instruction` and its own guards (rule 3: never rely on the agent).

In order, at send time:
1. idempotency: a replayed `instruction_id` with the same request returns the original
   ("already_sent", nothing resent); the same id with a different request is refused;
2. the approval exists, is approved (or edited), and matches exactly this bus, action type and
   parameters (an agent cannot change the action after the officer approved it);
3. the approval has not already produced an instruction (enforced by a unique constraint too);
4. the bus has a GPS fix and it is not stale (rule 4, computed here, at send time);
5. the action is valid for where the bus is now (skip / short-turn only at upcoming stops,
   short-turn only where the bus can turn).
Then the instruction is stored and published for the driver's app.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ActionType, ApprovalDecision
from app.db.models import (
    Approval,
    CandidateAction,
    DriverAssignment,
    DriverInstruction,
    Recommendation,
    Vehicle,
    VehiclePosition,
)
from mcp_servers.errors import tool_error
from mcp_servers.observations import age_seconds, route_model
from mcp_servers.schemas import (
    HoldAction,
    InstructionSent,
    ShortTurnAction,
    SkipStopAction,
    action_params,
)

log = structlog.get_logger()

AnyAction = HoldAction | SkipStopAction | ShortTurnAction
Publish = Callable[[InstructionSent], Awaitable[None]]
APPROVED = (ApprovalDecision.APPROVED, ApprovalDecision.EDITED)


def _sent(row: DriverInstruction, fleet_number: str, status: str) -> InstructionSent:
    return InstructionSent.model_validate(
        {
            "id": row.id,
            "instruction_id": row.instruction_id,
            "approval_id": row.approval_id,
            "vehicle_id": row.vehicle_id,
            "fleet_number": fleet_number,
            "driver_id": row.driver_id,
            "action": {"type": row.action_type.value, **row.params},
            "status": status,
            "sent_at": row.sent_at,
        }
    )


async def _replay(
    session: AsyncSession,
    instruction_id: str,
    approval_id: object,
    vehicle_id: object,
    action: AnyAction,
) -> InstructionSent | None:
    row = await session.scalar(
        select(DriverInstruction).where(DriverInstruction.instruction_id == instruction_id)
    )
    if row is None:
        return None
    same = (
        row.approval_id == approval_id
        and row.vehicle_id == vehicle_id
        and row.action_type.value == action.type
        and row.params == action_params(action)
    )
    if not same:
        raise tool_error(
            "instruction_id_conflict",
            "instruction_id was already used for a different instruction",
            instruction_id=instruction_id,
        )
    vehicle = await session.get(Vehicle, row.vehicle_id)
    log.info("instruction_replayed", instruction_id=instruction_id)
    return _sent(row, vehicle.fleet_number if vehicle else "?", "already_sent")


async def _check_approval(
    session: AsyncSession, approval_id: object, vehicle_id: object, action: AnyAction
) -> Approval:
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise tool_error("approval_not_found", "no such approval", approval_id=str(approval_id))
    if approval.decision not in APPROVED:
        raise tool_error(
            "not_approved",
            f"the officer's decision was '{approval.decision}'",
            approval_id=str(approval_id),
        )
    recommendation = await session.get(Recommendation, approval.recommendation_id)
    candidate = (
        await session.get(CandidateAction, recommendation.candidate_id) if recommendation else None
    )
    if candidate is None:
        raise tool_error("approval_mismatch", "the approval has no recommended action")
    approved_params = (
        approval.edited_params
        if approval.decision == ApprovalDecision.EDITED and approval.edited_params is not None
        else candidate.params
    )
    if (
        candidate.vehicle_id != vehicle_id
        or candidate.action_type.value != action.type
        or approved_params != action_params(action)
    ):
        raise tool_error(
            "approval_mismatch",
            "the instruction differs from what the officer approved",
            approved_action=candidate.action_type.value,
            approved_vehicle_id=str(candidate.vehicle_id),
        )
    used = await session.scalar(
        select(DriverInstruction.instruction_id).where(DriverInstruction.approval_id == approval_id)
    )
    if used is not None:
        raise tool_error(
            "approval_already_used",
            "this approval already produced an instruction",
            instruction_id=used,
        )
    return approval


async def _check_position(
    session: AsyncSession, vehicle: Vehicle, action: AnyAction, now: datetime, stale_after_s: int
) -> None:
    fix = await session.scalar(
        select(VehiclePosition)
        .where(VehiclePosition.vehicle_id == vehicle.id)
        .order_by(VehiclePosition.recorded_at.desc())
        .limit(1)
    )
    if fix is None or fix.direction_id is None or fix.distance_along_m is None:
        raise tool_error("no_position", f"{vehicle.fleet_number} has no GPS position")
    age = age_seconds(fix.recorded_at, now)
    if age > stale_after_s:
        raise tool_error(
            "stale_vehicle",
            f"{vehicle.fleet_number}'s GPS is {age:.0f} s old; stale vehicles are never instructed",
            age_seconds=age,
            stale_after_seconds=stale_after_s,
        )
    if isinstance(action, HoldAction):
        return
    route = (await route_model(session, vehicle.route_id)).model
    direction = route.direction(fix.direction_id)
    here = direction.stop_index_at(fix.distance_along_m)
    index = {s.stop_id: i for i, s in enumerate(direction.stops)}

    def upcoming(stop_id: str) -> int:
        i = index.get(stop_id)
        if i is None or not here < i < direction.n_stops - 1:
            raise tool_error(
                "invalid_action",
                f"{stop_id} is not an upcoming intermediate stop for {vehicle.fleet_number}",
                direction_id=fix.direction_id,
                last_stop_passed=direction.stops[here].stop_id,
            )
        return i

    if isinstance(action, SkipStopAction):
        if len(set(action.stop_ids)) != len(action.stop_ids):
            raise tool_error("invalid_action", "a stop is listed twice")
        for stop_id in action.stop_ids:
            upcoming(stop_id)
        return
    i = upcoming(action.at_stop_id)
    pair = route.turn_pairs[fix.direction_id][i]
    other = route.direction(1 - fix.direction_id)
    if pair is None or pair >= other.n_stops - 1:
        raise tool_error(
            "invalid_action",
            f"a bus cannot turn round at {action.at_stop_id} (no opposite stop nearby)",
        )


async def send_instruction(
    session: AsyncSession,
    *,
    instruction_id: str,
    approval_id: object,
    vehicle_id: object,
    action: AnyAction,
    now: datetime,
    stale_after_s: int,
    publish: Publish | None = None,
) -> InstructionSent:
    replay = await _replay(session, instruction_id, approval_id, vehicle_id, action)
    if replay is not None:
        return replay
    await _check_approval(session, approval_id, vehicle_id, action)
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None:
        raise tool_error("vehicle_not_found", "no such vehicle", vehicle_id=str(vehicle_id))
    await _check_position(session, vehicle, action, now, stale_after_s)

    driver_id = await session.scalar(
        select(DriverAssignment.driver_id).where(
            DriverAssignment.vehicle_id == vehicle.id, DriverAssignment.ends_at.is_(None)
        )
    )
    row = DriverInstruction(
        instruction_id=instruction_id,
        approval_id=approval_id,
        vehicle_id=vehicle.id,
        driver_id=driver_id,
        action_type=ActionType(action.type),
        params=action_params(action),
        sent_at=now,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent request won the race (same instruction_id, or same approval).
        await session.rollback()
        replay = await _replay(session, instruction_id, approval_id, vehicle_id, action)
        if replay is not None:
            return replay
        raise tool_error(
            "approval_already_used", "this approval already produced an instruction"
        ) from None
    sent = _sent(row, vehicle.fleet_number, "sent")
    log.info(
        "instruction_sent",
        instruction_id=instruction_id,
        approval_id=str(approval_id),
        vehicle=vehicle.fleet_number,
        action=action.type,
        driver_assigned=driver_id is not None,
    )
    if publish is not None:
        await publish(sent)
    return sent
