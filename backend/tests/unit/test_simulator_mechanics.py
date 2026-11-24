"""Dwell, capacity, terminals, actions and events on a small, quiet, deterministic route."""

import dataclasses

import pytest

from simulator import (
    ActionError,
    Breakdown,
    DemandSurge,
    DoNothing,
    Hold,
    Observations,
    RouteModel,
    RouteModelError,
    ShortTurn,
    SimConfig,
    Simulation,
    SkipStops,
    VehicleObservation,
    Weather,
    fast_forward,
    from_observations,
)
from simulator.engine import BusState
from simulator.metrics import headway_cv
from tests.sim_fixtures import QUIET, SEGMENT_S, line_route, route_32b, run_until


def _one_bus(
    at_stop: int = 1,
    direction: int = 0,
    waiting: dict[tuple[int, int], int] | None = None,
    config: SimConfig = QUIET,
    route: RouteModel | None = None,
) -> Simulation:
    """One bus that has just left `at_stop`."""
    route = route or line_route()
    obs = Observations(
        time_s=8 * 3600,
        vehicles=(
            VehicleObservation(
                "BUS", direction, route.direction(direction).stops[at_stop].distance_m, load=0
            ),
        ),
        waiting=waiting or {},
    )
    return from_observations(route, obs, config, seed=1)


def _bus(sim: Simulation) -> BusState:
    return sim.state.buses[0]


def test_dwell_follows_the_build_plan_rule() -> None:
    sim = _one_bus(waiting={(0, 2): 4})
    arrived = run_until(sim, "BUS", lambda b: b.stop_index == 2)
    assert _bus(sim).load == 4 and _bus(sim).status == "dwelling"
    left = run_until(sim, "BUS", lambda b: b.status == "running")
    assert left - arrived == pytest.approx(10 + 2.5 * 4, abs=1)


def test_alighting_adds_to_the_dwell() -> None:
    sim = _one_bus()
    _bus(sim).onboard[2] = 6  # six people for stop 2
    arrived = run_until(sim, "BUS", lambda b: b.stop_index == 2)
    left = run_until(sim, "BUS", lambda b: b.status == "running")
    assert left - arrived == pytest.approx(10 + 1.5 * 6, abs=1)
    assert sim.metrics().alighted == 6


def test_empty_stop_is_passed_without_stopping() -> None:
    sim = _one_bus()
    t2 = run_until(sim, "BUS", lambda b: b.stop_index == 2)
    t3 = run_until(sim, "BUS", lambda b: b.stop_index == 3)
    assert t3 - t2 == pytest.approx(SEGMENT_S, abs=1)


def test_full_bus_leaves_people_behind() -> None:
    sim = _one_bus(waiting={(0, 2): 100})
    run_until(sim, "BUS", lambda b: b.stop_index == 2)
    assert _bus(sim).load == 80
    assert sim.metrics().left_behind == 20
    assert sim.state.queues[0][2] == 20


def test_terminal_layover_then_the_other_direction() -> None:
    sim = _one_bus(at_stop=4)
    run_until(sim, "BUS", lambda b: b.status == "layover")
    assert (_bus(sim).direction, _bus(sim).stop_index) == (1, 0)
    t_layover = sim.state.elapsed_s
    t_out = run_until(sim, "BUS", lambda b: b.status == "running")
    assert t_out - t_layover == pytest.approx(QUIET.min_layover_s, abs=1)


def test_hold_requested_while_running_applies_at_the_next_stop() -> None:
    sim = _one_bus()
    sim.apply(Hold("BUS", 30))
    arrived = run_until(sim, "BUS", lambda b: b.stop_index == 2)
    left = run_until(sim, "BUS", lambda b: b.status == "running")
    assert left - arrived == pytest.approx(10 + 30, abs=1)


def test_skip_stop_overcarries_passengers_to_the_next_stop() -> None:
    sim = _one_bus()
    _bus(sim).onboard[2] = 5
    sim.apply(SkipStops("BUS", (2,)))
    t2 = run_until(sim, "BUS", lambda b: b.stop_index == 2)
    assert _bus(sim).status == "running"  # did not stop
    assert _bus(sim).onboard[3] == 5
    assert sim.metrics().overcarried == 5
    run_until(sim, "BUS", lambda b: b.stop_index == 3)
    assert sim.metrics().alighted == 5
    assert sim.state.elapsed_s - t2 == pytest.approx(SEGMENT_S, abs=1)


def test_short_turn_displaces_through_riders_and_reverses() -> None:
    sim = _one_bus()
    bus = _bus(sim)
    bus.onboard[2], bus.onboard[4], bus.onboard[5] = 2, 3, 4
    sim.apply(ShortTurn("BUS", 3))
    run_until(sim, "BUS", lambda b: b.status == "layover")
    m = sim.metrics()
    assert m.displaced == 7  # riders for stops 4 and 5
    assert m.alighted == 9
    assert _bus(sim).direction == 1
    assert _bus(sim).stop_index == 2  # west stop 2 is the same place as east stop 3
    assert _bus(sim).load == 0


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (SkipStops("BUS", (0,)), "not an upcoming"),  # behind the bus
        (SkipStops("BUS", (5,)), "not an upcoming"),  # the terminal
        (ShortTurn("BUS", 5), "not an upcoming"),
        (Hold("GHOST", 30), "unknown bus"),
    ],
)
def test_invalid_actions_are_rejected(action: object, message: str) -> None:
    with pytest.raises(ActionError, match=message):
        _one_bus().apply(action)  # type: ignore[arg-type]


