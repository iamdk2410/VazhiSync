"""B21 early warning, without the database: the predictor on a golden scenario, and the rules.

Done when: a golden scenario triggers a predicted event before actual bunching, and the early
action needs a smaller intervention than waiting.

The world (the golden scenario's simulation) plays the role of reality; the predictor only gets
what GPS would report (positions, loads) and the headways computed from them, as through
avl-mcp (rule 1).
"""

import math
import uuid

import pytest

from app.agents.early_warning import predict_bunching
from app.agents.headway_monitor import decide
from app.agui.view import RunView, fold
from app.live.headways import DirectionInfo, Fix, compute_headways
from mcp_servers.schemas import HeadwayPair
from scenario_gen.checks import ScenarioContext, to_sim_config
from scenario_gen.golden import load_golden
from simulator import Hold, Observations, RouteModel, SimConfig, Simulation, VehicleObservation
from tests.sim_fixtures import route_32b
from tests.unit.test_agents import pair as headway_pair

ROUTE = route_32b()
HORIZON_S = 15 * 60
SEEDS = (1, 2, 3)


def directions(route: RouteModel) -> dict[int, DirectionInfo]:
    return {
        d.direction_id: DirectionInfo(
            first_stop_m=d.stops[0].distance_m,
            scheduled_headway_s=float(d.scheduled_headway_s),
            avg_speed_mps=(d.stops[-1].distance_m - d.stops[0].distance_m)
            / d.stops[-1].scheduled_offset_s,
        )
        for d in route.directions
    }


DIRS = directions(ROUTE)
_ids: dict[str, uuid.UUID] = {}


def headways_of(sim: Simulation, config: SimConfig) -> list[HeadwayPair]:
    """What avl-mcp's get_headways would report for this world (fresh GPS)."""
    fixes = [Fix(b.bus_id, b.direction, b.distance_m, 0.0) for b in sim.snapshot().buses]
    return [
        HeadwayPair(
            direction_id=h.direction_id,
            leader_vehicle_id=_ids.setdefault(h.leader_id, uuid.uuid4()),
            leader_fleet_number=h.leader_id,
            follower_vehicle_id=_ids.setdefault(h.follower_id, uuid.uuid4()),
            follower_fleet_number=h.follower_id,
            gap_m=h.gap_m,
            headway_seconds=h.headway_s,
            scheduled_headway_seconds=h.scheduled_headway_s,
            ratio=h.ratio,
            status=h.status,
            data_quality="ok",
            leader_age_seconds=0,
            follower_age_seconds=0,
        )
        for h in compute_headways(
            fixes, DIRS, math.inf, config.bunching_ratio, config.closing_ratio
        )
    ]


def gps_observations(sim: Simulation) -> Observations:
    snap = sim.snapshot()
    return Observations(
        time_s=snap.time_s,
        vehicles=tuple(
            VehicleObservation(b.bus_id, b.direction, b.distance_m, b.load) for b in snap.buses
        ),
    )


