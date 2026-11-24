"""Tool inputs and outputs for avl-mcp and demand-mcp (rule 6: every tool boundary is a Pydantic
model). The MCP client (B8) validates results against these same models.

Everything here is what an operator could observe, computed server-side (rules 1 and 4).
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

RouteId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
StopId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
InstructionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,64}$")]
DirectionId = Annotated[int, Field(ge=0, le=1)]


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


# ------------------------------------------------------------------ errors

ErrorCode = Literal[
    "route_not_found",
    "vehicle_not_found",
    "approval_not_found",
    "not_approved",
    "approval_mismatch",
    "approval_already_used",
    "instruction_id_conflict",
    "no_position",
    "stale_vehicle",
    "invalid_action",
]


class ToolErrorBody(_Out):
    """Sent as the text of an `is_error` tool result, so callers can branch on `code`."""

    code: ErrorCode
    message: str
    details: dict[str, str | int | float | bool | None] = {}


# ------------------------------------------------------------------ avl-mcp


class VehiclePosition(_Out):
    vehicle_id: uuid.UUID
    fleet_number: str
    direction_id: DirectionId
    lon: float
    lat: float
    distance_m: float | None  # along the direction's route geometry
    speed_mps: float | None
    heading_deg: float | None
    load: int | None  # passenger counter, if reported
    recorded_at: datetime  # GPS fix time
    world_time_s: float | None  # the world's clock at the fix (seconds since midnight)
    age_seconds: float  # computed by the server at request time
    is_stale: bool  # age_seconds > STALE_GPS_SECONDS (server-side, rule 4)


class VehiclePositions(_Out):
    route_id: str
    server_time: datetime
    stale_after_seconds: int
    vehicles: list[VehiclePosition]
    missing: list[str]  # fleet numbers that have never reported


class HeadwayPair(_Out):
    direction_id: DirectionId
    leader_vehicle_id: uuid.UUID
    leader_fleet_number: str
    follower_vehicle_id: uuid.UUID
    follower_fleet_number: str
    gap_m: float
    headway_seconds: float
    scheduled_headway_seconds: float
    ratio: float  # headway / scheduled headway
    status: Literal["ok", "closing", "bunched", "stale"]
    data_quality: Literal["ok", "stale"]  # stale: one of the two fixes is too old to trust
    leader_age_seconds: float
    follower_age_seconds: float


class Headways(_Out):
    route_id: str
    server_time: datetime
    bunching_ratio: float
    closing_ratio: float
    stale_after_seconds: int
    pairs: list[HeadwayPair]
    not_in_service: list[str]  # waiting at a terminal (left out of headways)
    missing: list[str]  # never reported


# ------------------------------------------------------------------ demand-mcp


class StopEstimate(_Out):
    direction_id: DirectionId
    stop_id: str
    name: str
    sequence: int
    waiting_now: int | None
    arrivals_per_min: float | None  # estimated from recent counts on the world clock
    expected_boardings: float | None  # waiting_now + arrivals over the horizon
    sample_age_seconds: float | None
    data_quality: Literal["ok", "stale", "no_data"]


class BoardingEstimates(_Out):
    route_id: str
    server_time: datetime
    horizon_minutes: int
    stops: list[StopEstimate]


class HoldAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["hold"]
    seconds: Annotated[int, Field(ge=1, le=600)]


class SkipStopAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["skip_stop"]
    stop_ids: Annotated[list[StopId], Field(min_length=1, max_length=5)]


class ShortTurnAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["short_turn"]
    at_stop_id: StopId


Action = Annotated[HoldAction | SkipStopAction | ShortTurnAction, Field(discriminator="type")]


class InstructionSent(_Out):
    id: uuid.UUID
    instruction_id: str
    approval_id: uuid.UUID
    vehicle_id: uuid.UUID
    fleet_number: str
    driver_id: uuid.UUID | None  # None: no driver signed in to this bus
    action: Action
    status: Literal["sent", "already_sent"]  # already_sent: same request replayed, not resent
    sent_at: datetime


def action_params(action: HoldAction | SkipStopAction | ShortTurnAction) -> dict[str, object]:
    """The canonical `params` stored with candidates, approvals and instructions."""
    return action.model_dump(exclude={"type"})
