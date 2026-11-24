"""Plausibility checks (in code, never left to the LLM) and conversion to simulator inputs.

`problems(plan, ctx)` returns human-readable problems; an empty list means the plan is usable.
The messages are written so they can be sent back to the LLM verbatim for a regeneration.
"""

import dataclasses
from dataclasses import dataclass

from scenario_gen.schema import ScenarioPlan
from simulator import (
    Breakdown,
    DemandSurge,
    Event,
    RouteModel,
    SimConfig,
    Simulation,
    Slowdown,
    Weather,
)

# Speed factors that make sense for each kind of weather (Chennai monsoon traffic).
WEATHER_SPEED = {
    "light_rain": (0.8, 0.97),
    "heavy_rain": (0.55, 0.85),
    "waterlogging": (0.3, 0.6),
}
MAX_COMBINED_DEMAND = 8.0  # base x weather x surge at any one stop
MAX_DROPOUT_SHARE = 0.3  # of the fleet
DAY_END_S = 24 * 3600


@dataclass(frozen=True)
class ScenarioContext:
    """What a plan is checked against: the route and its fleet."""

    route: RouteModel
    fleet_size: int

    @classmethod
    def for_route(cls, route: RouteModel) -> "ScenarioContext":
        return cls(route=route, fleet_size=len(Simulation.new(route).state.buses))

    def bus_id(self, number: int) -> str:
        return f"{self.route.route_id}-{number:02d}"

    def stop_index(self, direction_id: int, stop_id: str) -> int | None:
        for i, stop in enumerate(self.route.direction(direction_id).stops):
            if stop.stop_id == stop_id:
                return i
        return None


@dataclass(frozen=True)
class DropoutSpec:
    bus_id: str
    start_s: int  # simulated seconds from the world's start
    duration_s: int


def _window(label: str, start: int, end: int, duration: int, out: list[str]) -> None:
    if start >= end:
        out.append(f"{label}: start_min ({start}) must be before end_min ({end})")
    if end > duration:
        out.append(f"{label}: end_min ({end}) is after the scenario ends ({duration} min)")


def problems(plan: ScenarioPlan, ctx: ScenarioContext) -> list[str]:
    out: list[str] = []
    d = plan.duration_min
    if plan.start_time_s + d * 60 > DAY_END_S:
        out.append("the scenario must end before midnight")

    for k, w in enumerate(plan.weather):
        label = f"weather[{k}] ({w.kind})"
        _window(label, w.start_min, w.end_min, d, out)
        lo, hi = WEATHER_SPEED[w.kind]
        if not lo <= w.speed_factor <= hi:
            out.append(f"{label}: speed_factor {w.speed_factor} should be between {lo} and {hi}")
    for earlier, later in zip(plan.weather, plan.weather[1:], strict=False):
        if later.start_min < earlier.end_min:
            out.append("weather windows must not overlap; list them in time order")

    for k, s in enumerate(plan.surges):
        label = f"surges[{k}]"
        _window(label, s.start_min, s.end_min, d, out)
        if len(set(s.stop_ids)) != len(s.stop_ids):
            out.append(f"{label}: a stop is listed twice")
        for stop_id in s.stop_ids:
            i = ctx.stop_index(s.direction_id, stop_id)
            if i is None:
                out.append(f"{label}: {stop_id} is not a stop of direction {s.direction_id}")
            elif i == ctx.route.direction(s.direction_id).n_stops - 1:
                out.append(f"{label}: {stop_id} is the last stop; nobody boards there")

    for k, sd in enumerate(plan.slowdowns):
        label = f"slowdowns[{k}]"
        _window(label, sd.start_min, sd.end_min, d, out)
        first = ctx.stop_index(sd.direction_id, sd.from_stop_id)
        last = ctx.stop_index(sd.direction_id, sd.to_stop_id)
        if first is None or last is None:
            missing = sd.from_stop_id if first is None else sd.to_stop_id
            out.append(f"{label}: {missing} is not a stop of direction {sd.direction_id}")
        elif first >= last:
            out.append(f"{label}: from_stop_id must come before to_stop_id in that direction")

    buses = [*(b.bus_number for b in plan.breakdowns), *(g.bus_number for g in plan.gps_dropouts)]
    for number in buses:
        if number > ctx.fleet_size:
            out.append(f"bus_number {number}: the route only has buses 1-{ctx.fleet_size}")
    broken = [b.bus_number for b in plan.breakdowns]
    if len(set(broken)) != len(broken):
        out.append("each bus can break down at most once")
    for k, br in enumerate(plan.breakdowns):
        if br.at_min >= d:
            out.append(f"breakdowns[{k}]: at_min is after the scenario ends")
    if len({g.bus_number for g in plan.gps_dropouts}) > MAX_DROPOUT_SHARE * ctx.fleet_size:
        out.append(f"GPS dropouts on more than {MAX_DROPOUT_SHARE:.0%} of the fleet is implausible")
    for k, gd in enumerate(plan.gps_dropouts):
        if gd.at_min >= d:
            out.append(f"gps_dropouts[{k}]: at_min is after the scenario ends")

    worst_weather = max((w.demand_factor for w in plan.weather), default=1.0)
    worst_surge = max((s.multiplier for s in plan.surges), default=1.0)
    combined = plan.demand_multiplier * worst_weather * worst_surge
    if combined > MAX_COMBINED_DEMAND:
        out.append(
            f"demand_multiplier x weather demand_factor x surge multiplier reaches {combined:.1f}; "
            f"keep it at or below {MAX_COMBINED_DEMAND:g} (buses hold 80 people)"
        )
    return out


def to_sim_config(
    plan: ScenarioPlan, ctx: ScenarioContext, base: SimConfig | None = None
) -> SimConfig:
    """The simulator settings for a checked plan (event times become clock seconds)."""
    base = base or SimConfig()
    t0 = plan.start_time_s

    def at(minute: int) -> int:
        return t0 + minute * 60

    events: list[Event] = []
    events += [
        Weather(at(w.start_min), at(w.end_min), w.speed_factor, w.demand_factor)
        for w in plan.weather
    ]
    for s in plan.surges:
        indices = tuple(
            i for i in (ctx.stop_index(s.direction_id, x) for x in s.stop_ids) if i is not None
        )
        events.append(
            DemandSurge(s.direction_id, indices, at(s.start_min), at(s.end_min), s.multiplier)
        )
    for sd in plan.slowdowns:
        a = ctx.stop_index(sd.direction_id, sd.from_stop_id)
        b = ctx.stop_index(sd.direction_id, sd.to_stop_id)
        if a is not None and b is not None:
            events.append(
                Slowdown(sd.direction_id, a, b, at(sd.start_min), at(sd.end_min), sd.speed_factor)
            )
    events += [
        Breakdown(ctx.bus_id(b.bus_number), at(b.at_min), b.duration_min * 60)
        for b in plan.breakdowns
    ]
    return dataclasses.replace(
        base, start_time_s=t0, demand_multiplier=plan.demand_multiplier, events=tuple(events)
    )


def dropouts(plan: ScenarioPlan, ctx: ScenarioContext) -> list[DropoutSpec]:
    """GPS dropouts are an observation-layer effect: the live runner applies them, not the
    simulator (the world keeps moving; only its GPS goes quiet)."""
    return [
        DropoutSpec(ctx.bus_id(g.bus_number), g.at_min * 60, g.duration_min * 60)
        for g in plan.gps_dropouts
    ]
