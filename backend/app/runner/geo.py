"""Turn "metres along a direction" into a GPS position and heading on the route geometry."""

import bisect
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.gtfs.geometry import LocalPlane, LonLat


@dataclass(frozen=True)
class Located:
    lon: float
    lat: float
    heading_deg: float  # 0 = north, clockwise


class LineIndex:
    """A polyline with cumulative distances on the same local plane the importer used.

    Stop distances (`distance_along_m`) were measured on `LocalPlane.around(stops)`, so the
    same plane is used here; positions then line up with stops exactly.
    """

    def __init__(self, line: Sequence[LonLat], plane: LocalPlane) -> None:
        if len(line) < 2:
            raise ValueError("a route line needs at least two points")
        self._points = list(line)
        self._xy = [plane.xy(p) for p in self._points]
        self._cum = [0.0]
        for (ax, ay), (bx, by) in itertools.pairwise(self._xy):
            self._cum.append(self._cum[-1] + math.hypot(bx - ax, by - ay))

    @property
    def length_m(self) -> float:
        return self._cum[-1]

    def locate(self, distance_m: float) -> Located:
        d = min(max(distance_m, 0.0), self.length_m)
        i = min(max(bisect.bisect_right(self._cum, d) - 1, 0), len(self._points) - 2)
        # Zero-length pieces (repeated points) have no direction: step to the next real one.
        j = i
        while j < len(self._points) - 2 and self._cum[j + 1] - self._cum[j] <= 0:
            j += 1
        seg = self._cum[j + 1] - self._cum[j]
        t = 0.0 if seg <= 0 else min(max((d - self._cum[j]) / seg, 0.0), 1.0)
        (alon, alat), (blon, blat) = self._points[j], self._points[j + 1]
        (ax, ay), (bx, by) = self._xy[j], self._xy[j + 1]
        heading = math.degrees(math.atan2(bx - ax, by - ay)) % 360
        return Located(
            lon=alon + t * (blon - alon), lat=alat + t * (blat - alat), heading_deg=heading
        )
