"""Action Generator: 2-4 concrete candidates plus "do nothing".

For a bunched pair (leader ahead, follower too close behind):
- hold the follower at its next stop long enough to restore the gap (and a half-length hold);
- let the leader skip its quietest upcoming stops (needs demand data; skipped if unavailable);
- short-turn the follower when the opposite direction has a large gap to fill.
An LLM (through the gateway, B9) may add up to 3 ideas the rules missed, chosen only from the
stops the rules allow; they become the same canonical candidates and are checked exactly like
the rule ones. If every model fails or the token budget is spent, the rules' candidates are
used alone. Candidates that break the rulebook, or whose action type has done badly before,
are dropped with reasons. Uses only the snapshot and demand-mcp (its allowlist).
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.agents.rulebook import MAX_HOLD_S, RecentInstruction, RuleContext, violations
from app.db.enums import ActionType, CandidateSource, InstructionStatus
from app.db.models import CandidateAction, DriverInstruction, InterventionOutcome
from app.graph.deps import WorkflowDeps
from app.graph.state import Candidate, DroppedCandidate, Snapshot, WorkflowState
from app.harness.cases import SimilarCase, raw_features, similar_cases, vectorize
from app.harness.context import PastOutcome, StopOption, action_generator_messages
from mcp_servers.schemas import BoardingEstimates, VehiclePosition
from simulator import RouteModel

MAX_ACTIONS = 4  # plus do_nothing
HOLD_STEP_S = 30
SKIP_LOOKAHEAD_STOPS = 6
SHORT_TURN_GAP_RATIO = 1.5  # opposite direction needs a gap this big (x scheduled) to fill
MIN_PAST_OUTCOMES = 3
MAX_LLM_IDEAS = 3
MAX_TURN_OPTIONS = 3
AGENT = "action_generator"


class LlmIdea(BaseModel):
    action_type: Literal["hold", "skip_stop", "short_turn"]
    hold_seconds: int | None = Field(default=None, ge=HOLD_STEP_S, le=MAX_HOLD_S)
    stop_ids: list[str] | None = Field(default=None, min_length=1, max_length=2)
    at_stop_id: str | None = None
    why: str = Field(min_length=3, max_length=300)


class LlmIdeas(BaseModel):
    ideas: list[LlmIdea] = Field(default_factory=list, max_length=MAX_LLM_IDEAS)


def _vehicle(snapshot: Snapshot, vehicle_id: str) -> VehiclePosition | None:
    return next((v for v in snapshot.positions.vehicles if str(v.vehicle_id) == vehicle_id), None)


def _round_hold(seconds: float) -> int:
    return int(min(MAX_HOLD_S, max(HOLD_STEP_S, round(seconds / HOLD_STEP_S) * HOLD_STEP_S)))


def skip_options(
    snapshot: Snapshot, route: RouteModel, estimates: BoardingEstimates | None
) -> list[StopOption]:
    """Upcoming stops of the leader with fresh demand data, in route order."""
    pair = snapshot.pair
    leader = _vehicle(snapshot, str(pair.leader_vehicle_id))
    if leader is None or leader.distance_m is None or estimates is None:
        return []
    d = pair.direction_id
    direction = route.direction(d)
    here = direction.stop_index_at(leader.distance_m)
    ahead = {
        s.stop_id: s
        for s in estimates.stops
        if s.direction_id == d and s.data_quality == "ok" and s.expected_boardings is not None
    }
    out: list[StopOption] = []
    for i in range(here + 1, min(here + 1 + SKIP_LOOKAHEAD_STOPS, direction.n_stops - 1)):
        est = ahead.get(direction.stops[i].stop_id)
        if est is not None:
            out.append(
                StopOption(
                    stop_id=est.stop_id,
                    name=est.name,
                    expected_boardings=round(est.expected_boardings or 0.0, 1),
                )
            )
    return out


def turn_options(snapshot: Snapshot, route: RouteModel) -> list[StopOption]:
    """Stops ahead of the follower where it could turn into the other direction."""
    pair = snapshot.pair
    follower = _vehicle(snapshot, str(pair.follower_vehicle_id))
    if follower is None or follower.distance_m is None:
        return []
    d = pair.direction_id
    direction = route.direction(d)
    opposite = route.direction(1 - d)
    here = direction.stop_index_at(follower.distance_m)
    out: list[StopOption] = []
    for i in range(here + 1, direction.n_stops - 1):
        turn = route.turn_pairs[d][i]
        if turn is not None and turn < opposite.n_stops - 1:
            stop = direction.stops[i]
            out.append(StopOption(stop_id=stop.stop_id, name=stop.name))
            if len(out) == MAX_TURN_OPTIONS:
                break
    return out


def next_stop_name(route: RouteModel, v: VehiclePosition | None) -> str | None:
    if v is None or v.distance_m is None:
        return None
    direction = route.direction(v.direction_id)
    i = min(direction.stop_index_at(v.distance_m) + 1, direction.n_stops - 1)
    return direction.stops[i].name


def idea_problems(
    ideas: LlmIdeas, skippable: list[StopOption], turn_stops: list[StopOption]
) -> list[str]:
    """Why an LLM answer can't be used as it is (fed back once for repair)."""
    skip_ids = sorted(s.stop_id for s in skippable)
    turn_ids = sorted(s.stop_id for s in turn_stops)
    out: list[str] = []
    for i, idea in enumerate(ideas.ideas):
        where = f"ideas.{i}"
        if idea.action_type == "hold":
            if idea.hold_seconds is None:
                out.append(f"{where}.hold_seconds: required for a hold")
            elif idea.hold_seconds % HOLD_STEP_S:
                out.append(f"{where}.hold_seconds: use a multiple of {HOLD_STEP_S}")
        if idea.action_type == "skip_stop":
            unknown = [s for s in idea.stop_ids or [] if s not in skip_ids]
            if not idea.stop_ids or unknown:
                out.append(f"{where}.stop_ids: choose from {skip_ids or 'none (no demand data)'}")
        if idea.action_type == "short_turn" and idea.at_stop_id not in turn_ids:
            out.append(f"{where}.at_stop_id: choose from {turn_ids or 'none'}")
    return out


