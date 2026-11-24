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
]


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


class RankedCandidate(_Model):
    candidate_key: str
    benefit_passenger_minutes: float  # vs do nothing (positive = better), from the simulator
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
