"""Headways between consecutive buses, from what GPS reported (rule 1: never the world's state).

Pure functions, shared by the world runner's bunching detector (B6) and avl-mcp's
`get_headways` tool (B7), so both always agree.

Headway is the gap to the bus ahead in the same direction, converted to time at the route's
timetabled average speed (length / scheduled trip time, dwell included), and compared with the
scheduled headway. Buses waiting in the terminal zone are not in service and are left out.
A pair involving a stale vehicle is reported as "stale" and never classified.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

TERMINAL_ZONE_M = 50.0  # within this of the first stop: waiting at the terminal

HeadwayStatus = Literal["ok", "closing", "bunched", "stale"]


@dataclass(frozen=True)
class Fix:
    """The latest GPS report for one vehicle."""

    vehicle_id: str
    direction_id: int
    distance_m: float  # along the direction's route geometry
    age_s: float  # seconds since the fix was recorded


@dataclass(frozen=True)
class DirectionInfo:
    first_stop_m: float
    scheduled_headway_s: float
    avg_speed_mps: float  # length / scheduled trip time

    def __post_init__(self) -> None:
        if self.scheduled_headway_s <= 0 or self.avg_speed_mps <= 0:
            raise ValueError("scheduled headway and average speed must be positive")


@dataclass(frozen=True)
class Headway:
    direction_id: int
    leader_id: str
    follower_id: str
    gap_m: float
    headway_s: float
    scheduled_headway_s: float
    ratio: float  # headway / scheduled headway
    status: HeadwayStatus


def classify(ratio: float, bunching_ratio: float, closing_ratio: float) -> HeadwayStatus:
    if ratio < bunching_ratio:
        return "bunched"
    if ratio < closing_ratio:
        return "closing"
    return "ok"


def compute_headways(
    fixes: Sequence[Fix],
    directions: dict[int, DirectionInfo],
    stale_after_s: float,
    bunching_ratio: float,
    closing_ratio: float,
) -> list[Headway]:
    """One Headway per consecutive in-service pair, per direction, leader first."""
    out: list[Headway] = []
    for d, info in sorted(directions.items()):
        in_service = sorted(
            (
                f
                for f in fixes
                if f.direction_id == d and f.distance_m > info.first_stop_m + TERMINAL_ZONE_M
            ),
            key=lambda f: f.distance_m,
            reverse=True,
        )
        for leader, follower in itertools.pairwise(in_service):
            gap = leader.distance_m - follower.distance_m
            headway = gap / info.avg_speed_mps
            ratio = headway / info.scheduled_headway_s
            stale = leader.age_s > stale_after_s or follower.age_s > stale_after_s
            out.append(
                Headway(
                    direction_id=d,
                    leader_id=leader.vehicle_id,
                    follower_id=follower.vehicle_id,
                    gap_m=round(gap, 1),
                    headway_s=round(headway, 1),
                    scheduled_headway_s=info.scheduled_headway_s,
                    ratio=round(ratio, 3),
                    status="stale" if stale else classify(ratio, bunching_ratio, closing_ratio),
                )
            )
    return out