def ideas_to_candidates(
    ideas: LlmIdeas, snapshot: Snapshot, skippable: list[StopOption], turn_stops: list[StopOption]
) -> list[Candidate]:
    """Canonical candidates (same keys and params as the rules') from checked LLM ideas."""
    pair = snapshot.pair
    leader = _vehicle(snapshot, str(pair.leader_vehicle_id))
    follower = _vehicle(snapshot, str(pair.follower_vehicle_id))
    names = {s.stop_id: s.name for s in [*skippable, *turn_stops]}
    order = [s.stop_id for s in skippable]
    out: list[Candidate] = []
    for idea in ideas.ideas:
        why = idea.why.strip()
        if idea.action_type == "hold" and follower is not None and idea.hold_seconds:
            secs = _round_hold(idea.hold_seconds)
            out.append(
                Candidate(
                    key=f"hold:{follower.fleet_number}:{secs}",
                    action_type="hold",
                    vehicle_id=follower.vehicle_id,
                    fleet_number=follower.fleet_number,
                    params={"seconds": secs},
                    rationale=f"Hold {follower.fleet_number} {secs} s. {why}",
                    source="llm",
                )
            )
        elif idea.action_type == "skip_stop" and leader is not None and idea.stop_ids:
            stops = sorted(set(idea.stop_ids), key=order.index)
            skipped = ", ".join(names[s] for s in stops)
            out.append(
                Candidate(
                    key=f"skip:{leader.fleet_number}:{'+'.join(stops)}",
                    action_type="skip_stop",
                    vehicle_id=leader.vehicle_id,
                    fleet_number=leader.fleet_number,
                    params={"stop_ids": stops},
                    rationale=f"{leader.fleet_number} skips {skipped}. {why}",
                    source="llm",
                )
            )
        elif idea.action_type == "short_turn" and follower is not None and idea.at_stop_id:
            at = names[idea.at_stop_id]
            out.append(
                Candidate(
                    key=f"short_turn:{follower.fleet_number}:{idea.at_stop_id}",
                    action_type="short_turn",
                    vehicle_id=follower.vehicle_id,
                    fleet_number=follower.fleet_number,
                    params={"at_stop_id": idea.at_stop_id},
                    rationale=f"Short-turn {follower.fleet_number} at {at}. {why}",
                    source="llm",
                )
            )
    return out


