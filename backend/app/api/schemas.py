"""Request/response models. Named models only, so the frontend can generate types (B13)."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.auth.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from app.db.enums import (
    ActionType,
    BunchingEventKind,
    BunchingEventStatus,
    BunchingSeverity,
    CantComplyReason,
    InstructionStatus,
    Role,
    ScenarioSource,
    ScenarioStatus,
    WorkflowStatus,
)
from scenario_gen.preview import Preview
from scenario_gen.schema import ScenarioPlan

Password = Annotated[str, Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
StopId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]


class StrictModel(BaseModel):
    """Request bodies reject unknown fields, e.g. a `role` sent to /auth/register."""

    model_config = ConfigDict(extra="forbid")


# --- auth ---
class RegisterRequest(StrictModel):
    email: EmailStr
    password: Password
    display_name: DisplayName


class LoginRequest(StrictModel):
    # Not EmailStr: login must never reject an address that exists in the database.
    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth2 token type, not a secret)
    expires_in: int
    role: Role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: Role
    display_name: str


# --- officer ---
class BunchingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_id: str
    leader_vehicle_id: uuid.UUID
    follower_vehicle_id: uuid.UUID
    detected_at: datetime
    headway_seconds: float
    scheduled_headway_seconds: float
    severity: BunchingSeverity
    status: BunchingEventStatus
    kind: BunchingEventKind = BunchingEventKind.ACTUAL  # predicted: early warning (B21)
    predicted_in_seconds: float | None = None


class AlertsResponse(BaseModel):
    alerts: list[BunchingEventOut]


# --- driver ---
class AssignmentOut(BaseModel):
    vehicle_id: uuid.UUID
    fleet_number: str
    route_id: str
    starts_at: datetime


class AssignmentResponse(BaseModel):
    assignment: AssignmentOut | None


# --- passenger ---
class FavouriteStopIn(StrictModel):
    alert_minutes: Annotated[int, Field(ge=1, le=60)] = 5


class FavouriteStopOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stop_id: str
    alert_minutes: int
    created_at: datetime


class FavouritesResponse(BaseModel):
    favourites: list[FavouriteStopOut]


# --- routes (GeoJSON) ---
class PointGeometry(BaseModel):
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]  # [lon, lat]


class LineStringGeometry(BaseModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]]


class StopProperties(BaseModel):
    stop_id: str
    name: str
    sequence: int
    distance_along_m: float
    scheduled_offset_seconds: int


class StopFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: str
    geometry: PointGeometry
    properties: StopProperties


class StopFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[StopFeature]


class ShapeProperties(BaseModel):
    direction_id: int
    headsign: str
    length_m: float
    shape_source: str


class ShapeFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: LineStringGeometry
    properties: ShapeProperties


class HeadwayHourOut(BaseModel):
    hour: int
    headway_seconds: int
    departures: int


class RouteDirectionOut(BaseModel):
    direction_id: int
    headsign: str
    length_m: float
    scheduled_run_seconds: int
    peak_headway_seconds: int
    trips_per_day: int
    first_departure: str
    last_departure: str
    headway_profile: list[HeadwayHourOut]
    shape: ShapeFeature
    stops: StopFeatureCollection


class RouteSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    short_name: str
    long_name: str
    agency: str


class RouteOut(RouteSummary):
    attribution: str | None
    directions: list[RouteDirectionOut]


class RoutesResponse(BaseModel):
    routes: list[RouteSummary]


# --- live world (B6) ---
WorldSpeed = Literal[1, 5, 10]


class WorldSpeedRequest(StrictModel):
    speed: WorldSpeed


class WorldStatus(BaseModel):
    route_id: str | None  # None until the runner has published a frame
    sim_time: str | None
    speed: WorldSpeed | None  # the speed requested by officers (None: runner default)
    vehicles: int
    stale_vehicles: int
    open_events: int


# --- simulation lab (B5) ---
class ScenarioCreate(StrictModel):
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=5, max_length=500)
    ]


class ScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    route_id: str
    source: ScenarioSource
    golden_key: str | None
    description: str
    status: ScenarioStatus
    seed: int
    plan: ScenarioPlan | None
    preview: Preview | None  # simulator: this scenario vs a normal day (rule 2)
    attempts: int
    problems: list[str] | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class ScenarioList(BaseModel):
    scenarios: list[ScenarioOut]


class WorldScenarioOut(BaseModel):
    scenario_id: uuid.UUID | None  # None: a normal day
    name: str | None


# --- approval, dispatch and driver feedback (B12) ---
MAX_HOLD_SECONDS = 180  # rulebook R1 (docs/rulebook.md)


class ActionParams(BaseModel):
    seconds: int | None = None  # hold
    stop_ids: list[str] | None = None  # skip_stop
    at_stop_id: str | None = None  # short_turn


class RankedOut(BaseModel):
    candidate_key: str
    benefit_passenger_minutes: float
    benefit_low: float
    benefit_high: float
    bad_case_benefit: float
    passenger_wait_minutes: float
    bunching_count: float
    rejected_reason: str | None = None


class RecommendationOut(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    run_status: WorkflowStatus
    round: int
    action_type: ActionType
    vehicle_id: uuid.UUID | None
    fleet_number: str | None
    params: ActionParams
    benefit_passenger_minutes: float
    explanation: str
    is_near_tie: bool
    ranking: list[RankedOut]
    created_at: datetime
    expires_at: datetime | None


class RecommendationsResponse(BaseModel):
    recommendations: list[RecommendationOut]


class DecisionRequest(StrictModel):
    decision: Literal["approve", "edit", "reject"]
    seconds: Annotated[int, Field(ge=1, le=MAX_HOLD_SECONDS)] | None = None  # edit: hold length
    reason: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=300)]
        | None
    ) = None


class DecisionResponse(BaseModel):
    status: Literal["accepted", "already_decided"]
    approval_id: uuid.UUID
    run_id: uuid.UUID


class InstructionOut(BaseModel):
    id: uuid.UUID
    instruction_id: str
    vehicle_id: uuid.UUID
    action_type: ActionType
    params: ActionParams
    status: InstructionStatus
    sent_at: datetime
    acknowledged_at: datetime | None
    done_at: datetime | None
    cancel_reason: str | None


class CurrentInstructionResponse(BaseModel):
    instruction: InstructionOut | None


class CantComplyRequest(StrictModel):
    reason: CantComplyReason
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] | None = None
