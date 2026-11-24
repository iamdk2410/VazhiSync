"""Enumerations shared by the database models and the API schemas."""

from enum import StrEnum


class Role(StrEnum):
    OFFICER = "officer"
    DRIVER = "driver"
    PASSENGER = "passenger"


class ActionType(StrEnum):
    HOLD = "hold"
    SKIP_STOP = "skip_stop"
    SHORT_TURN = "short_turn"
    DO_NOTHING = "do_nothing"


class BunchingSeverity(StrEnum):
    CLOSING = "closing"  # headway below CLOSING_RATIO x scheduled
    BUNCHED = "bunched"  # headway below BUNCHING_RATIO x scheduled


class BunchingEventStatus(StrEnum):
    OPEN = "open"
    HANDLING = "handling"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class WorkflowStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class CandidateSource(StrEnum):
    RULE = "rule"
    LLM = "llm"
    OFFICER_EDIT = "officer_edit"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    EDITED = "edited"
    REJECTED = "rejected"


class InstructionStatus(StrEnum):
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    DONE = "done"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CANT_COMPLY = "cant_comply"  # the driver replied they can't (B12): reason required


class CantComplyReason(StrEnum):
    BLOCKING_TRAFFIC = "blocking_traffic"
    BUS_FULL = "bus_full"
    OTHER = "other"


class NotificationKind(StrEnum):
    SKIP_STOP = "skip_stop"
    SHORT_TURN = "short_turn"
    ARRIVAL = "arrival"
    SERVICE = "service"


class StepStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"


class ScenarioSource(StrEnum):
    GOLDEN = "golden"
    GENERATED = "generated"


class ScenarioStatus(StrEnum):
    PENDING = "pending"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
