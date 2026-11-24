"""B4 acceptance: with no control, a seeded run reliably bunches within 15-30 simulated
minutes, and the same seed always gives the same result."""

import statistics

from simulator import Simulation, fast_forward
from simulator.actions import DoNothing
from tests.sim_fixtures import route_32b

SEEDS = range(1, 31)


def _first_bunching_minutes(seed: int, limit_min: int = 45) -> float | None:
    sim = Simulation.new(route_32b(), seed=seed)
    while sim.state.elapsed_s < limit_min * 60 and sim.state.metrics.first_bunching_s is None:
        sim.step(30)
    first = sim.state.metrics.first_bunching_s
    return None if first is None else first / 60


def test_do_nothing_reliably_bunches_within_15_to_30_minutes() -> None:
    firsts = [_first_bunching_minutes(seed) for seed in SEEDS]
    assert all(f is not None and f <= 35 for f in firsts), firsts  # every run bunches
    in_window = [f for f in firsts if f is not None and 15 <= f <= 30]
    assert len(in_window) / len(firsts) >= 0.75, firsts
    assert 15 <= statistics.median(f for f in firsts if f is not None) <= 30


def test_same_seed_same_result() -> None:
    a, b = Simulation.new(route_32b(), seed=7), Simulation.new(route_32b(), seed=7)
    a.step(45 * 60)
    b.step(45 * 60)
    assert a.snapshot() == b.snapshot()


def test_different_seed_different_result() -> None:
    a, b = Simulation.new(route_32b(), seed=7), Simulation.new(route_32b(), seed=8)
    a.step(45 * 60)
    b.step(45 * 60)
    assert a.snapshot() != b.snapshot()


def test_what_if_runs_are_reproducible() -> None:
    world = Simulation.new(route_32b(), seed=3)
    world.step(20 * 60)
    assert fast_forward(world, DoNothing(), 15) == fast_forward(world, DoNothing(), 15)


def test_simulated_trip_times_match_the_timetable() -> None:
    """Calibration guard: the median trip should stay close to the GTFS run times."""
    route = route_32b()
    trips: dict[int, list[float]] = {0: [], 1: []}
    for seed in (1, 2, 3):
        sim = Simulation.new(route, seed=seed)
        started: dict[str, tuple[int, float]] = {}
        for _ in range(2 * 3600 // 10):
            sim.step(10)
            for bus in sim.state.buses:
                if bus.status == "running" and bus.stop_index == 0 and bus.bus_id not in started:
                    started[bus.bus_id] = (bus.direction, sim.state.time_s)
                begun = started.get(bus.bus_id)
                if begun and bus.status == "layover" and bus.direction != begun[0]:
                    trips[begun[0]].append((sim.state.time_s - begun[1]) / 60)
                    del started[bus.bus_id]
    for d in (0, 1):
        scheduled = route.direction(d).scheduled_run_s / 60
        assert abs(statistics.median(trips[d]) - scheduled) <= 0.15 * scheduled
