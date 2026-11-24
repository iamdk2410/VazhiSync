"""Passenger and service-regularity metrics, accumulated as the simulation runs."""

import statistics
from dataclasses import dataclass, field


@dataclass
class MetricsState:
    total_wait_s: float = 0.0  # passenger-seconds spent waiting at stops
    boarded: int = 0
    alighted: int = 0
    left_behind: int = 0  # passenger-denials: queue still waiting when a full bus left
    overcarried: int = 0  # carried past their stop by a skip-stop
    displaced: int = 0  # put off early by a short-turn
    bunching_count: int = 0  # bunching episodes (a pair entering the bunched state)
    first_bunching_s: float | None = None
    # Observed arrival headways at stops, per direction (for headway variation).
    headways: dict[int, list[float]] = field(default_factory=lambda: {0: [], 1: []})


@dataclass(frozen=True)
class Metrics:
    total_wait_s: float
    boarded: int
    alighted: int
    left_behind: int
    overcarried: int
    displaced: int
    bunching_count: int
    first_bunching_s: float | None
    headway_cv: float | None  # coefficient of variation of stop headways (0 = perfectly even)
    elapsed_s: float

    @property
    def passenger_wait_minutes(self) -> float:
        return self.total_wait_s / 60

    @property
    def avg_wait_s(self) -> float | None:
        return self.total_wait_s / self.boarded if self.boarded else None


def headway_cv(headways: dict[int, list[float]]) -> float | None:
    """Mean over directions of std/mean of observed headways."""
    cvs = []
    for values in headways.values():
        if len(values) >= 2:
            mean = statistics.fmean(values)
            if mean > 0:
                cvs.append(statistics.pstdev(values) / mean)
    return statistics.fmean(cvs) if cvs else None


def freeze(state: MetricsState, elapsed_s: float) -> Metrics:
    return Metrics(
        total_wait_s=round(state.total_wait_s, 1),
        boarded=state.boarded,
        alighted=state.alighted,
        left_behind=state.left_behind,
        overcarried=state.overcarried,
        displaced=state.displaced,
        bunching_count=state.bunching_count,
        first_bunching_s=state.first_bunching_s,
        headway_cv=headway_cv(state.headways),
        elapsed_s=elapsed_s,
    )
