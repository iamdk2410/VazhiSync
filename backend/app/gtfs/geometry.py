"""Small planar geometry helpers for one city-scale route (no external GIS dependency).

Coordinates are (lon, lat) in WGS84. For distances we project onto a local equirectangular
plane centred on the route; over ~20 km the error is well under 0.1 %, far below GPS noise.
"""

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8

LonLat = tuple[float, float]


@dataclass(frozen=True)
class Projection:
    """Where a point falls on a polyline."""

    distance_along_m: float  # from the polyline's start
    offset_m: float  # perpendicular distance from the polyline


class LocalPlane:
    """Equirectangular projection to metres around a reference latitude."""

    def __init__(self, ref_lat: float) -> None:
        self._kx = EARTH_RADIUS_M * math.cos(math.radians(ref_lat)) * math.pi / 180
        self._ky = EARTH_RADIUS_M * math.pi / 180

    @classmethod
    def around(cls, points: Sequence[LonLat]) -> "LocalPlane":
        if not points:
            raise ValueError("need at least one point")
        return cls(sum(lat for _, lat in points) / len(points))

    def xy(self, p: LonLat) -> tuple[float, float]:
        return p[0] * self._kx, p[1] * self._ky


def haversine_m(a: LonLat, b: LonLat) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def polyline_length_m(line: Sequence[LonLat]) -> float:
    return sum(haversine_m(a, b) for a, b in itertools.pairwise(line))


def project_onto(line: Sequence[LonLat], point: LonLat, plane: LocalPlane) -> Projection:
    """Closest point on `line` to `point`: distance along the line and perpendicular offset."""
    if len(line) < 2:
        raise ValueError("a polyline needs at least two points")
    px, py = plane.xy(point)
    best = Projection(distance_along_m=0.0, offset_m=math.inf)
    walked = 0.0
    for a, b in itertools.pairwise(line):
        ax, ay = plane.xy(a)
        bx, by = plane.xy(b)
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg**2))
        offset = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if offset < best.offset_m:
            best = Projection(distance_along_m=walked + t * seg, offset_m=offset)
        walked += seg
    return best


def project_in_order(
    line: Sequence[LonLat], points: Sequence[LonLat], plane: LocalPlane, window_m: float = 400
) -> list[Projection]:
    """Project an ordered stop list, preferring matches just ahead of the previous stop.

    A route that passes the same street twice (or loops at a terminus) can make a later stop
    snap to an earlier stretch of road. Restricting each search to the part of the line
    after the previous stop (minus a small backwards `window_m`) keeps the order honest;
    the validator then still rejects genuinely out-of-order stops.
    """
    results: list[Projection] = []
    floor = 0.0
    for point in points:
        sub, start = _tail_from(line, plane, max(0.0, floor - window_m))
        proj = project_onto(sub, point, plane)
        results.append(Projection(start + proj.distance_along_m, proj.offset_m))
        floor = results[-1].distance_along_m
    return results


def _tail_from(
    line: Sequence[LonLat], plane: LocalPlane, from_m: float
) -> tuple[list[LonLat], float]:
    """The part of `line` from `from_m` metres onwards, and where it starts (in metres)."""
    walked = 0.0
    seg = 0.0
    for i, (a, b) in enumerate(itertools.pairwise(line)):
        ax, ay = plane.xy(a)
        bx, by = plane.xy(b)
        seg = math.hypot(bx - ax, by - ay)
        if walked + seg >= from_m:
            return list(line[i:]), walked
        walked += seg
    # Past the end: the last segment, which starts one segment before the end.
    return list(line[-2:]), walked - seg


def slice_line(
    line: Sequence[LonLat], from_m: float, to_m: float, plane: LocalPlane
) -> list[LonLat]:
    """The part of `line` between two distances along it (endpoints interpolated)."""
    if to_m < from_m - 1e-6:
        raise ValueError("to_m must be >= from_m")
    from_m = min(from_m, to_m)  # tolerate float rounding when both sit at the same point
    out: list[LonLat] = []
    walked = 0.0
    for a, b in itertools.pairwise(line):
        ax, ay = plane.xy(a)
        bx, by = plane.xy(b)
        seg = math.hypot(bx - ax, by - ay)
        start, end = walked, walked + seg
        if end >= from_m and start <= to_m and seg > 0:
            t0 = max(0.0, (from_m - start) / seg)
            t1 = min(1.0, (to_m - start) / seg)
            p0 = (a[0] + t0 * (b[0] - a[0]), a[1] + t0 * (b[1] - a[1]))
            p1 = (a[0] + t1 * (b[0] - a[0]), a[1] + t1 * (b[1] - a[1]))
            if not out:
                out.append(p0)
            out.append(p1)
        walked = end
    return out


def planar_length_m(line: Sequence[LonLat], plane: LocalPlane) -> float:
    """Length on the local plane: the same units as Projection.distance_along_m."""
    total = 0.0
    for a, b in itertools.pairwise(line):
        ax, ay = plane.xy(a)
        bx, by = plane.xy(b)
        total += math.hypot(bx - ax, by - ay)
    return total


def as_lonlats(coords: Sequence[Sequence[float]]) -> list[LonLat]:
    """Typed (lon, lat) tuples from GeoJSON-style coordinate lists."""
    return [(float(c[0]), float(c[1])) for c in coords]
