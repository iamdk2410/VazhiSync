"""One-off: snap each direction to the road network, stop pair by stop pair, and cache it.

    uv run python -m app.gtfs.snap_shape --route 32B

Each consecutive stop pair is routed on its own (through any via points in
`<route>.waypoints.json`), so every stretch gets its own road distance, free-flow duration and
`geometry_source`. A stretch whose road/straight ratio exceeds the detour limit is flagged and
drawn straight (fix it with waypoints). Three to four short-turn connections (a bus turning
round to the opposite direction mid-route) are routed too and saved to `<route>.turns.geojson`.

Sends only the route's public stop coordinates to an OSRM server (default: the public demo
server). Results are committed under backend/data/gtfs/routes/, so imports, tests and CI never
call the network. Map data (c) OpenStreetMap contributors, ODbL.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from app.gtfs.builder import SEGMENT_DETOUR_MIN_M, SEGMENT_DETOUR_RATIO_MAX, dominant_pattern
from app.gtfs.feed import FeedError, GtfsZip
from app.gtfs.geometry import LonLat, as_lonlats, haversine_m, polyline_length_m
from app.gtfs.route_config import (
    ROUTES_DIR,
    ConfigError,
    GeometrySource,
    SegmentInfo,
    load_route_config,
    load_waypoints,
)
from simulator.model import MAX_TURN_PAIR_DISTANCE_M

DEFAULT_OSRM = "https://router.project-osrm.org"
USER_AGENT = "vazhisync-gtfs-snap/0.2"
TIMEOUT_SECONDS = 30
ATTEMPTS = 3  # transient errors only (rule 7)
ATTRIBUTION = "Map data (c) OpenStreetMap contributors"
TURN_FRACTIONS = (1 / 3, 2 / 3)  # where along each direction the short-turn connections are


class SnapError(Exception):
    pass


class _OsrmGeometry(BaseModel):
    type: Literal["LineString"]
    coordinates: Annotated[list[tuple[float, float]], Field(min_length=2)]


class OsrmRoute(BaseModel):
    geometry: _OsrmGeometry
    distance: float
    duration: float


class _OsrmResponse(BaseModel):
    code: str
    routes: list[OsrmRoute] = []
    message: str | None = None


def osrm_routes(base_url: str, points: list[LonLat], alternatives: bool = False) -> list[OsrmRoute]:
    coords = ";".join(f"{lon:.6f},{lat:.6f}" for lon, lat in points)
    url = (
        f"{base_url.rstrip('/')}/route/v1/driving/{coords}?overview=full&geometries=geojson"
        f"&alternatives={'true' if alternatives else 'false'}"
    )
    if not url.startswith("https://"):
        raise SnapError("OSRM base URL must be https")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
                body = resp.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == ATTEMPTS:
                raise SnapError(f"OSRM HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == ATTEMPTS:
                raise SnapError(f"OSRM unreachable: {exc}") from None
        time.sleep(2**attempt)
    try:
        parsed = _OsrmResponse.model_validate_json(body)
    except ValidationError as exc:
        raise SnapError(f"unexpected OSRM response: {exc}") from None
    if parsed.code != "Ok" or not parsed.routes:
        raise SnapError(f"OSRM returned {parsed.code}: {parsed.message}")
    return parsed.routes


# A router takes points (and whether alternatives are wanted) and returns candidate routes.
# Production uses OSRM over HTTP; tests pass a fake.
Router = Callable[[list[LonLat], bool], list[OsrmRoute]]


def osrm_router(base_url: str, pause_s: float = 1.0) -> Router:
    def route(points: list[LonLat], alternatives: bool) -> list[OsrmRoute]:
        time.sleep(pause_s)  # be polite to the shared demo server
        return osrm_routes(base_url, points, alternatives)

    return route


def snap_pair(
    router: Router, a: LonLat, b: LonLat, via: list[LonLat]
) -> tuple[list[LonLat], float, float, GeometrySource, bool]:
    """Route one stop-to-stop stretch: (line, road_m, duration_s, source, flagged)."""
    straight = haversine_m(a, b)
    source: GeometrySource
    if via:
        best = router([a, *via, b], False)[0]
        source = "waypoints"
    else:
        best = min(router([a, b], True), key=lambda r: r.distance)
        source = "osrm-pair"
    flagged = (
        best.distance > SEGMENT_DETOUR_MIN_M
        and straight > 0
        and best.distance / straight > SEGMENT_DETOUR_RATIO_MAX
    )
    if flagged and not via:
        # Car routing around one-way streets or U-turns: buses don't do this. Draw it straight
        # at the same speed; a waypoints entry can replace it with the real road.
        duration = max(1.0, best.duration * straight / best.distance)
        return [a, b], straight, duration, "straight", True
    line = as_lonlats(best.geometry.coordinates)
    return line, best.distance, max(1.0, best.duration), source, flagged


def snap_direction(
    router: Router, points: list[LonLat], via: Callable[[int], list[LonLat]]
) -> tuple[list[LonLat], list[SegmentInfo]]:
    """The whole direction as consecutive per-pair stretches, plus one record per stretch.

    `via(from_seq)` returns the waypoints for the stretch starting at stop `from_seq` (1-based).
    """
    line: list[LonLat] = []
    segments: list[SegmentInfo] = []
    for i in range(len(points) - 1):
        piece, road, duration, source, flagged = snap_pair(
            router, points[i], points[i + 1], via(i + 1)
        )
        straight = haversine_m(points[i], points[i + 1])
        segments.append(
            SegmentInfo(
                from_seq=i + 1,
                to_seq=i + 2,
                road_m=round(road, 1),
                duration_s=round(duration, 1),
                straight_m=round(straight, 1),
                ratio=round(road / straight, 2) if straight > 0 else 0.0,
                geometry_source=source,
                flagged=flagged,
            )
        )
        for p in piece:
            if not line or haversine_m(line[-1], p) > 0.5:
                line.append(p)
    return line, segments


def turn_points(
    stops: dict[int, list[LonLat]], fractions: tuple[float, ...] = TURN_FRACTIONS
) -> list[tuple[int, int, int]]:
    """(direction, stop index, opposite-direction stop index) for each short-turn connection.

    For each direction: the intermediate stop nearest each fraction of the way along that has
    an opposite-direction stop within MAX_TURN_PAIR_DISTANCE_M (the simulator's rule), where
    that opposite stop is not a terminal.
    """
    chosen: list[tuple[int, int, int]] = []
    for d in (0, 1):
        own, other = stops[d], stops[1 - d]
        candidates: list[tuple[int, int]] = []
        for i in range(1, len(own) - 1):
            gaps = [haversine_m(own[i], q) for q in other]
            j = gaps.index(min(gaps))
            if haversine_m(own[i], other[j]) <= MAX_TURN_PAIR_DISTANCE_M and j < len(other) - 1:
                candidates.append((i, j))
        picked: set[int] = set()
        for f in fractions:
            target = f * (len(own) - 1)
            remaining = [c for c in candidates if c[0] not in picked]
            if remaining:
                i, j = min(remaining, key=lambda c: abs(c[0] - target))
                picked.add(i)
                chosen.append((d, i, j))
    return chosen


def _feature(line: list[LonLat], properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {
            "type": "LineString",
            "coordinates": [[round(x, 6), round(y, 6)] for x, y in line],
        },
        "properties": properties,
    }


def turn_feature(
    router: Router,
    turn: tuple[int, int, int],
    stops: dict[int, list[LonLat]],
    stop_ids: dict[int, list[str]],
) -> dict[str, object]:
    d, i, j = turn
    best = min(router([stops[d][i], stops[1 - d][j]], True), key=lambda r: r.distance)
    return _feature(
        as_lonlats(best.geometry.coordinates),
        {
            "direction_id": d,
            "at_seq": i + 1,
            "at_stop_id": stop_ids[d][i],
            "to_direction_id": 1 - d,
            "to_seq": j + 1,
            "to_stop_id": stop_ids[1 - d][j],
            "road_m": round(best.distance, 1),
            "duration_s": round(max(1.0, best.duration), 1),
            "geometry_source": "osrm-pair",
        },
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.gtfs.snap_shape", description=__doc__)
    parser.add_argument("--route", required=True)
    parser.add_argument("--feed", type=Path)
    parser.add_argument(
        "--osrm", default=DEFAULT_OSRM, help=f"OSRM base URL (default {DEFAULT_OSRM})"
    )
    args = parser.parse_args(argv)
    try:
        config = load_route_config(args.route)
        waypoints = load_waypoints(config.id)
        gtfs = GtfsZip(args.feed or config.feed_path)
        try:
            feed = gtfs.load({d.gtfs_route_id for d in config.directions})
        finally:
            gtfs.close()
        router = osrm_router(args.osrm)
        generated = datetime.now(UTC).isoformat(timespec="seconds")
        stops: dict[int, list[LonLat]] = {}
        stop_ids: dict[int, list[str]] = {}
        for d in sorted(config.directions, key=lambda c: c.direction_id):
            pattern, _, _ = dominant_pattern(feed.trips_by_route[d.gtfs_route_id])
            points = [(feed.stops[s].stop_lon, feed.stops[s].stop_lat) for s in pattern]
            stops[d.direction_id], stop_ids[d.direction_id] = points, list(pattern)

            def via(seq: int, d_id: int = d.direction_id) -> list[LonLat]:
                return waypoints.via(d_id, seq)

            line, segments = snap_direction(router, points, via)
            feature = _feature(
                line,
                {
                    "gtfs_route_id": d.gtfs_route_id,
                    "stop_ids": list(pattern),
                    "source": f"OSRM driving route per stop pair via {args.osrm}",
                    "attribution": ATTRIBUTION,
                    "license": "ODbL-1.0",
                    "generated_at": generated,
                    "segments": [s.model_dump() for s in segments],
                },
            )
            out = ROUTES_DIR / d.shape_file
            out.write_text(json.dumps(feature, indent=1) + "\n", encoding="utf-8")
            flagged = [f"{s.from_seq}->{s.to_seq}" for s in segments if s.flagged]
            print(
                f"dir {d.direction_id}: {len(points)} stops -> {len(line)} points, "
                f"{polyline_length_m(line) / 1000:.2f} km, "
                f"{sum(s.duration_s for s in segments) / 60:.1f} min free-flow -> {out.name}; "
                f"flagged: {flagged or 'none'}"
            )
        turns = [turn_feature(router, t, stops, stop_ids) for t in turn_points(stops)]
        out = ROUTES_DIR / f"{config.id}.turns.geojson"
        collection = {
            "type": "FeatureCollection",
            "properties": {
                "source": f"OSRM driving route via {args.osrm}",
                "attribution": ATTRIBUTION,
                "license": "ODbL-1.0",
                "generated_at": generated,
            },
            "features": turns,
        }
        out.write_text(json.dumps(collection, indent=1) + "\n", encoding="utf-8")
        print(f"{len(turns)} short-turn connections -> {out.name}")
    except (ConfigError, FeedError, SnapError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
