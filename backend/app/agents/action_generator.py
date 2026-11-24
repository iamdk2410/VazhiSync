"""Action Generator: 2-4 concrete candidates plus "do nothing" (rules only; B9 adds LLM ideas).

For a bunched pair (leader ahead, follower too close behind):
- hold the follower at its next stop long enough to restore the gap (and a half-length hold);
- let the leader skip its quietest upcoming stops (needs demand data; skipped if unavailable);
- short-turn the follower when the opposite direction has a large gap to fill.
Candidates that break the rulebook, or whose action type has done badly before, are dropped
with reasons. Uses only the snapshot and demand-mcp (its allowlist).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.agents.rulebook import MAX_HOLD_S, RecentInstruction, RuleContext, violations
from app.db.enums import ActionType, CandidateSource, InstructionStatus
from app.db.models import CandidateAction, DriverInstruction, InterventionOutcome
from app.graph.deps import WorkflowDeps
from app.graph.state import Candidate, DroppedCandidate, Snapshot, WorkflowState
from mcp_servers.schemas import BoardingEstimates, VehiclePosition
from simulator import RouteModel

MAX_ACTIONS = 4  # plus do_nothing
HOLD_STEP_S = 30
SKIP_LOOKAHEAD_STOPS = 6
SHORT_TURN_GAP_RATIO = 1.5  # opposite direction needs a gap this big (x scheduled) to fill
MIN_PAST_OUTCOMES = 3


def _vehicle(snapshot: Snapshot, vehicle_id: str) -> VehiclePosition | None:
    return next((v for v in snapshot.positions.vehicles if str(v.vehicle_id) == vehicle_id), None)


def _round_hold(seconds: float) -> int:
    return int(min(MAX_HOLD_S, max(HOLD_STEP_S, round(seconds / HOLD_STEP_S) * HOLD_STEP_S)))


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

    if leader is not None and leader.distance_m is not None and estimates is not None:
        here = direction.stop_index_at(leader.distance_m)
        ahead = {
            s.stop_id: s
            for s in estimates.stops
            if s.direction_id == d and s.data_quality == "ok" and s.expected_boardings is not None
        }
        upcoming = [
            direction.stops[i].stop_id
            for i in range(here + 1, min(here + 1 + SKIP_LOOKAHEAD_STOPS, direction.n_stops - 1))
            if direction.stops[i].stop_id in ahead
        ]
        quiet = sorted(upcoming, key=lambda sid: ahead[sid].expected_boardings or 0.0)[:2]
        if quiet:
            stops = sorted(quiet, key=upcoming.index)
            names = ", ".join(ahead[s].name for s in stops)
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


async def _poor_past_outcomes(deps: WorkflowDeps) -> set[str]:
    """Action types whose measured outcomes (B12) made waits worse on average."""
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
    return {
        a.value for a, gain, n in rows if n >= MIN_PAST_OUTCOMES and gain is not None and gain < 0
    }


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
    proposed = propose(snapshot, deps.route.model, estimates)

    ids = [str(snapshot.pair.leader_vehicle_id), str(snapshot.pair.follower_vehicle_id)]
    ctx = await _rule_context(deps, snapshot, ids)
    poor = await _poor_past_outcomes(deps)
    kept: list[Candidate] = []
    dropped: list[DroppedCandidate] = []
    for c in proposed:
        reasons = violations(c, ctx, snapshot.pair.direction_id)
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
                source=CandidateSource.RULE,
                rationale=c.rationale,
            )
            for c in kept
        ]
        session.add_all(rows)
        await session.commit()
    kept = [c.model_copy(update={"db_id": row.id}) for c, row in zip(kept, rows, strict=True)]
    out: dict[str, Any] = {"snapshot": snapshot, "candidates": kept, "dropped": dropped}
    if optional.degraded is not None:
        out["degraded"] = [optional.degraded.tool]
    return out
