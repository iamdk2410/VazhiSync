"""B10 agent logic without the graph: rulebook, proposals, ranking, routing, observations."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.agents.action_generator import propose
from app.agents.control_advisor import rank
from app.agents.headway_monitor import decide, world_time
from app.agents.impact_simulator import build_observations, to_action
from app.agents.rulebook import RecentInstruction, RuleContext, violations
from app.agents.specs import AGENTS
from app.graph.state import Candidate, SimOutcome, Snapshot
from app.harness.specs import AGENT_TOOLS
from mcp_servers.schemas import (
    BoardingEstimates,
    HeadwayPair,
    Headways,
    StopEstimate,
    VehiclePosition,
    VehiclePositions,
)
from simulator import DoNothing, Hold, ShortTurn, SkipStops
from tests.sim_fixtures import route_32b

ROUTE = route_32b()
NOW = datetime(2026, 11, 24, 8, 30, tzinfo=UTC)
LEAD, FOLLOW, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def vehicle(
    vid: uuid.UUID, fleet: str, d: int, dist: float, stale: bool = False
) -> VehiclePosition:
    return VehiclePosition(
        vehicle_id=vid,
        fleet_number=fleet,
        direction_id=d,
        lon=80.28,
        lat=13.09,
        distance_m=dist,
        speed_mps=5,
        heading_deg=0,
        load=30,
        recorded_at=NOW,
        world_time_s=None if stale else 8 * 3600 + 1800,
        age_seconds=90 if stale else 2,
        is_stale=stale,
    )


def pair(
    status: str = "bunched", d: int = 0, ratio: float = 0.2, quality: str = "ok"
) -> HeadwayPair:
    return HeadwayPair(
        direction_id=d,
        leader_vehicle_id=LEAD,
        leader_fleet_number="32B-04",
        follower_vehicle_id=FOLLOW,
        follower_fleet_number="32B-05",
        gap_m=200,
        headway_seconds=ratio * 300,
        scheduled_headway_seconds=300,
        ratio=ratio,
        status=status,
        data_quality=quality,
        leader_age_seconds=2,
        follower_age_seconds=2,
    )


def snapshot(
    other_gap: float | None = None, estimates: BoardingEstimates | None = None
) -> Snapshot:
    pairs = [pair()]
    if other_gap is not None:
        p = pair("ok", d=1, ratio=other_gap)
        pairs.append(p.model_copy(update={"leader_vehicle_id": OTHER}))
    positions = VehiclePositions(
        route_id="32B",
        server_time=NOW,
        stale_after_seconds=60,
        missing=[],
        vehicles=[
            vehicle(LEAD, "32B-04", 0, 4100),
            vehicle(FOLLOW, "32B-05", 0, 3900),
            vehicle(OTHER, "32B-09", 1, 2000),
        ],
    )
    headways = Headways(
        route_id="32B",
        server_time=NOW,
        bunching_ratio=0.5,
        closing_ratio=0.75,
        stale_after_seconds=60,
        pairs=pairs,
        not_in_service=[],
        missing=[],
    )
    return Snapshot(
        world_time_s=8 * 3600 + 1800,
        positions=positions,
        headways=headways,
        estimates=estimates,
        pair=pairs[0],
    )


def estimates() -> BoardingEstimates:
    stops = [
        StopEstimate(
            direction_id=0,
            stop_id=s.stop_id,
            name=s.name,
            sequence=i + 1,
            waiting_now=3,
            arrivals_per_min=1.0,
            expected_boardings=float(10 + i % 5),
            sample_age_seconds=2,
            data_quality="ok",
        )
        for i, s in enumerate(ROUTE.direction(0).stops[:-1])
    ]
    return BoardingEstimates(route_id="32B", server_time=NOW, horizon_minutes=15, stops=stops)


def ctx(**changes: Any) -> RuleContext:
    base = RuleContext(
        route_id="32B",
        world_time_s=8 * 3600,
        now=NOW,
        stop_names={s.stop_id: s.name for d in ROUTE.directions for s in d.stops},
        last_departure_s={0: 21 * 3600 + 300, 1: 22 * 3600},
        headway_s={0: 300.0, 1: 360.0},
    )
    for key, value in changes.items():
        setattr(base, key, value)
    return base


def cand(
    kind: str, params: dict[str, Any], vid: uuid.UUID = FOLLOW, fleet: str = "32B-05"
) -> Candidate:
    return Candidate(
        key=f"{kind}:{fleet}",
        action_type=kind,
        vehicle_id=vid,
        fleet_number=fleet,
        params=params,
        rationale="test",
    )


# ------------------------------------------------------------------ rulebook


@pytest.mark.parametrize(
    ("candidate", "context", "rule"),
    [
        (cand("hold", {"seconds": 240}), {}, "R1"),
        (cand("skip_stop", {"stop_ids": ["MTC-52", "MTC-150", "MTC-100"]}), {}, "R2"),
        (cand("skip_stop", {"stop_ids": ["MTC-6809"]}), {}, "R3"),  # Stanley Hospital
        (cand("skip_stop", {"stop_ids": ["MTC-82"]}), {}, "R3"),  # Central railway station
        (cand("short_turn", {"at_stop_id": "MTC-100"}), {"world_time_s": 21 * 3600}, "R4"),
        (
            cand("hold", {"seconds": 60}),
            {
                "recent": [
                    RecentInstruction(str(FOLLOW), "skip_stop", NOW - timedelta(minutes=4), False)
                ]
            },
            "R5",
        ),
        (
            cand("hold", {"seconds": 60}),
            {"recent": [RecentInstruction(str(FOLLOW), "hold", NOW - timedelta(minutes=20), True)]},
            "R6",
        ),
    ],
)
def test_rulebook_hard_rules(candidate: Candidate, context: dict[str, Any], rule: str) -> None:
    assert any(v.startswith(rule) for v in violations(candidate, ctx(**context), 0))


def test_rulebook_allows_ordinary_actions() -> None:
    c = ctx(recent=[RecentInstruction(str(FOLLOW), "hold", NOW - timedelta(minutes=30), False)])
    assert violations(cand("hold", {"seconds": 180}), c, 0) == []
    assert violations(cand("skip_stop", {"stop_ids": ["MTC-52", "MTC-150"]}), c, 0) == []
    assert violations(cand("short_turn", {"at_stop_id": "MTC-100"}), c, 0) == []
    nothing = Candidate(
        key="do_nothing",
        action_type="do_nothing",
        vehicle_id=None,
        fleet_number=None,
        params={},
        rationale="",
    )
    assert violations(nothing, ctx(world_time_s=23 * 3600), 0) == []


def test_any_stop_named_hospital_is_protected_on_any_route() -> None:
    c = ctx(route_id="X1", stop_names={"S9": "Apollo Hospital Gate"})
    assert any(
        v.startswith("R3") for v in violations(cand("skip_stop", {"stop_ids": ["S9"]}), c, 0)
    )


# ------------------------------------------------------------------ proposals


def test_proposals_cover_hold_skip_short_turn_and_do_nothing() -> None:
    candidates = propose(snapshot(other_gap=1.8, estimates=estimates()), ROUTE, estimates())
    kinds = [c.action_type for c in candidates]
    assert kinds[-1] == "do_nothing" and 3 <= len(candidates) <= 5
    assert {"hold", "skip_stop", "short_turn"} <= set(kinds)
    hold = candidates[0]
    assert hold.fleet_number == "32B-05" and hold.params == {"seconds": 180}  # 240 s short, capped
    skip = next(c for c in candidates if c.action_type == "skip_stop")
    assert skip.fleet_number == "32B-04" and 1 <= len(skip.params["stop_ids"]) <= 2
    turn = next(c for c in candidates if c.action_type == "short_turn")
    assert turn.fleet_number == "32B-05"


def test_without_demand_data_or_opposite_gap_fewer_options() -> None:
    kinds = [c.action_type for c in propose(snapshot(), ROUTE, None)]
    assert "skip_stop" not in kinds and "short_turn" not in kinds
    assert kinds == ["hold", "hold", "do_nothing"]  # full and half hold


# ------------------------------------------------------------------ ranking


SEEDS = [11, 22, 33, 44, 55]


def outcome(
    key: str,
    wait: float | None,
    bunch: float = 1.0,
    status: str = "completed",
    spread: list[float] | None = None,
) -> SimOutcome:
    """Per-seed waits = wait + spread (common seeds, so benefits are paired)."""
    per_seed = [] if wait is None else [wait + d for d in (spread or [0, 0, 0, 0, 0])]
    return SimOutcome(
        candidate_key=key,
        status=status,
        seeds=SEEDS,
        passenger_wait_minutes=wait,
        per_seed_wait=per_seed,
        bunching_count=bunch if wait else None,
    )


CANDS = [
    cand("hold", {"seconds": 120}),
    cand("hold", {"seconds": 60}, fleet="32B-06"),
    Candidate(
        key="do_nothing",
        action_type="do_nothing",
        vehicle_id=None,
        fleet_number=None,
        params={},
        rationale="baseline",
    ),
]
A, B = CANDS[0].key, CANDS[1].key
NOISE = [-20.0, -10.0, 0.0, 10.0, 20.0]


def test_best_saving_is_recommended_with_a_confidence_range() -> None:
    base = outcome("do_nothing", 1000, spread=NOISE)
    a = outcome(A, 700, spread=[-30, -10, 0, 10, 30])  # paired benefits 290..310
    r = rank([base, a, outcome(B, 900, spread=NOISE)], CANDS, 15)
    assert r.candidate_key == A and r.benefit_passenger_minutes == 300
    assert r.end_reason == "recommended" and not r.is_near_tie
    best = r.ranking[0]
    assert best.benefit_low < 300 < best.benefit_high  # 95 % range of the mean
    assert best.bad_case_benefit == pytest.approx(294.0)  # 10th percentile of 290,300,300,300,310
    assert [x.candidate_key for x in r.ranking] == [A, B, "do_nothing"]
    assert "300 ±" in r.explanation and "worst case" in r.explanation


def test_overlapping_ranges_are_a_near_tie() -> None:
    base = outcome("do_nothing", 1000)
    a = outcome(A, 700, spread=[-60, -30, 0, 30, 60])
    b = outcome(B, 720, spread=[-50, -25, 0, 25, 50])
    r = rank([base, a, b], CANDS, 15)
    assert r.end_reason == "escalated" and r.is_near_tie and r.candidate_key == A
    apart = rank([base, outcome(A, 700, spread=NOISE), outcome(B, 900, spread=NOISE)], CANDS, 15)
    assert not apart.is_near_tie  # narrow, separated ranges: no tie


def test_safest_option_is_named_when_it_differs() -> None:
    base = outcome("do_nothing", 1000)
    risky = outcome(A, 680, spread=[-200, -100, 0, 100, 250])  # best mean, a bad seed
    steady = outcome(B, 740, spread=[-5, -2, 0, 2, 5])  # a little less, every time
    r = rank([base, risky, steady], CANDS, 15)
    assert r.candidate_key == A and r.safest_candidate_key == B
    assert "Safest option" in r.explanation


def test_do_nothing_wins_when_nothing_helps() -> None:
    r = rank([outcome("do_nothing", 1000), outcome(A, 1050), outcome(B, 1200)], CANDS, 15)
    assert r.candidate_key == "do_nothing" and r.end_reason == "do_nothing_recommended"


def test_fix_that_creates_new_bunching_is_rejected() -> None:
    r = rank(
        [outcome("do_nothing", 1000, bunch=1), outcome(A, 600, bunch=3), outcome(B, 900)], CANDS, 15
    )
    assert r.candidate_key == B
    assert next(x for x in r.ranking if x.candidate_key == A).rejected_reason is not None


def test_partial_timeouts_rank_the_completed_and_missing_baseline_escalates() -> None:
    r = rank(
        [outcome("do_nothing", 1000), outcome(A, None, status="timed_out"), outcome(B, 800)],
        CANDS,
        15,
    )
    assert r.candidate_key == B and "Not simulated in time" in r.explanation
    r = rank([outcome("do_nothing", None, status="timed_out"), outcome(A, 700)], CANDS, 15)
    assert r.end_reason == "escalated" and r.ranking == []


# ------------------------------------------------------------------ monitor and simulator inputs


def test_routing_decisions() -> None:
    assert decide(None) == "not_bunched"
    assert decide(pair("stale", quality="stale")) == "stale"
    assert decide(pair("closing", ratio=0.6)) == "not_bunched"
    assert decide(pair("bunched")) == "generate"


def test_observations_come_only_from_tool_results() -> None:
    snap = snapshot(estimates=estimates())
    obs = build_observations(snap, ROUTE)
    assert obs.time_s == world_time(snap.positions) == 8 * 3600 + 1800
    assert {v.vehicle_id for v in obs.vehicles} == {"32B-04", "32B-05", "32B-09"}
    assert obs.waiting[(0, 0)] == 3 and len(obs.waiting) == ROUTE.direction(0).n_stops - 1


def test_candidates_map_to_simulator_actions() -> None:
    dirs = {"32B-05": 0}
    assert to_action(cand("hold", {"seconds": 90}), ROUTE, dirs) == Hold("32B-05", 90)
    assert to_action(cand("skip_stop", {"stop_ids": ["MTC-52"]}), ROUTE, dirs) == SkipStops(
        "32B-05", (9,)
    )
    assert to_action(cand("short_turn", {"at_stop_id": "MTC-100"}), ROUTE, dirs) == ShortTurn(
        "32B-05", 12
    )
    assert to_action(CANDS[2], ROUTE, dirs) == DoNothing()


def test_agent_specs_match_the_enforced_allowlists() -> None:
    assert set(AGENTS) == set(AGENT_TOOLS)
    for name, spec in AGENTS.items():
        assert spec.allowed_tools == AGENT_TOOLS[name]
