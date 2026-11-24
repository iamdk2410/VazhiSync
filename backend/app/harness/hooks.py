"""Pre- and post-tool hooks.

Pre-hooks run before the call leaves the process and may refuse it (HookRejected). The send
guard deliberately duplicates send_driver_instruction's own server guards (defence in depth,
rule 12): an unapproved or stale send is stopped here, and would be stopped again by the server.
Post-hooks run on every validated result.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import ApprovalDecision
from app.db.models import Approval, CandidateAction, DriverInstruction, Recommendation
from app.harness.errors import HarnessError, HookRejected
from mcp_servers.schemas import (
    HoldAction,
    ShortTurnAction,
    SkipStopAction,
    VehiclePositions,
    action_params,
)


@dataclass(frozen=True)
class ToolCall:
    run_id: uuid.UUID
    agent: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


class PreHook(Protocol):
    async def __call__(self, call: ToolCall) -> None: ...


class PostHook(Protocol):
    async def __call__(self, call: ToolCall, result: BaseModel) -> None: ...


class _SendArgs(BaseModel):
    instruction_id: str
    approval_id: uuid.UUID
    vehicle_id: uuid.UUID
    action: HoldAction | SkipStopAction | ShortTurnAction


PositionsLookup = Callable[[], Awaitable[VehiclePositions]]


class SendGuard:
    """Refuses send_driver_instruction unless the approval and the bus check out right now."""

    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], positions: PositionsLookup
    ) -> None:
        self._sessionmaker = sessionmaker
        self._positions = positions

    async def __call__(self, call: ToolCall) -> None:
        if call.tool != "send_driver_instruction":
            return
        try:
            args = _SendArgs.model_validate(call.arguments)
        except ValidationError:
            raise HookRejected(
                "malformed instruction", tool=call.tool, code="invalid_arguments"
            ) from None
        await self._check_approval(call.tool, args)
        await self._check_fresh(call.tool, args.vehicle_id)

    async def _check_approval(self, tool: str, args: _SendArgs) -> None:
        async with self._sessionmaker() as session:
            approval = await session.get(Approval, args.approval_id)
            if approval is None or approval.decision not in (
                ApprovalDecision.APPROVED,
                ApprovalDecision.EDITED,
            ):
                raise HookRejected(
                    "no approved decision for this instruction", tool=tool, code="not_approved"
                )
            rec = await session.get(Recommendation, approval.recommendation_id)
            cand = await session.get(CandidateAction, rec.candidate_id) if rec else None
            approved = (
                approval.edited_params
                if approval.decision == ApprovalDecision.EDITED and approval.edited_params
                else (cand.params if cand else None)
            )
            if (
                cand is None
                or cand.vehicle_id != args.vehicle_id
                or cand.action_type.value != args.action.type
                or approved != action_params(args.action)
            ):
                raise HookRejected(
                    "instruction differs from the approval", tool=tool, code="approval_mismatch"
                )
            used = await session.scalar(
                select(DriverInstruction.instruction_id).where(
                    DriverInstruction.approval_id == args.approval_id
                )
            )
        if used is not None and used != args.instruction_id:
            # (the same instruction_id is a safe replay: the server answers "already_sent")
            raise HookRejected("approval already used", tool=tool, code="approval_already_used")

    async def _check_fresh(self, tool: str, vehicle_id: uuid.UUID) -> None:
        try:
            positions = await self._positions()
        except HarnessError:
            # Freshness can't be verified (avl-mcp down): fail closed.
            raise HookRejected(
                "cannot verify GPS freshness (vehicle positions unavailable)",
                tool=tool, code="freshness_unknown",
            ) from None  # fmt: skip
        fix = next((v for v in positions.vehicles if v.vehicle_id == vehicle_id), None)
        if fix is None:
            raise HookRejected("bus has no GPS position", tool=tool, code="no_position")
        if fix.is_stale:
            raise HookRejected(
                f"bus GPS is stale ({fix.age_seconds:.0f} s); stale buses are never instructed",
                tool=tool, code="stale_vehicle",
            )  # fmt: skip
