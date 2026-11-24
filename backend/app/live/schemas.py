"""Live frame models: published by the world runner, validated and filtered by the API.

Everything in a frame is what an operator could observe (GPS fixes, counted queues), never
the world simulator's hidden state (rule 1). Staleness is computed server-side.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import BunchingSeverity, Role
from app.live.headways import HeadwayStatus
from scenario_gen.schema import ScenarioPlan

Crowding = Literal["low", "medium", "high"]
DirectionId = Annotated[int, Field(ge=0, le=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LiveVehicle(_Frozen):
    vehicle_id: uuid.UUID
    fleet_number: str
    direction_id: DirectionId
    lon: float
    lat: float
    heading_deg: float
    distance_m: float
    speed_mps: float
    load: int | None  # officer only; None in passenger frames
    capacity: int
    crowding: Crowding
    last_fix_at: datetime
    age_seconds: float
    is_stale: bool


class LiveQueue(_Frozen):
    direction_id: DirectionId
    stop_id: str
    waiting: int


class LiveHeadway(_Frozen):
    direction_id: DirectionId
    leader_vehicle_id: uuid.UUID
    follower_vehicle_id: uuid.UUID
    gap_m: float
    headway_seconds: float
    scheduled_headway_seconds: float
    ratio: float
    status: HeadwayStatus


class LiveEvent(_Frozen):
    event_id: uuid.UUID
    leader_vehicle_id: uuid.UUID
    follower_vehicle_id: uuid.UUID
    severity: BunchingSeverity
    detected_at: datetime
    headway_seconds: float


class LiveFrame(_Frozen):
    type: Literal["frame"] = "frame"
    route_id: str
    sim_time: str  # "08:12:30" on the simulated clock
    sim_elapsed_s: float
    speed: Literal[1, 5, 10]
    scenario: str | None = None  # name of the scenario loaded into the world, if any
    generated_at: datetime
    vehicles: list[LiveVehicle]
    queues: list[LiveQueue]
    headways: list[LiveHeadway]
    events: list[LiveEvent]


class WorldScenario(_Frozen):
    """What the live world should run (set by `POST /lab/scenarios/{id}/apply`).

    The runner restarts the world whenever `token` changes; `plan` None means a normal day.
    """

    token: Annotated[str, Field(min_length=1, max_length=64)]
    scenario_id: uuid.UUID | None = None
    name: str | None = None
    seed: int | None = None
    plan: ScenarioPlan | None = None


class BunchingRaised(_Frozen):
    """Published on BUNCHING_CHANNEL when an event opens; the workflow (B10) starts from it."""

    type: Literal["bunching"] = "bunching"
    event_id: uuid.UUID
    route_id: str
    leader_vehicle_id: uuid.UUID
    follower_vehicle_id: uuid.UUID
    headway_seconds: float
    scheduled_headway_seconds: float
    detected_at: datetime


def crowding(load: int, capacity: int) -> Crowding:
    share = load / capacity if capacity > 0 else 1.0
    if share < 0.5:
        return "low"
    if share < 0.85:
        return "medium"
    return "high"


def project(frame: LiveFrame, role: Role, own_vehicle_id: uuid.UUID | None = None) -> LiveFrame:
    """What one role may see of a frame (rule 10: filtered on the server).

    - officer: everything
    - driver: only their own bus and the gaps to the buses either side of it
    - passenger: public data only: positions, crowding level and staleness (no raw loads,
      queues, headways or events)
    """
    if role == Role.OFFICER:
        return frame
    if role == Role.DRIVER:
        return frame.model_copy(
            update={
                "vehicles": [v for v in frame.vehicles if v.vehicle_id == own_vehicle_id],
                "queues": [],
                "headways": [
                    h
                    for h in frame.headways
                    if own_vehicle_id in (h.leader_vehicle_id, h.follower_vehicle_id)
                ],
                "events": [],
            }
        )
    return frame.model_copy(
        update={
            "vehicles": [v.model_copy(update={"load": None}) for v in frame.vehicles],
            "queues": [],
            "headways": [],
            "events": [],
        }
    )
