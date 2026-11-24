"""Timetable maths: GTFS times, scheduled headways and per-stop time offsets."""

import itertools
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

PEAK_START_HOUR = 7
PEAK_END_HOUR = 10  # exclusive


@dataclass(frozen=True)
class HourHeadway:
    hour: int
    headway_seconds: int
    departures: int


def parse_gtfs_time(value: str) -> int:
    """'HH:MM:SS' to seconds after midnight. GTFS allows hours >= 24 (after-midnight trips)."""
    parts = value.strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"invalid GTFS time: {value!r}")
    h, m, s = (int(p) for p in parts)
    if m > 59 or s > 59 or h > 47:
        raise ValueError(f"invalid GTFS time: {value!r}")
    return h * 3600 + m * 60 + s


def format_gtfs_time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def unique_sorted(departures: Iterable[int]) -> list[int]:
    """Departure times with duplicates removed (the feed lists some trips twice)."""
    return sorted(set(departures))


def headway_profile(departures: Iterable[int]) -> list[HourHeadway]:
    """Median gap to the next departure, grouped by the hour of the earlier departure."""
    times = unique_sorted(departures)
    gaps: dict[int, list[int]] = defaultdict(list)
    for a, b in itertools.pairwise(times):
        gaps[a // 3600].append(b - a)
    return [
        HourHeadway(hour=h, headway_seconds=round(statistics.median(g)), departures=len(g))
        for h, g in sorted(gaps.items())
    ]


def peak_headway_seconds(profile: Sequence[HourHeadway]) -> int | None:
    """Median of the hourly headways in the morning peak (07:00-10:00)."""
    peak = [p.headway_seconds for p in profile if PEAK_START_HOUR <= p.hour < PEAK_END_HOUR]
    return round(statistics.median(peak)) if peak else None


def median_offsets(trips: Sequence[Sequence[int]]) -> list[int]:
    """Per-stop median time since the first stop, across trips with the same stop pattern."""
    if not trips:
        raise ValueError("need at least one trip")
    n = len(trips[0])
    if any(len(t) != n for t in trips):
        raise ValueError("all trips must have the same number of stops")
    return [round(statistics.median(t[i] - t[0] for t in trips)) for i in range(n)]
