"""The bus simulator: buses cycling a two-direction route, passengers counted per stop.

One `Simulation` instance is either the *world* (ground truth, B6) or a *what-if model*
built only from observations (`from_observations`, rule 1). Time advances in fixed ticks
(default 1 s); every random draw comes from a named stream, so a seed fully determines a run.
"""

import copy
import random
from dataclasses import dataclass, field
from typing import Literal

from simulator.actions import Action, ActionError, DoNothing, Hold, ShortTurn, SkipStops
from simulator.config import Breakdown, DemandSurge, SimConfig, Slowdown, Weather
from simulator.metrics import Metrics, MetricsState, freeze
from simulator.model import DirectionModel, RouteModel
from simulator.rng import lognormal_factor, poisson, stream

BusStatus = Literal["running", "dwelling", "holding", "layover"]


@dataclass
class BusState:
    bus_id: str
    direction: int
    stop_index: int  # last stop reached; when running, the segment is stop_index -> +1
    distance_m: float
    status: BusStatus
    capacity: int
    onboard: list[int]  # passengers by destination stop index (current direction)
    segment_progress: float = 0.0  # 0..1 along the current segment
    segment_time_s: float = 1.0  # nominal travel time drawn for the current segment
    dwell_remaining_s: float = 0.0
    hold_remaining_s: float = 0.0
    pending_hold_s: float = 0.0  # a hold requested while running: applied at the next stop
    layover_remaining_s: float = 0.0
    breakdown_remaining_s: float = 0.0
    skip: set[int] = field(default_factory=set)
    short_turn_at: int | None = None
    bunched: bool = False  # currently bunched behind the bus ahead (hysteresis)

    @property
    def load(self) -> int:
        return sum(self.onboard)


@dataclass
class SimState:
    time_s: float
    elapsed_s: float
    buses: list[BusState]
    queues: list[list[int]]  # [direction][stop] passengers waiting
    last_arrival: list[list[float | None]]  # [direction][stop] time the last bus got there
    metrics: MetricsState
    streams: dict[str, random.Random]
    breakdowns_started: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class BusView:
    bus_id: str
    direction: int
    stop_index: int
    distance_m: float
    status: BusStatus
    load: int
    capacity: int
    broken_down: bool


@dataclass(frozen=True)
class SimSnapshot:
    time_s: float
    elapsed_s: float
    buses: tuple[BusView, ...]
    queues: tuple[tuple[int, ...], tuple[int, ...]]
    metrics: Metrics


