"""All ORM models. Importing this package registers every table on `Base.metadata`."""

from app.db.models.cases import Case
from app.db.models.fleet import DriverAssignment, StopQueue, Vehicle, VehiclePosition
from app.db.models.notifications import Notification
from app.db.models.routes import Route, RouteDirection, RouteDirectionStop, Stop
from app.db.models.scenarios import ScenarioRecord
from app.db.models.telemetry import AgentStep, LlmUsage, ToolCallRecord
from app.db.models.users import FavouriteStop, User
from app.db.models.workflow import (
    Approval,
    BunchingEvent,
    CandidateAction,
    DriverInstruction,
    InterventionOutcome,
    Recommendation,
    SimulationJob,
    WorkflowRun,
)

__all__ = [
    "AgentStep",
    "Approval",
    "BunchingEvent",
    "CandidateAction",
    "Case",
    "DriverAssignment",
    "DriverInstruction",
    "FavouriteStop",
    "InterventionOutcome",
    "LlmUsage",
    "Notification",
    "Recommendation",
    "Route",
    "RouteDirection",
    "RouteDirectionStop",
    "ScenarioRecord",
    "SimulationJob",
    "Stop",
    "StopQueue",
    "ToolCallRecord",
    "User",
    "Vehicle",
    "VehiclePosition",
    "WorkflowRun",
]