def with_case_holds(
    candidates: list[Candidate], snapshot: Snapshot, cases: list[SimilarCase]
) -> list[Candidate]:
    """B22: a hold that worked in a similar past case (measured: it saved passenger time) is
    offered again, if not already there. Only holds: a past skip or short-turn names stops
    that may not lie ahead now. Checked and simulated like every other candidate."""
    follower = _vehicle(snapshot, str(snapshot.pair.follower_vehicle_id))
    if follower is None:
        return candidates
    keys = {c.key for c in candidates}
    extra: list[Candidate] = []
    for case in cases:
        actual = case.actual_benefit_passenger_minutes
        if case.action_type != "hold" or actual is None or actual <= 0:
            continue
        seconds = _round_hold(float(case.params.get("seconds", 0)))
        key = f"hold:{follower.fleet_number}:{seconds}"
        if key in keys:
            continue
        keys.add(key)
        extra.append(
            Candidate(
                key=key,
                action_type="hold",
                vehicle_id=follower.vehicle_id,
                fleet_number=follower.fleet_number,
                params={"seconds": seconds},
                rationale=(
                    f"Hold {follower.fleet_number} {seconds} s: in a similar case on "
                    f"{case.decided_at[:10]} a {seconds} s hold saved {actual:.0f} "
                    "passenger-minutes."
                ),
            )
        )
    if not extra:
        return candidates
    actions = [c for c in candidates if c.action_type != "do_nothing"]
    nothing = [c for c in candidates if c.action_type == "do_nothing"]
    return [*(actions[:2] + extra + actions[2:])[:MAX_ACTIONS], *nothing]


def merge(rule: list[Candidate], llm: list[Candidate]) -> list[Candidate]:
    """The rules' first two, then new LLM ideas, then the remaining rule options; at most
    MAX_ACTIONS actions plus do nothing (every candidate costs simulations)."""
    actions = [c for c in rule if c.action_type != "do_nothing"]
    nothing = [c for c in rule if c.action_type == "do_nothing"]
    seen = {c.key for c in actions}
    fresh: list[Candidate] = []
    for c in llm:
        if c.key not in seen:
            seen.add(c.key)
            fresh.append(c)
    return [*(actions[:2] + fresh + actions[2:])[:MAX_ACTIONS], *nothing]


