"""Small routes and helpers for simulator tests."""

import json
from pathlib import Path

from simulator import RouteModel, SimConfig, Simulation
from simulator.model import DirectionModel, StopModel

FIXTURES = Path(__file__).parent / "fixtures"

# A quiet world: no random demand, no travel noise, short layovers. Each 500 m segment takes
# max(60 s x 0.7, 500 m / 12.5 m/s) = 42 s.
QUIET = SimConfig(
    base_arrivals_per_min=0.0,
    travel_noise_sigma=0.0,
    initial_spacing_jitter_s=0.0,
    min_layover_s=60.0,
    run_share=0.70,  # pinned so segment times below stay exact if the default is retuned
)
SEGMENT_S = 42


def route_32b() -> RouteModel:
    return RouteModel.from_dict(json.loads((FIXTURES / "route_32b.json").read_text("utf-8")))


def line_route(n: int = 6, spacing_m: float = 500.0, far_return: bool = False) -> RouteModel:
    """Two directions along one straight street (or, with far_return, a parallel street 1 km
    away so short-turns have nowhere to turn)."""
    step = spacing_m / 108_000  # ~degrees of longitude per metre at 13 N
    out = tuple(
        StopModel(f"A{i}", f"Stop {i}", 80.25 + i * step, 13.05, i * spacing_m, i * 60)
        for i in range(n)
    )
    lat_back = 13.059 if far_return else 13.05
    back = tuple(
        StopModel(
            f"B{i}",
            f"Stop {n - 1 - i}",
            80.25 + (n - 1 - i) * step,
            lat_back,
            i * spacing_m,
            i * 60,
        )
        for i in range(n)
    )
    return RouteModel(
        route_id="LINE",
        directions=(
            DirectionModel(0, "East", (n - 1) * spacing_m, 300, out),
            DirectionModel(1, "West", (n - 1) * spacing_m, 300, back),
        ),
    )


def run_until(sim: Simulation, bus_id: str, predicate, limit_s: int = 3600) -> float:  # type: ignore[no-untyped-def]
    """Tick until predicate(bus) is true; return the elapsed seconds."""
    for _ in range(limit_s):
        bus = next(b for b in sim.state.buses if b.bus_id == bus_id)
        if predicate(bus):
            return sim.state.elapsed_s
        sim.step(1)
    raise AssertionError(f"condition not reached within {limit_s} s")