def bunches_within(sim: Simulation, pair: tuple[str, str], config: SimConfig) -> bool:
    c = sim.copy()
    for _ in range(HORIZON_S // 30):
        c.step(30)
        if any(
            (h.leader_fleet_number, h.follower_fleet_number) == pair and h.status == "bunched"
            for h in headways_of(c, config)
        ):
            return True
    return False


def smallest_hold(sim: Simulation, pair: tuple[str, str], config: SimConfig) -> int | None:
    """The shortest hold of the follower (30 s steps, rulebook max 180 s) after which the
    pair does not bunch in the next 15 minutes; None if even 180 s doesn't do it."""
    for seconds in range(30, 181, 30):
        c = sim.copy()
        c.apply(Hold(pair[1], seconds))
        if not bunches_within(c, pair, config):
            return seconds
    return None


def test_a_golden_scenario_is_predicted_early_and_the_early_fix_is_smaller() -> None:
    golden = next(g for g in load_golden() if g.key == "normal_morning_peak")
    config = to_sim_config(golden.plan, ScenarioContext.for_route(ROUTE), SimConfig())
    world = Simulation.new(ROUTE, config, golden.seed)

    predicted: dict[tuple[str, str], tuple[float, float]] = {}  # pair -> (when, lead time)
    actual: tuple[tuple[str, str], float] | None = None
    elapsed = 0.0
    while actual is None and elapsed < 40 * 60:
        pairs = headways_of(world, config)
        bunched = [p for p in pairs if p.status == "bunched"]
        if bunched:
            actual = ((bunched[0].leader_fleet_number, bunched[0].follower_fleet_number), elapsed)
            break
        for p in predict_bunching(
            ROUTE, DIRS, gps_observations(world), config, pairs,
            seeds=SEEDS, horizon_s=HORIZON_S, step_s=30, min_agree=2,
        ):  # fmt: skip
            predicted.setdefault((p.leader, p.follower), (elapsed, p.in_s))
        world.step(60)
        elapsed += 60
    assert actual is not None, "the golden scenario should bunch by itself"
    pair, bunched_at = actual
    print(f"\n  first bunch {pair} at {bunched_at / 60:.1f} min; predicted: {predicted.get(pair)}")

    # Predicted before it happened, within the 10-15 minute horizon.
    assert pair in predicted
    warned_at, lead_s = predicted[pair]
    assert warned_at < bunched_at
    assert 0 < lead_s <= HORIZON_S
    assert bunched_at - warned_at >= 5 * 60  # real warning time, not a last-second call

    # Acting at the warning needs a smaller fix than waiting for the bunch.
    replay = Simulation.new(ROUTE, config, golden.seed)
    replay.step(warned_at)
    early = smallest_hold(replay, pair, config)
    replay.step(bunched_at - warned_at)
    late = smallest_hold(replay, pair, config)
    print(f"  smallest hold that works: early {early} s, once bunched {late} s")
    assert early is not None
    assert late is None or early < late


def test_only_fresh_pairs_that_are_fine_now_are_watched() -> None:
    sim = Simulation.new(ROUTE, SimConfig(), 1)
    obs = gps_observations(sim)
    already = [headway_pair("bunched"), headway_pair("ok", quality="stale")]
    assert predict_bunching(
        ROUTE, DIRS, obs, SimConfig(), already, seeds=SEEDS, horizon_s=60, step_s=30, min_agree=1
    ) == []  # fmt: skip


def test_seeds_must_agree() -> None:
    golden = next(g for g in load_golden() if g.key == "normal_morning_peak")
    config = to_sim_config(golden.plan, ScenarioContext.for_route(ROUTE), SimConfig())
    world = Simulation.new(ROUTE, config, golden.seed)
    world.step(180)
    args = (ROUTE, DIRS, gps_observations(world), config, headways_of(world, config))
    loose = predict_bunching(*args, seeds=SEEDS, horizon_s=HORIZON_S, step_s=30, min_agree=1)
    strict = predict_bunching(*args, seeds=SEEDS, horizon_s=HORIZON_S, step_s=30, min_agree=3)
    assert {(p.leader, p.follower) for p in strict} <= {(p.leader, p.follower) for p in loose}
    assert all(p.seeds_agreeing == 3 for p in strict)
    assert all(p.in_s <= HORIZON_S for p in loose)


@pytest.mark.parametrize(
    ("status", "predicted", "route"),
    [
        ("bunched", False, "generate"),
        ("closing", False, "not_bunched"),
        ("closing", True, "generate"),  # acting early is the point of a prediction
        ("ok", True, "generate"),
        ("stale", True, "stale"),  # rule 4 holds for predictions too
    ],
)
def test_headway_monitor_accepts_a_predicted_pair_before_it_bunches(
    status: str, predicted: bool, route: str
) -> None:
    assert decide(headway_pair(status), predicted=predicted) == route
    assert decide(None, predicted=predicted) == "not_bunched"


def test_the_run_view_says_it_was_an_early_warning() -> None:
    view = fold(RunView(run_id="r"), "headway_monitor", {"event_kind": "predicted"})
    assert view.event_kind == "predicted"
