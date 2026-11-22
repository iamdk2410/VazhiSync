"""All ORM models. Importing this package registers every table on `Base.metadata`."""

from app.db.models.fleet import DriverAssignment, StopQueue, Vehicle, VehiclePosition
from app.db.models.notifications import Notification
from app.db.models.telemetry import AgentStep, LlmUsage
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
    "DriverAssignment",
    "DriverInstruction",
    "FavouriteStop",
    "InterventionOutcome",
    "LlmUsage",
    "Notification",
    "Recommendation",
    "SimulationJob",
    "StopQueue",
    "User",
    "Vehicle",
    "VehiclePosition",
    "WorkflowRun",
]
