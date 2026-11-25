"""The officer's view of one workflow run: the AG-UI shared state (STATE_SNAPSHOT / STATE_DELTA).

Built only from the workflow's own state, which itself comes from MCP tool results and the
simulator (rule 1): no world-simulator internals, no raw tool payloads. `fold` is pure, so the
same projection serves the live stream, reconnects and tests.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.graph.state import (
    Candidate,
    DroppedCandidate,
    RecommendationResult,
    SimOutcome,
    Snapshot,
)

StepStatus = Literal["running", "done", "error", "paused"]
RunStatus = Literal["running", "paused", "finished", "failed"]


class StepView(BaseModel):
    id: str  # LangGraph task id (or the agent_steps index when rebuilt from the database)
    name: str  # node name; simulate branches are "simulate:<candidate key>"
    status: StepStatus
    routing: str | None = None


class PairView(BaseModel):
    direction_id: int
    leader: str
    follower: str
    headway_s: float
    scheduled_headway_s: float
    ratio: float
    status: str


class CandidateView(BaseModel):
    key: str
    action_type: str
    fleet_number: str | None
    params: dict[str, Any]
    source: str  # rule | llm | officer_edit
    rationale: str
    round: int = 0


class SimulationView(BaseModel):
    status: str  # completed | failed | timed_out
    passenger_wait_minutes: float | None = None
    bunching_count: float | None = None
    error: str | None = None


class RecommendationView(BaseModel):
    recommendation_id: str | None
    candidate_key: str
    benefit_passenger_minutes: float
    ranking: list[dict[str, Any]]
    explanation: str
    explanation_source: str = "rules"
    near_tie: bool
    safest_candidate_key: str | None = None
    end_reason: str
    expires_at: str | None = None
    similar_cases: list[dict[str, Any]] = Field(default_factory=list)  # B22
    confidence: str = "normal"
    confidence_note: str | None = None


class RunView(BaseModel):
    run_id: str
    status: RunStatus = "running"
    waiting_for: str | None = None
    end_reason: str | None = None
    round: int = 0
    event_kind: str = "actual"  # predicted: started by early warning (B21)
    degraded: list[str] = Field(default_factory=list)
    pair: PairView | None = None
    steps: list[StepView] = Field(default_factory=list)
    candidates: list[CandidateView] = Field(default_factory=list)
    dropped: list[DroppedCandidate] = Field(default_factory=list)
    simulations: dict[str, SimulationView] = Field(default_factory=dict)
    recommendation: RecommendationView | None = None
    decision: dict[str, Any] | None = None
    instruction: dict[str, Any] | None = None
    driver_reply: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None

    def doc(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def pair_view(snapshot: Snapshot) -> PairView:
    p = snapshot.pair
    return PairView(
        direction_id=p.direction_id,
        leader=p.leader_fleet_number,
        follower=p.follower_fleet_number,
        headway_s=round(p.headway_seconds, 1),
        scheduled_headway_s=round(p.scheduled_headway_seconds, 1),
        ratio=round(p.ratio, 3),
        status=p.status,
    )


def candidate_view(c: Candidate) -> CandidateView:
    source = "officer_edit" if c.key.startswith("edit:") else c.source
    return CandidateView(
        key=c.key,
        action_type=c.action_type,
        fleet_number=c.fleet_number,
        params=c.params,
        source=source,
        rationale=c.rationale,
        round=c.round,
    )


def recommendation_view(r: RecommendationResult) -> RecommendationView:
    return RecommendationView(
        recommendation_id=str(r.recommendation_id) if r.recommendation_id else None,
        candidate_key=r.candidate_key,
        benefit_passenger_minutes=r.benefit_passenger_minutes,
        ranking=[x.model_dump(mode="json") for x in r.ranking],
        explanation=r.explanation,
        explanation_source=r.explanation_source,
        near_tie=r.is_near_tie,
        safest_candidate_key=r.safest_candidate_key,
        end_reason=r.end_reason,
        expires_at=r.expires_at.isoformat() if r.expires_at else None,
        similar_cases=r.similar_cases,
        confidence=r.confidence,
        confidence_note=r.confidence_note,
    )


def simulation_view(o: SimOutcome) -> SimulationView:
    return SimulationView(
        status=o.status,
        passenger_wait_minutes=o.passenger_wait_minutes,
        bunching_count=o.bunching_count,
        error=o.error,
    )


def fold(view: RunView, node: str, update: dict[str, Any] | None) -> RunView:
    """The view after one node's state update (pure)."""
    if not update:
        return view
    v = view.model_copy(deep=True)
    if "round" in update and update["round"] != v.round:
        v.round = int(update["round"])
        v.simulations, v.recommendation = {}, None  # a new round is planned from scratch
    if update.get("event_kind"):
        v.event_kind = str(update["event_kind"])
    if (snapshot := update.get("snapshot")) is not None:
        v.pair = pair_view(snapshot)
    if "candidates" in update:
        v.candidates = [candidate_view(c) for c in update["candidates"]]
        v.simulations = {}
    if "dropped" in update:
        v.dropped = list(update["dropped"])
    for outcome in update.get("sim_outcomes", []):
        v.simulations[outcome.candidate_key] = simulation_view(outcome)
    if (rec := update.get("recommendation")) is not None:
        v.recommendation = recommendation_view(rec)
    for tool in update.get("degraded", []):
        if tool not in v.degraded:
            v.degraded.append(tool)
    for key in ("decision", "instruction", "driver_reply", "outcome"):
        if update.get(key) is not None:
            setattr(v, key, update[key])
    if update.get("end_reason") is not None:
        v.end_reason = update["end_reason"]
    return v


def step_name(task_name: str, task_input: Any) -> str:
    """'simulate:<candidate key>' for the parallel branches, else the node name."""
    if task_name == "simulate" and isinstance(task_input, dict):
        candidate = task_input.get("candidate")
        key = getattr(candidate, "key", None)
        if key:
            return f"simulate:{key}"
    return task_name
