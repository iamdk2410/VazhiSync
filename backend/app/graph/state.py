"""WorkflowState and the models that travel through the graph (rule 6: validated Pydantic).

Everything an agent sees came from an MCP tool (the snapshot) or from the simulator run on
those observations; never from the world simulator itself (rule 1).
"""

import operator
import uuid
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from mcp_servers.schemas import BoardingEstimates, HeadwayPair, Headways, VehiclePositions

ActionKind = Literal["hold", "skip_stop", "short_turn", "do_nothing"]
EndReason = Literal[
    "stale_data",  # the pair's GPS is stale: flagged, nobody is instructed (rule 4)
    "not_bunched",  # the pair recovered (or isn't adjacent any more)
    "recommended",  # a ranked recommendation awaits the officer
    "escalated",  # near-tie or no usable baseline: the officer decides
    "do_nothing_recommended",  # every action was worse than waiting
    "rejected",  # the officer rejected every round (B12)
    "replans_exhausted",  # expired / can't comply / invalid at send too many times (B12)
    "outcome_measured",  # dispatched, carried out and measured (B12)
]
MAX_ROUNDS = 2  # regeneration rounds after the first recommendation (reject, re-plan)


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Snapshot(_Model):
    """What the Headway Monitor observed, via avl-mcp and demand-mcp only."""

    world_time_s: float
    positions: VehiclePositions
    headways: Headways
    estimates: BoardingEstimates | None  # None: demand data unavailable (degraded)
    pair: HeadwayPair


class Candidate(_Model):
    key: str  # stable id within the run, e.g. "hold:32B-04:120"
    action_type: ActionKind
    vehicle_id: uuid.UUID | None  # None for do_nothing
    fleet_number: str | None
    params: dict[str, Any]  # canonical params: {"seconds"} / {"stop_ids"} / {"at_stop_id"}
    rationale: str
    db_id: uuid.UUID | None = None  # candidate_actions row
    round: int = 0  # regeneration round it belongs to (B12)


class DroppedCandidate(_Model):
    key: str
    reasons: list[str]  # rulebook / cooldown / past-outcome reasons


class SimOutcome(_Model):
    """One candidate simulated over several seeds (common random numbers across candidates)."""

    candidate_key: str
    status: Literal["completed", "failed", "timed_out"]
    seeds: list[int]
    passenger_wait_minutes: float | None = None  # mean over seeds
    per_seed_wait: list[float] = []
    bunching_count: float | None = None
    left_behind: float | None = None
    overcarried: float | None = None
    displaced: float | None = None
    headway_cv: float | None = None
    error: str | None = None
    duration_ms: int = 0
    round: int = 0


class RankedCandidate(_Model):
    candidate_key: str
    benefit_passenger_minutes: float  # mean vs do nothing (positive = better), from the simulator
    benefit_low: float  # 95 % confidence range of the mean benefit across seeds
    benefit_high: float
    bad_case_benefit: float  # 10th percentile of the per-seed benefits (the "safest" measure)
    passenger_wait_minutes: float
    bunching_count: float
    rejected_reason: str | None = None


class RecommendationResult(_Model):
    candidate_key: str  # "do_nothing" when nothing beats waiting
    benefit_passenger_minutes: float
    ranking: list[RankedCandidate]
    explanation: str
    is_near_tie: bool
    end_reason: EndReason
    safest_candidate_key: str | None = None  # best bad case, when it differs from the winner
    recommendation_id: uuid.UUID | None = None  # recommendations row (what the officer decides on)


class WorkflowState(TypedDict, total=False):
    run_id: str
    event_id: str
    route_id: str
    leader_vehicle_id: str
    follower_vehicle_id: str
    snapshot: Snapshot
    candidates: list[Candidate]
    dropped: list[DroppedCandidate]
    sim_outcomes: Annotated[list[SimOutcome], operator.add]  # one per Send branch
    recommendation: RecommendationResult
    degraded: Annotated[list[str], operator.add]  # tools the run did without
    end_reason: EndReason
    route: str  # the last routing decision (also stored in agent_steps)
    candidate: Candidate  # payload of a simulate branch (Send)
    round: int  # 0 for the first recommendation, +1 per reject / edit / re-plan (B12)
    decision: dict[str, Any]  # the officer's decision (resume value of await_officer)
    rejected_keys: Annotated[list[str], operator.add]  # candidates the officer rejected
    instruction: dict[str, Any]  # the dispatched instruction (InstructionSent as JSON)
    driver_reply: dict[str, Any]  # resume value of await_driver
    acked_world_time_s: float  # the world's clock when the driver acknowledged


# ------------------------------------------------------------------ resume values (B12)


class OfficerDecision(_Model):
    """What resumes `await_officer`. `expired`: the recommendation went stale before a decision."""

    decision: Literal["approve", "edit", "reject", "expired"]
    approval_id: uuid.UUID | None = None
    officer_id: uuid.UUID | None = None
    params: dict[str, Any] | None = None  # edit: the officer's parameters (e.g. {"seconds": 90})
    reason: str | None = None  # reject: required


class DriverReply(_Model):
    """What resumes `await_driver`."""

    kind: Literal["ack", "cant_comply", "timeout"]
    instruction_id: str
    world_time_s: float | None = None  # ack: the world's clock when acknowledged
    reason: str | None = None  # cant_comply: blocking_traffic | bus_full | other


class OutcomeDue(_Model):
    """What resumes `await_outcome` once ~15 world-minutes have passed since the ack."""

    kind: Literal["measure"] = "measure"
