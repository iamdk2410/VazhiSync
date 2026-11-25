"""B22 case retrieval without the database: features, confidence, reusing what worked."""

import math
import uuid
from typing import Any

from app.agents.action_generator import propose, with_case_holds
from app.agents.control_advisor import rank, with_cases
from app.db.vector import Vector, to_text
from app.harness.cases import (
    FEATURE_NAMES,
    UNKNOWN,
    SimilarCase,
    confidence,
    raw_features,
    vectorize,
)
from app.live.headways import DirectionInfo
from app.runner.route_loader import WorldRoute
from tests.unit.test_agents import CANDS, NOISE, ROUTE, A, B, estimates, outcome, snapshot

WORLD = WorldRoute(
    model=ROUTE,
    lines={},
    stop_ids={},
    directions={
        d.direction_id: DirectionInfo(
            first_stop_m=d.stops[0].distance_m,
            scheduled_headway_s=float(d.scheduled_headway_s),
            avg_speed_mps=6.0,
        )
        for d in ROUTE.directions
    },
)


def case(
    action: str = "hold",
    predicted: float = 100.0,
    actual: float | None = 90.0,
    seconds: int = 120,
    distance: float = 0.05,
) -> SimilarCase:
    return SimilarCase(
        case_id=uuid.uuid4(),
        distance=distance,
        action_type=action,
        params={"seconds": seconds} if action == "hold" else {"stop_ids": ["S1"]},
        predicted_benefit_passenger_minutes=predicted,
        actual_benefit_passenger_minutes=actual,
        bunching_resolved=True if actual is not None else None,
        decided_at="2026-11-24T08:30:00+00:00",
        situation={"gap_ratio": 0.2},
    )


# ------------------------------------------------------------------ features


def test_features_are_numbers_from_the_observations_scaled_to_about_0_1() -> None:
    raw = raw_features(snapshot(estimates=estimates()), WORLD)
    vector = vectorize(raw)
    assert len(vector) == len(FEATURE_NAMES) == 9
    assert all(0.0 <= v <= 1.0 for v in vector)
    assert raw["gap_ratio"] == 0.2 and raw["leader_load"] == 30.0
    assert raw["next_stop_demand"] > 0  # demand-mcp estimates at the leader's next stops
    assert vector == vectorize(raw_features(snapshot(estimates=estimates()), WORLD))  # stable


def test_missing_readings_sit_in_the_middle() -> None:
    raw = raw_features(snapshot(estimates=None), WORLD)
    assert raw["next_stop_demand"] == -1
    assert vectorize(raw)[FEATURE_NAMES.index("next_stop_demand")] == UNKNOWN


def test_a_different_situation_is_further_away() -> None:
    base = vectorize(raw_features(snapshot(estimates=estimates()), WORLD))
    other = raw_features(snapshot(estimates=estimates()), WORLD) | {"gap_ratio": 0.45}
    assert math.dist(base, base) == 0
    assert math.dist(base, vectorize(other)) > 0.1


def test_vectors_travel_as_text() -> None:
    assert to_text([0.5, 1, 0.25]) == "[0.5,1.0,0.25]"
    parse = Vector(3).result_processor(None, None)
    assert parse("[0.5,1,0.25]") == [0.5, 1.0, 0.25]


# ------------------------------------------------------------------ confidence


def test_confidence_drops_when_similar_cases_underperformed() -> None:
    poor = [case(predicted=100, actual=30), case(predicted=80, actual=20)]
    verdict = confidence("hold", poor)
    assert verdict.level == "lowered" and verdict.note is not None
    assert "50 passenger-minutes against 180 predicted (28%)" in verdict.note


def test_confidence_needs_enough_measured_cases_of_the_same_action() -> None:
    assert confidence("hold", [case(actual=10)]).level == "normal"  # just one
    assert confidence("hold", [case(actual=None), case(actual=None)]).level == "normal"
    other = [case("skip_stop", actual=1), case("skip_stop", actual=2)]
    assert confidence("hold", other).level == "normal"  # a different action
    good = [case(predicted=100, actual=95), case(predicted=50, actual=45)]
    assert confidence("hold", good).level == "normal"


def test_the_card_gets_the_cases_and_the_note_follows_any_wording() -> None:
    rec = rank([outcome("do_nothing", 1000, spread=NOISE), outcome(A, 700, spread=NOISE),
                outcome(B, 900, spread=NOISE)], CANDS, 15)  # fmt: skip
    poor = [c.model_dump(mode="json") for c in (case(actual=30), case(actual=20))]
    out = with_cases(rec, CANDS, poor)
    assert out.similar_cases == poor and out.confidence == "lowered"
    assert out.confidence_note is not None and out.explanation.startswith(rec.explanation)
    assert out.confidence_note in out.explanation
    assert out.candidate_key == rec.candidate_key and out.ranking == rec.ranking  # rule 2
    assert with_cases(rec, CANDS, []) == rec


# ------------------------------------------------------------------ reusing what worked


def test_a_hold_that_worked_in_a_similar_case_is_offered_again() -> None:
    snap = snapshot(estimates=estimates())
    base = propose(snap, ROUTE, snap.estimates)
    worked = case(seconds=60, actual=40)
    out = with_case_holds(base, snap, [worked])
    added = [c for c in out if c.key == "hold:32B-05:60"]
    assert added and "saved 40 passenger-minutes" in added[0].rationale
    assert out[-1].action_type == "do_nothing" and len(out) <= 5


def test_failed_unmeasured_or_non_hold_cases_add_nothing() -> None:
    snap = snapshot(estimates=estimates())
    base = propose(snap, ROUTE, snap.estimates)
    nothing: list[Any] = [
        case(seconds=60, actual=-5),
        case(seconds=60, actual=None),
        case("skip_stop", actual=50),
        case(seconds=180, actual=50),  # already proposed by the rules
    ]
    assert [c.key for c in with_case_holds(base, snap, nothing)] == [c.key for c in base]
