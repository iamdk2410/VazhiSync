"""Simulation parameters and scenario events (B5's Scenario schema maps onto these).

Defaults are calibrated on route 32B with OSRM road-time segment shares (B6 recalibration):
with no control, 26/30 seeds first bunch between 15 and 30 simulated minutes and all by
32 min; trips take 34.8 / 36.9 min vs the timetable's 35 / 37; peak waits and crowding are
high, as in a bunched Chennai peak. See docs/progress.md (B4, B6).
"""

from dataclasses import dataclass, field

# Hourly demand multipliers (Chennai weekday shape: morning and evening peaks).
DEFAULT_DEMAND_PROFILE: dict[int, float] = {
    4: 0.15, 5: 0.35, 6: 0.6, 7: 0.85, 8: 1.0, 9: 1.0, 10: 0.8, 11: 0.65, 12: 0.6,
    13: 0.6, 14: 0.6, 15: 0.7, 16: 0.85, 17: 1.0, 18: 1.0, 19: 0.9, 20: 0.6, 21: 0.4,
    22: 0.2, 23: 0.1,
}  # fmt: skip


# ------------------------------------------------------------------------- events


@dataclass(frozen=True)
class Weather:
    """City-wide: travel slows by `speed_factor`, demand scales by `demand_factor`."""

    start_s: int
    end_s: int
    speed_factor: float = 1.0  # 0.75 = 25 % slower
    demand_factor: float = 1.0


@dataclass(frozen=True)
class Slowdown:
    """Traffic on part of one direction: segments from_stop..to_stop (indices) slow down."""

    direction: int
    from_stop: int
    to_stop: int
    start_s: int
    end_s: int
    speed_factor: float


@dataclass(frozen=True)
class DemandSurge:
    """Extra passengers at some stops (e.g. a match ending at Chepauk)."""

    direction: int
    stop_indices: tuple[int, ...]
    start_s: int
    end_s: int
    multiplier: float


@dataclass(frozen=True)
class Breakdown:
    """A bus stops where it is for `duration_s` (doors closed, no boarding)."""

    bus_id: str
    start_s: int
    duration_s: int


Event = Weather | Slowdown | DemandSurge | Breakdown


# ------------------------------------------------------------------------- config


@dataclass(frozen=True)
class SimConfig:
    start_time_s: int = 8 * 3600
    tick_s: float = 1.0
    fleet_size: int | None = None  # None: cycle time / scheduled headway
    capacity: int = 80
    # Dwell rule from the build plan: 10 s + 2.5 s per boarder + 1.5 s per alighter.
    dwell_base_s: float = 10.0
    board_s: float = 2.5
    alight_s: float = 1.5
    min_layover_s: float = 180.0
    short_turn_s: float = 120.0  # time to turn the bus round
    # Share of the timetabled trip spent moving (the rest is dwell), spread over segments by
    # road time. Calibrated so simulated 32B trips match the timetable (34.8 / 36.9 vs 35 / 37).
    run_share: float = 0.68
    max_speed_mps: float = 12.5  # 45 km/h cap on free-flow running
    travel_noise_sigma: float = 0.10  # log-normal spread of segment travel times
    base_arrivals_per_min: float = 1.3  # per stop, per direction, at demand multiplier 1.0
    demand_profile: dict[int, float] = field(default_factory=lambda: dict(DEFAULT_DEMAND_PROFILE))
    demand_multiplier: float = 1.0
    initial_spacing_jitter_s: float = 10.0  # real buses never start perfectly spaced
    bunching_ratio: float = 0.5  # headway < ratio x scheduled = bunched (CLAUDE.md config)
    closing_ratio: float = 0.75  # bunched pair recovers above this
    events: tuple[Event, ...] = ()

    def __post_init__(self) -> None:
        if self.tick_s <= 0 or self.capacity <= 0:
            raise ValueError("tick_s and capacity must be positive")
        if not 0 < self.bunching_ratio < self.closing_ratio < 1.5:
            raise ValueError("need 0 < bunching_ratio < closing_ratio")
        if not 0 < self.run_share <= 1:
            raise ValueError("run_share must be in (0, 1]")
        if self.fleet_size is not None and self.fleet_size < 2:
            raise ValueError("fleet_size must be at least 2")