def propose(
    snapshot: Snapshot, route: RouteModel, estimates: BoardingEstimates | None
) -> list[Candidate]:
    pair = snapshot.pair
    d = pair.direction_id
    direction = route.direction(d)
    leader = _vehicle(snapshot, str(pair.leader_vehicle_id))
    follower = _vehicle(snapshot, str(pair.follower_vehicle_id))
    actions: list[Candidate] = []

    if follower is not None:
        deficit = pair.scheduled_headway_seconds - pair.headway_seconds
        full = _round_hold(deficit)
        actions.append(
            Candidate(
                key=f"hold:{follower.fleet_number}:{full}",
                action_type="hold",
                vehicle_id=follower.vehicle_id,
                fleet_number=follower.fleet_number,
                params={"seconds": full},
                rationale=(
                    f"Hold {follower.fleet_number} {full} s at its next stop to open the gap to "
                    f"{pair.leader_fleet_number} ({pair.headway_seconds:.0f} s vs "
                    f"{pair.scheduled_headway_seconds:.0f} s scheduled)."
                ),
            )
        )

    skippable = skip_options(snapshot, route, estimates)
    if leader is not None and skippable:
        order = [o.stop_id for o in skippable]
        quiet = sorted(sorted(skippable, key=lambda o: o.expected_boardings or 0.0)[:2],
                       key=lambda o: order.index(o.stop_id))  # fmt: skip
        stops = [o.stop_id for o in quiet]
        names = ", ".join(o.name for o in quiet)
        actions.append(
            Candidate(
                key=f"skip:{leader.fleet_number}:{'+'.join(stops)}",
                action_type="skip_stop",
                vehicle_id=leader.vehicle_id,
                fleet_number=leader.fleet_number,
                params={"stop_ids": stops},
                rationale=(
                    f"{leader.fleet_number} skips {names} (fewest expected boardings) to "
                    f"pull ahead of {pair.follower_fleet_number}."
                ),
            )
        )

    if follower is not None and follower.distance_m is not None:
        other = 1 - d
        gaps = [
            p for p in snapshot.headways.pairs if p.direction_id == other and p.status != "stale"
        ]
        if gaps and max(p.ratio for p in gaps) >= SHORT_TURN_GAP_RATIO:
            here = direction.stop_index_at(follower.distance_m)
            opposite = route.direction(other)
            for i in range(here + 1, direction.n_stops - 1):
                turn = route.turn_pairs[d][i]
                if turn is not None and turn < opposite.n_stops - 1:
                    stop = direction.stops[i]
                    widest = max(p.ratio for p in gaps)
                    actions.append(
                        Candidate(
                            key=f"short_turn:{follower.fleet_number}:{stop.stop_id}",
                            action_type="short_turn",
                            vehicle_id=follower.vehicle_id,
                            fleet_number=follower.fleet_number,
                            params={"at_stop_id": stop.stop_id},
                            rationale=(
                                f"Short-turn {follower.fleet_number} at {stop.name} to fill a "
                                f"{widest:.1f}x gap in the other direction."
                            ),
                        )
                    )
                    break

    if follower is not None and len(actions) < MAX_ACTIONS:
        full = (
            int(actions[0].params["seconds"]) if actions and actions[0].action_type == "hold" else 0
        )
        half = _round_hold(full / 2) if full else 0
        if half and half != full:
            actions.append(
                Candidate(
                    key=f"hold:{follower.fleet_number}:{half}",
                    action_type="hold",
                    vehicle_id=follower.vehicle_id,
                    fleet_number=follower.fleet_number,
                    params={"seconds": half},
                    rationale=f"A shorter {half} s hold for {follower.fleet_number}.",
                )
            )

    do_nothing = Candidate(
        key="do_nothing",
        action_type="do_nothing",
        vehicle_id=None,
        fleet_number=None,
        params={},
        rationale="Let the buses run; the baseline every action is compared with.",
    )
    return [*actions[:MAX_ACTIONS], do_nothing]


async def _rule_context(
    deps: WorkflowDeps, snapshot: Snapshot, vehicle_ids: list[str]
) -> RuleContext:
    route = deps.route.model
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    DriverInstruction.vehicle_id,
                    DriverInstruction.action_type,
                    DriverInstruction.sent_at,
                    DriverInstruction.status,
                ).where(DriverInstruction.vehicle_id.in_(vehicle_ids))
            )
        ).all()
    active = {InstructionStatus.SENT, InstructionStatus.ACKNOWLEDGED}
    return RuleContext(
        route_id=route.route_id,
        world_time_s=snapshot.world_time_s,
        now=datetime.now(UTC),
        stop_names={s.stop_id: s.name for d in route.directions for s in d.stops},
        last_departure_s=deps.last_departure_s,
        headway_s={d.direction_id: float(d.scheduled_headway_s) for d in route.directions},
        recent=[
            RecentInstruction(str(v), a.value, sent, status in active)
            for v, a, sent, status in rows
        ],
    )


async def _past_outcomes(deps: WorkflowDeps) -> list[PastOutcome]:
    """What measured interventions (B12) did to waits, per action type."""
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    InterventionOutcome.action_type,
                    func.avg(
                        InterventionOutcome.avg_wait_minutes_before
                        - InterventionOutcome.avg_wait_minutes_after
                    ),
                    func.count(),
                ).group_by(InterventionOutcome.action_type)
            )
        ).all()
    return [
        PastOutcome(action_type=a.value, interventions=n, avg_wait_change_minutes=round(gain, 2))
        for a, gain, n in rows
        if gain is not None
    ]


def poor_action_types(past: list[PastOutcome]) -> set[str]:
    """Action types that made waits worse on average over enough interventions."""
    return {
        p.action_type
        for p in past
        if p.interventions >= MIN_PAST_OUTCOMES and p.avg_wait_change_minutes < 0
    }


