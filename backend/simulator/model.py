"""Static route description the simulator runs on (pure data, no I/O).

Built by the app from the imported GTFS route (see app/gtfs/sim_route.py) or loaded from a
JSON fixture in tests. Distances are metres along each direction's road geometry.
"""

import math
from dataclasses import dataclass, field
from typing import Any

EARTH_RADIUS_M = 6_371_008.8
MAX_TURN_PAIR_DISTANCE_M = 400.0  # a short-turn needs an opposite-direction stop this close


class RouteModelError(ValueError):
    pass


@dataclass(frozen=True)
class StopModel:
    stop_id: str
    name: str
    lon: float
    lat: float
    distance_m: float  # along this direction's geometry
    scheduled_offset_s: int  # timetable time from the first stop


@dataclass(frozen=True)
class DirectionModel:
    direction_id: int
    headsign: str
    length_m: float
    scheduled_headway_s: int
    stops: tuple[StopModel, ...]

    def __post_init__(self) -> None:
        if len(self.stops) < 2:
            raise RouteModelError(f"direction {self.direction_id}: needs at least two stops")
        for a, b in zip(self.stops, self.stops[1:], strict=False):
            if b.distance_m <= a.distance_m:
                raise RouteModelError(
                    f"direction {self.direction_id}: stop {b.stop_id} is not after {a.stop_id}"
                )
            if b.scheduled_offset_s < a.scheduled_offset_s:
                raise RouteModelError(f"direction {self.direction_id}: timetable goes backwards")
        if self.scheduled_headway_s <= 0:
            raise RouteModelError(f"direction {self.direction_id}: headway must be positive")

    @property
    def n_stops(self) -> int:
        return len(self.stops)

    @property
    def scheduled_run_s(self) -> int:
        return self.stops[-1].scheduled_offset_s - self.stops[0].scheduled_offset_s

    def segment_length_m(self, i: int) -> float:
        """Length of the segment from stop i to stop i+1."""
        return self.stops[i + 1].distance_m - self.stops[i].distance_m

    def segment_scheduled_s(self, i: int) -> int:
        return self.stops[i + 1].scheduled_offset_s - self.stops[i].scheduled_offset_s

    def stop_index_at(self, distance_m: float) -> int:
        """Index of the last stop at or before `distance_m` (clamped to the route)."""
        index = 0
        for i, stop in enumerate(self.stops):
            if stop.distance_m <= distance_m + 1e-6:
                index = i
        return min(index, self.n_stops - 1)


@dataclass(frozen=True)
class RouteModel:
    route_id: str
    directions: tuple[DirectionModel, DirectionModel]
    # turn_pairs[d][i] = index of the nearest opposite-direction stop, or None if too far.
    turn_pairs: tuple[tuple[int | None, ...], tuple[int | None, ...]] = field(init=False)

    def __post_init__(self) -> None:
        if [d.direction_id for d in self.directions] != [0, 1]:
            raise RouteModelError("directions must be [0, 1] in order")
        pairs = tuple(
            tuple(_nearest(stop, self.directions[1 - d].stops) for stop in self.directions[d].stops)
            for d in (0, 1)
        )
        object.__setattr__(self, "turn_pairs", pairs)

    def direction(self, d: int) -> DirectionModel:
        return self.directions[d]

    # ----------------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "directions": [
                {
                    "direction_id": d.direction_id,
                    "headsign": d.headsign,
                    "length_m": d.length_m,
                    "scheduled_headway_s": d.scheduled_headway_s,
                    "stops": [
                        {
                            "stop_id": s.stop_id,
                            "name": s.name,
                            "lon": s.lon,
                            "lat": s.lat,
                            "distance_m": s.distance_m,
                            "scheduled_offset_s": s.scheduled_offset_s,
                        }
                        for s in d.stops
                    ],
                }
                for d in self.directions
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouteModel":
        try:
            directions = tuple(
                DirectionModel(
                    direction_id=int(d["direction_id"]),
                    headsign=str(d["headsign"]),
                    length_m=float(d["length_m"]),
                    scheduled_headway_s=int(d["scheduled_headway_s"]),
                    stops=tuple(
                        StopModel(
                            stop_id=str(s["stop_id"]),
                            name=str(s["name"]),
                            lon=float(s["lon"]),
                            lat=float(s["lat"]),
                            distance_m=float(s["distance_m"]),
                            scheduled_offset_s=int(s["scheduled_offset_s"]),
                        )
                        for s in d["stops"]
                    ),
                )
                for d in data["directions"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteModelError(f"invalid route data: {exc}") from None
        if len(directions) != 2:
            raise RouteModelError("a route needs exactly two directions")
        return cls(route_id=str(data["route_id"]), directions=(directions[0], directions[1]))


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _nearest(stop: StopModel, others: tuple[StopModel, ...]) -> int | None:
    best, best_d = None, math.inf
    for j, other in enumerate(others):
        dist = haversine_m(stop.lon, stop.lat, other.lon, other.lat)
        if dist < best_d:
            best, best_d = j, dist
    return best if best_d <= MAX_TURN_PAIR_DISTANCE_M else None
