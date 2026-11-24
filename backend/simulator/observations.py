"""Building a what-if model from what the advisor can observe (rule 1).

The advisor only ever sees what the MCP tools return: vehicle positions (with staleness),
loads when known, and stop queue estimates. It never sees the world simulator's hidden state
(true passenger destinations, travel-time draws, random streams). `from_observations` takes
exactly those observable fields and fills the gaps with typical values.
"""

from dataclasses import dataclass, field

from simulator.config import SimConfig
from simulator.engine import BusState, Simulation, _empty_state
from simulator.model import RouteModel


class ObservationError(ValueError):
    pass


@dataclass(frozen=True)
class VehicleObservation:
    vehicle_id: str
    direction: int
    distance_m: float  # along that direction's geometry
    load: int | None = None  # passenger count if the AVL reports it
    stale: bool = False  # GPS older than STALE_GPS_SECONDS (still modelled, never instructed)


@dataclass(frozen=True)
class Observations:
    time_s: float
    vehicles: tuple[VehicleObservation, ...]
    # (direction, stop_index) -> people waiting, from demand estimates; missing = typical value
    waiting: dict[tuple[int, int], int] = field(default_factory=dict)


def from_observations(
    route: RouteModel, observations: Observations, config: SimConfig | None = None, seed: int = 0
) -> Simulation:
    config = config or SimConfig()
    if not observations.vehicles:
        raise ObservationError("no vehicles observed")
    state = _empty_state(route, config, seed)
    state.time_s = float(observations.time_s)
    sim = Simulation(route, config, seed, state)
    seen: set[str] = set()
    for v in observations.vehicles:
        if v.vehicle_id in seen:
            raise ObservationError(f"vehicle {v.vehicle_id} observed twice")
        seen.add(v.vehicle_id)
        if v.direction not in (0, 1):
            raise ObservationError(f"vehicle {v.vehicle_id}: invalid direction {v.direction}")
        direction = route.direction(v.direction)
        distance = min(max(v.distance_m, 0.0), direction.stops[-1].distance_m)
        i = direction.stop_index_at(distance)
        bus = BusState(
            bus_id=v.vehicle_id,
            direction=v.direction,
            stop_index=i,
            distance_m=distance,
            status="layover",
            capacity=config.capacity,
            onboard=[0] * direction.n_stops,
        )
        if i >= direction.n_stops - 1:
            bus.layover_remaining_s = config.min_layover_s  # at the terminal
        else:
            sim._depart(bus)
            seg = direction.segment_length_m(i)
            bus.segment_progress = min((distance - direction.stops[i].distance_m) / seg, 0.99)
            load = v.load if v.load is not None else None
            if load is not None and not 0 <= load <= config.capacity:
                raise ObservationError(f"vehicle {v.vehicle_id}: load {load} out of range")
            bus.onboard = sim._expected_onboard(v.direction, i, total=load)
        state.buses.append(bus)
    for d in (0, 1):
        direction = route.direction(d)
        for i in range(direction.n_stops - 1):
            observed = observations.waiting.get((d, i))
            if observed is not None and observed < 0:
                raise ObservationError(f"negative queue at stop {d}:{i}")
            typical = sim._rate_per_s(d, i) * direction.scheduled_headway_s / 2
            state.queues[d][i] = observed if observed is not None else round(typical)
    return sim