async def llm_ideas(
    state: WorkflowState,
    deps: WorkflowDeps,
    snapshot: Snapshot,
    rule_candidates: list[Candidate],
    past: list[PastOutcome],
    similar: list[dict[str, Any]] | None = None,
) -> list[Candidate]:
    """Up to 3 extra candidates from the LLM gateway; [] when it's off or falls back."""
    if deps.llm is None:
        return []
    route = deps.route.model
    skippable = skip_options(snapshot, route, snapshot.estimates)
    turn_stops = turn_options(snapshot, route)
    leader = _vehicle(snapshot, str(snapshot.pair.leader_vehicle_id))
    follower = _vehicle(snapshot, str(snapshot.pair.follower_vehicle_id))
    messages = action_generator_messages(
        pair=snapshot.pair,
        leader=leader,
        follower=follower,
        leader_next_stop=next_stop_name(route, leader),
        follower_next_stop=next_stop_name(route, follower),
        skippable=skippable,
        turn_stops=turn_stops,
        existing=[c.key for c in rule_candidates],
        rejected=state.get("rejected_keys", []),
        past=past,
        max_hold_s=MAX_HOLD_S,
        similar=similar,
    )
    result = await deps.llm.complete(
        agent=AGENT,
        messages=messages,
        output=LlmIdeas,
        rules=LlmIdeas,  # the rules' candidates are already there: no extra ideas
        run_id=deps.run_id,
        budget=deps.budget,
        check=lambda ideas: idea_problems(ideas, skippable, turn_stops),
    )
    return ideas_to_candidates(result.value, snapshot, skippable, turn_stops)


async def action_generator(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    snapshot = state["snapshot"]
    optional = await deps.optional_tool(
        "action_generator",
        "get_boarding_estimates",
        {
            "route_id": state["route_id"],
            "direction_id": snapshot.pair.direction_id,
            "horizon_minutes": int(deps.horizon_min),
        },
    )
    estimates = optional.value if isinstance(optional.value, BoardingEstimates) else None
    snapshot = snapshot.model_copy(update={"estimates": estimates})
    round_ = state.get("round", 0)
    rejected = set(state.get("rejected_keys", []))
    past = await _past_outcomes(deps)
    raw = raw_features(snapshot, deps.route)
    async with deps.sessionmaker() as session:
        cases = await similar_cases(session, state["route_id"], vectorize(raw))
    similar = [c.model_dump(mode="json") for c in cases]
    rule_candidates = propose(snapshot, deps.route.model, estimates)
    rule_candidates = with_case_holds(rule_candidates, snapshot, cases)
    ideas = await llm_ideas(state, deps, snapshot, rule_candidates, past, similar)
    proposed = [c.model_copy(update={"round": round_}) for c in merge(rule_candidates, ideas)]

    ids = [str(snapshot.pair.leader_vehicle_id), str(snapshot.pair.follower_vehicle_id)]
    ctx = await _rule_context(deps, snapshot, ids)
    poor = poor_action_types(past)
    kept: list[Candidate] = []
    dropped: list[DroppedCandidate] = []
    for c in proposed:
        reasons = violations(c, ctx, snapshot.pair.direction_id)
        if c.key in rejected:
            reasons.append("rejected by the officer in an earlier round")
        if c.action_type in poor:
            reasons.append(f"past outcomes: {c.action_type} has made waits worse here")
        if reasons:
            dropped.append(DroppedCandidate(key=c.key, reasons=reasons))
        else:
            kept.append(c)

    async with deps.sessionmaker() as session:
        rows = [
            CandidateAction(
                run_id=deps.run_id,
                action_type=ActionType(c.action_type),
                vehicle_id=c.vehicle_id,
                params=c.params,
                source=CandidateSource.LLM if c.source == "llm" else CandidateSource.RULE,
                rationale=c.rationale,
                regeneration_round=round_,
            )
            for c in kept
        ]
        session.add_all(rows)
        await session.commit()
    kept = [c.model_copy(update={"db_id": row.id}) for c, row in zip(kept, rows, strict=True)]
    out: dict[str, Any] = {
        "snapshot": snapshot,
        "candidates": kept,
        "dropped": dropped,
        "case_features": raw,
        "similar_cases": similar,
    }
    if optional.degraded is not None:
        out["degraded"] = [optional.degraded.tool]
    return out