class Simulation:
    def __init__(self, route: RouteModel, config: SimConfig, seed: int, state: SimState) -> None:
        self.route = route
        self.config = config
        self.seed = seed
        self.state = state

    # ------------------------------------------------------------------ construction

    @classmethod
    def new(cls, route: RouteModel, config: SimConfig | None = None, seed: int = 0) -> "Simulation":
        """A fresh world: buses spread evenly round the cycle, queues and loads warmed up."""
        config = config or SimConfig()
        sim = cls(route, config, seed, _empty_state(route, config, seed))
        init = sim._stream("init")
        cycle, legs = sim._cycle()
        n = config.fleet_size or max(2, round(cycle / sim._mean_headway()))
        for k in range(n):
            phase = (k * cycle / n + init.uniform(-1, 1) * config.initial_spacing_jitter_s) % cycle
            sim.state.buses.append(sim._bus_at_phase(f"{route.route_id}-{k + 1:02d}", phase, legs))
        for d in (0, 1):
            direction = route.direction(d)
            for i in range(direction.n_stops - 1):
                expected = sim._rate_per_s(d, i) * direction.scheduled_headway_s / 2
                sim.state.queues[d][i] = poisson(init, expected)
        return sim

    def copy(self) -> "Simulation":
        """An independent copy (route and config are immutable and shared)."""
        return Simulation(self.route, self.config, self.seed, copy.deepcopy(self.state))

    # ------------------------------------------------------------------ public API

    def step(self, dt: float) -> None:
        """Advance the simulation by `dt` seconds (rounded to whole ticks)."""
        for _ in range(max(0, round(dt / self.config.tick_s))):
            self._tick(self.config.tick_s)

    def apply(self, action: Action) -> None:
        """Apply a corrective action. Raises ActionError if invalid for the bus's position."""
        if isinstance(action, DoNothing):
            return
        bus = self._bus(action.bus_id)
        if bus.breakdown_remaining_s > 0:
            raise ActionError(f"{bus.bus_id} is broken down")
        direction = self.route.direction(bus.direction)
        if isinstance(action, Hold):
            if bus.status in ("dwelling", "holding"):
                bus.hold_remaining_s += action.seconds
            elif bus.status == "layover":
                bus.layover_remaining_s += action.seconds
            else:
                bus.pending_hold_s += action.seconds
        elif isinstance(action, SkipStops):
            if bus.status == "layover":
                raise ActionError(f"{bus.bus_id} is at a terminal; nothing to skip yet")
            for i in action.stop_indices:
                if not bus.stop_index < i < direction.n_stops - 1:
                    raise ActionError(
                        f"stop {i} is not an upcoming intermediate stop for {bus.bus_id}"
                    )
            bus.skip.update(action.stop_indices)
        elif isinstance(action, ShortTurn):
            if bus.status == "layover":
                raise ActionError(f"{bus.bus_id} is at a terminal")
            i = action.at_stop
            if not bus.stop_index < i < direction.n_stops - 1:
                raise ActionError(f"stop {i} is not an upcoming intermediate stop")
            pair = self.route.turn_pairs[bus.direction][i]
            other = self.route.direction(1 - bus.direction)
            if pair is None or pair >= other.n_stops - 1:
                raise ActionError(f"no opposite-direction stop near stop {i} to turn at")
            bus.short_turn_at = i

    def snapshot(self) -> SimSnapshot:
        s = self.state
        return SimSnapshot(
            time_s=s.time_s,
            elapsed_s=s.elapsed_s,
            buses=tuple(
                BusView(
                    bus_id=b.bus_id,
                    direction=b.direction,
                    stop_index=b.stop_index,
                    distance_m=round(b.distance_m, 1),
                    status=b.status,
                    load=b.load,
                    capacity=b.capacity,
                    broken_down=b.breakdown_remaining_s > 0,
                )
                for b in s.buses
            ),
            queues=(tuple(s.queues[0]), tuple(s.queues[1])),
            metrics=self.metrics(),
        )

    def metrics(self) -> Metrics:
        return freeze(self.state.metrics, self.state.elapsed_s)

    def reset_metrics(self) -> None:
        self.state.metrics = MetricsState()
        self.state.elapsed_s = 0.0

    # ------------------------------------------------------------------ the tick

    def _tick(self, dt: float) -> None:
        s = self.state
        self._start_breakdowns()
        for d in (0, 1):
            direction = self.route.direction(d)
            for i in range(direction.n_stops - 1):  # nobody boards at the final stop
                lam = self._rate_per_s(d, i) * dt
                if lam > 0:
                    s.queues[d][i] += poisson(self._stream(f"arr:{d}:{i}"), lam)
        s.metrics.total_wait_s += dt * (sum(s.queues[0]) + sum(s.queues[1]))
        for bus in s.buses:
            self._advance(bus, dt)
        s.time_s += dt
        s.elapsed_s += dt

    def _advance(self, bus: BusState, dt: float) -> None:
        if bus.breakdown_remaining_s > 0:
            bus.breakdown_remaining_s -= dt
            return
        if bus.status == "layover":
            bus.layover_remaining_s -= dt
            if bus.layover_remaining_s <= 0:
                self._serve(bus)
        elif bus.status == "dwelling":
            bus.dwell_remaining_s += self.config.board_s * self._board_late(bus)
            bus.dwell_remaining_s -= dt
            if bus.dwell_remaining_s <= 0:
                if bus.hold_remaining_s > 0:
                    bus.status = "holding"
                else:
                    self._depart(bus)
        elif bus.status == "holding":
            self._board_late(bus)  # boarding continues, covered by the hold
            bus.hold_remaining_s -= dt
            if bus.hold_remaining_s <= 0:
                self._depart(bus)
        else:  # running
            factor = self._speed_factor(bus.direction, bus.stop_index)
            bus.segment_progress += dt * factor / bus.segment_time_s
            direction = self.route.direction(bus.direction)
            a = direction.stops[bus.stop_index].distance_m
            seg = direction.segment_length_m(bus.stop_index)
            bus.distance_m = a + min(bus.segment_progress, 1.0) * seg
            if bus.segment_progress >= 1.0:
                self._arrive(bus, bus.stop_index + 1)

    # ------------------------------------------------------------------ stops

    def _arrive(self, bus: BusState, i: int) -> None:
        direction = self.route.direction(bus.direction)
        bus.stop_index = i
        bus.distance_m = direction.stops[i].distance_m
        bus.segment_progress = 0.0
        m = self.state.metrics
        if i == direction.n_stops - 1 or bus.short_turn_at == i:
            self._record_arrival(bus, bus.direction, i)
            turning = bus.short_turn_at == i and i < direction.n_stops - 1
            if turning:
                m.displaced += sum(bus.onboard[i + 1 :])
            m.alighted += bus.load
            new_d = 1 - bus.direction
            new_direction = self.route.direction(new_d)
            pair = self.route.turn_pairs[bus.direction][i] if turning else 0
            if pair is None:  # validated in apply(); guard against a route change
                pair = 0
            bus.direction, bus.stop_index = new_d, pair
            bus.distance_m = new_direction.stops[pair].distance_m
            bus.onboard = [0] * new_direction.n_stops
            bus.skip.clear()
            bus.short_turn_at = None
            bus.status = "layover"
            layover = self.config.short_turn_s if turning else self.config.min_layover_s
            bus.layover_remaining_s = layover + bus.pending_hold_s
            bus.pending_hold_s = 0.0
        elif i in bus.skip:
            bus.skip.discard(i)
            self._record_arrival(bus, bus.direction, i)
            carried = bus.onboard[i]
            bus.onboard[i + 1] += carried
            bus.onboard[i] = 0
            m.overcarried += carried
            self._depart(bus)
        else:
            self._serve(bus)

    def _serve(self, bus: BusState) -> None:
        """Doors open at the current stop: alight, board, set the dwell."""
        d, i = bus.direction, bus.stop_index
        self._record_arrival(bus, d, i)
        m, c = self.state.metrics, self.config
        alight = bus.onboard[i]
        bus.onboard[i] = 0
        m.alighted += alight
        board = self._board(bus, limit_to_space=True, count_left_behind=True)
        hold = bus.pending_hold_s
        bus.pending_hold_s = 0.0
        if alight or board or hold:
            bus.status = "dwelling"
            bus.dwell_remaining_s = c.dwell_base_s + c.board_s * board + c.alight_s * alight
            bus.hold_remaining_s += hold
        else:
            self._depart(bus)  # nobody waiting or getting off: drive past

    def _board(self, bus: BusState, limit_to_space: bool, count_left_behind: bool) -> int:
        d, i = bus.direction, bus.stop_index
        direction = self.route.direction(d)
        if i >= direction.n_stops - 1:
            return 0
        queue = self.state.queues[d][i]
        space = bus.capacity - bus.load if limit_to_space else queue
        board = max(0, min(queue, space))
        if count_left_behind and queue > board:
            self.state.metrics.left_behind += queue - board
        if board:
            self.state.queues[d][i] -= board
            rng = self._stream(f"dest:{d}:{i}")
            remaining = direction.n_stops - 1 - i
            for _ in range(board):
                bus.onboard[i + 1 + int(rng.random() * remaining)] += 1
            self.state.metrics.boarded += board
        return board

    def _board_late(self, bus: BusState) -> int:
        """Passengers who reach the stop while the doors are still open."""
        return self._board(bus, limit_to_space=True, count_left_behind=False)

    def _depart(self, bus: BusState) -> None:
        direction = self.route.direction(bus.direction)
        bus.status = "running"
        bus.dwell_remaining_s = bus.hold_remaining_s = 0.0
        bus.segment_progress = 0.0
        noise = lognormal_factor(self._stream(f"bus:{bus.bus_id}"), self.config.travel_noise_sigma)
        bus.segment_time_s = self._base_run_s(direction, bus.stop_index) * noise

    def _record_arrival(self, bus: BusState, d: int, i: int) -> None:
        s, c = self.state, self.config
        last = s.last_arrival[d][i]
        s.last_arrival[d][i] = s.time_s
        if last is None:
            return
        headway = s.time_s - last
        s.metrics.headways[d].append(headway)
        scheduled = self.route.direction(d).scheduled_headway_s
        if headway < c.bunching_ratio * scheduled:
            if not bus.bunched:
                bus.bunched = True
                s.metrics.bunching_count += 1
                if s.metrics.first_bunching_s is None:
                    s.metrics.first_bunching_s = s.elapsed_s
        elif headway > c.closing_ratio * scheduled:
            bus.bunched = False

    # ------------------------------------------------------------------ rates and events

    def _rate_per_s(self, d: int, i: int) -> float:
        c, t = self.config, self.state.time_s
        if i >= self.route.direction(d).n_stops - 1:
            return 0.0
        hour = int(t // 3600) % 24
        rate = c.base_arrivals_per_min / 60 * c.demand_profile.get(hour, 0.0) * c.demand_multiplier
        for e in c.events:
            if isinstance(e, Weather) and e.start_s <= t < e.end_s:
                rate *= e.demand_factor
            elif (
                isinstance(e, DemandSurge)
                and e.direction == d
                and i in e.stop_indices
                and e.start_s <= t < e.end_s
            ):
                rate *= e.multiplier
        return rate

    def _speed_factor(self, d: int, segment: int) -> float:
        t, factor = self.state.time_s, 1.0
        for e in self.config.events:
            if (isinstance(e, Weather) and e.start_s <= t < e.end_s) or (
                isinstance(e, Slowdown)
                and e.direction == d
                and e.from_stop <= segment < e.to_stop
                and e.start_s <= t < e.end_s
            ):
                factor *= e.speed_factor
        return max(factor, 0.05)

    def _start_breakdowns(self) -> None:
        s = self.state
        for e in self.config.events:
            if (
                isinstance(e, Breakdown)
                and e.bus_id not in s.breakdowns_started
                and e.start_s <= s.time_s
            ):
                s.breakdowns_started.add(e.bus_id)
                for bus in s.buses:
                    if bus.bus_id == e.bus_id:
                        bus.breakdown_remaining_s = e.duration_s

    # ------------------------------------------------------------------ helpers

    def _base_run_s(self, direction: DirectionModel, i: int) -> float:
        """Free-flow running time for segment i (timetable share, capped by max speed)."""
        scheduled = direction.segment_scheduled_s(i) * self.config.run_share
        fastest = direction.segment_length_m(i) / self.config.max_speed_mps
        return max(scheduled, fastest, 5.0)

    def _stream(self, name: str) -> random.Random:
        streams = self.state.streams
        if name not in streams:
            streams[name] = stream(self.seed, name)
        return streams[name]

    def _bus(self, bus_id: str) -> BusState:
        for bus in self.state.buses:
            if bus.bus_id == bus_id:
                return bus
        raise ActionError(f"unknown bus {bus_id}")

    def _mean_headway(self) -> float:
        return sum(d.scheduled_headway_s for d in self.route.directions) / 2

    def _cycle(self) -> tuple[float, list[tuple[str, int, float]]]:
        """Scheduled cycle: run dir 0, layover, run dir 1, layover. Returns (length, legs)."""
        legs: list[tuple[str, int, float]] = []
        for d in (0, 1):
            legs.append(("run", d, float(self.route.direction(d).scheduled_run_s)))
            legs.append(("layover", 1 - d, self.config.min_layover_s))
        return sum(length for _, _, length in legs), legs

    def _bus_at_phase(
        self, bus_id: str, phase: float, legs: list[tuple[str, int, float]]
    ) -> BusState:
        leg = 0
        while leg < len(legs) - 1 and phase >= legs[leg][2]:
            phase -= legs[leg][2]
            leg += 1
        kind, d, length = legs[leg]
        direction = self.route.direction(d)
        bus = BusState(
            bus_id=bus_id,
            direction=d,
            stop_index=0,
            distance_m=0.0,
            status="layover",
            capacity=self.config.capacity,
            onboard=[0] * direction.n_stops,
        )
        if kind == "layover":
            bus.layover_remaining_s = length - phase
            return bus
        offset0 = direction.stops[0].scheduled_offset_s
        i = 0
        while (
            i < direction.n_stops - 2
            and direction.stops[i + 1].scheduled_offset_s - offset0 <= phase
        ):
            i += 1
        seg_sched = max(direction.segment_scheduled_s(i), 1)
        frac = min(
            max((phase - (direction.stops[i].scheduled_offset_s - offset0)) / seg_sched, 0.0), 0.99
        )
        bus.stop_index = i
        self._depart(bus)
        bus.segment_progress = frac
        bus.distance_m = direction.stops[i].distance_m + frac * direction.segment_length_m(i)
        bus.onboard = self._expected_onboard(d, i)
        return bus

    def _expected_onboard(self, d: int, i: int, total: int | None = None) -> list[int]:
        """Typical load profile at stop i: boardings upstream, destinations downstream.

        Used to warm up a fresh world and, in `from_observations`, to spread an observed
        load over destinations (the observer never sees real passenger destinations).
        """
        direction = self.route.direction(d)
        n = direction.n_stops
        weights = [0.0] * n
        for k in range(i + 1):
            boarded = self._rate_per_s(d, k) * direction.scheduled_headway_s
            for j in range(i + 1, n):
                weights[j] += boarded / (n - 1 - k)
        expected = sum(weights)
        target = expected if total is None else total
        if target <= 0:
            return [0] * n
        if expected <= 0:  # no demand model to go on: spread evenly downstream
            weights = [1.0 if j > i else 0.0 for j in range(n)]
            expected = float(n - 1 - i)
        scaled = [w * target / expected for w in weights]
        onboard = [int(x) for x in scaled]
        # Hand out the rounding remainder to the largest fractions, deterministically.
        short = round(target) - sum(onboard)
        for j in sorted(range(n), key=lambda j: scaled[j] - onboard[j], reverse=True)[
            : max(short, 0)
        ]:
            onboard[j] += 1
        cap = self.config.capacity
        while sum(onboard) > cap:
            j = max(range(n), key=lambda j: onboard[j])
            onboard[j] -= 1
        return onboard


def _empty_state(route: RouteModel, config: SimConfig, seed: int) -> SimState:
    return SimState(
        time_s=float(config.start_time_s),
        elapsed_s=0.0,
        buses=[],
        queues=[[0] * route.direction(d).n_stops for d in (0, 1)],
        last_arrival=[[None] * route.direction(d).n_stops for d in (0, 1)],
        metrics=MetricsState(),
        streams={},
    )