def test_short_turn_needs_a_nearby_opposite_stop() -> None:
    sim = _one_bus(route=line_route(far_return=True))
    with pytest.raises(ActionError, match="no opposite-direction stop"):
        sim.apply(ShortTurn("BUS", 3))


def test_hold_bounds() -> None:
    with pytest.raises(ActionError):
        Hold("BUS", 0)
    with pytest.raises(ActionError):
        Hold("BUS", 3600)


def test_weather_slows_travel() -> None:
    rain = dataclasses.replace(QUIET, events=(Weather(0, 24 * 3600, speed_factor=0.5),))
    sim = _one_bus(config=rain)
    assert run_until(sim, "BUS", lambda b: b.stop_index == 2) == pytest.approx(2 * SEGMENT_S, abs=1)


def test_demand_surge_fills_the_surging_stop() -> None:
    base = dataclasses.replace(QUIET, base_arrivals_per_min=1.0)
    surge = dataclasses.replace(base, events=(DemandSurge(0, (2,), 0, 24 * 3600, 10.0),))
    calm, busy = _one_bus(at_stop=4, config=base), _one_bus(at_stop=4, config=surge)
    calm.step(600)
    busy.step(600)
    assert busy.state.queues[0][2] > 4 * calm.state.queues[0][2]
    assert busy.state.queues[0][1] == calm.state.queues[0][1]  # other stops unaffected


def test_breakdown_freezes_the_bus() -> None:
    cfg = dataclasses.replace(QUIET, events=(Breakdown("BUS", 8 * 3600 + 10, 120),))
    sim = _one_bus(config=cfg)
    sim.step(15)
    frozen_at = _bus(sim).distance_m
    sim.step(100)
    assert _bus(sim).distance_m == frozen_at
    assert sim.snapshot().buses[0].broken_down
    sim.step(60)
    assert _bus(sim).distance_m > frozen_at


def test_passengers_are_conserved() -> None:
    sim = Simulation.new(route_32b(), seed=5)
    riding_at_start = sum(b.load for b in sim.state.buses)  # warm start: buses aren't empty
    sim.step(3600)
    m = sim.metrics()
    riding_now = sum(b.load for b in sim.state.buses)
    assert riding_at_start + m.boarded == m.alighted + riding_now


def test_fast_forward_leaves_the_original_untouched() -> None:
    world = Simulation.new(route_32b(), seed=2)
    world.step(600)
    before = world.snapshot()
    result = fast_forward(world, Hold(world.state.buses[0].bus_id, 60), 15)
    assert world.snapshot() == before
    assert result.elapsed_s == 15 * 60
    assert result != fast_forward(world, DoNothing(), 15)


def test_from_observations_uses_only_what_was_observed() -> None:
    route = line_route()
    obs = Observations(
        time_s=9 * 3600,
        vehicles=(
            VehicleObservation("X", 0, 700.0, load=12),
            VehicleObservation("Y", 1, 1200.0, load=None, stale=True),  # stale: still modelled
        ),
        waiting={(0, 3): 9},
    )
    sim = from_observations(route, obs, QUIET, seed=4)
    x, y = sim.state.buses
    assert (x.direction, x.stop_index, x.distance_m, x.load) == (0, 1, 700.0, 12)
    assert (y.direction, y.stop_index) == (1, 2)
    assert sim.state.queues[0][3] == 9
    assert sim.state.time_s == 9 * 3600
    assert sum(x.onboard[:2]) == 0  # nobody is bound for a stop already passed


@pytest.mark.parametrize(
    ("vehicles", "message"),
    [
        ((), "no vehicles"),
        ((VehicleObservation("A", 0, 0.0), VehicleObservation("A", 0, 10.0)), "observed twice"),
        ((VehicleObservation("A", 2, 0.0),), "invalid direction"),
        ((VehicleObservation("A", 0, 0.0, load=500),), "out of range"),
    ],
)
def test_bad_observations_are_rejected(
    vehicles: tuple[VehicleObservation, ...], message: str
) -> None:
    from simulator import ObservationError

    with pytest.raises(ObservationError, match=message):
        from_observations(line_route(), Observations(0, vehicles), QUIET)


def test_route_model_round_trip_and_validation() -> None:
    route = route_32b()
    assert RouteModel.from_dict(route.to_dict()) == route
    data = route.to_dict()
    data["directions"][0]["stops"][3]["distance_m"] = 0.0
    with pytest.raises(RouteModelError, match="is not after"):
        RouteModel.from_dict(data)


def test_32b_has_turn_points_for_mid_route_stops() -> None:
    pairs = route_32b().turn_pairs[0]
    assert sum(p is not None for p in pairs[1:-1]) >= len(pairs) - 4


def test_headway_cv() -> None:
    assert headway_cv({0: [300, 300, 300], 1: []}) == 0
    assert headway_cv({0: [100, 500], 1: [300, 300]}) == pytest.approx((200 / 300 + 0) / 2)
    assert headway_cv({0: [], 1: []}) is None
