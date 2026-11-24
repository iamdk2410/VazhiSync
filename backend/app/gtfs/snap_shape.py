"""One-off: snap each direction's stop sequence to the road network and cache it as GeoJSON.

    uv run python -m app.gtfs.snap_shape --route 32B

Sends only the route's public stop coordinates to an OSRM server (default: the public demo
server). The result is written to backend/data/gtfs/routes/<shape_file> and committed, so
imports, tests and CI never call the network. Map data (c) OpenStreetMap contributors, ODbL.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError

from app.gtfs.builder import SEGMENT_DETOUR_MIN_M, SEGMENT_DETOUR_RATIO_MAX, dominant_pattern
from app.gtfs.feed import FeedError, GtfsZip
from app.gtfs.geometry import (
    LocalPlane,
    LonLat,
    as_lonlats,
    haversine_m,
    planar_length_m,
    polyline_length_m,
    project_in_order,
    slice_line,
)
from app.gtfs.route_config import ROUTES_DIR, ConfigError, load_route_config

DEFAULT_OSRM = "https://router.project-osrm.org"
USER_AGENT = "vazhisync-gtfs-snap/0.1"
TIMEOUT_SECONDS = 30
ATTEMPTS = 3  # transient errors only (rule 7)


class SnapError(Exception):
    pass


class _OsrmGeometry(BaseModel):
    type: Literal["LineString"]
    coordinates: Annotated[list[tuple[float, float]], Field(min_length=2)]


class _OsrmRoute(BaseModel):
    geometry: _OsrmGeometry
    distance: float


class _OsrmResponse(BaseModel):
    code: str
    routes: list[_OsrmRoute] = []
    message: str | None = None


def osrm_routes(
    base_url: str, points: list[tuple[float, float]], alternatives: bool = False
) -> list[_OsrmRoute]:
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


def repair_detours(
    base_url: str, line: list[LonLat], points: list[LonLat]
) -> tuple[list[LonLat], list[dict[str, object]]]:
    """Re-snap stop-to-stop stretches that detour (one-way/U-turn artefacts of car routing).

    Each flagged stretch is re-routed on its own, taking OSRM's shortest alternative; if that
    still detours, the stretch becomes a straight line. Returns the new line and a record of
    what was patched (stored in the GeoJSON so the change is visible).
    """
    plane = LocalPlane.around(points)
    proj = project_in_order(line, points, plane)
    pieces: list[list[LonLat]] = [slice_line(line, 0.0, proj[0].distance_along_m, plane)]
    patched: list[dict[str, object]] = []
    for i in range(len(points) - 1):
        a_m, b_m = proj[i].distance_along_m, proj[i + 1].distance_along_m
        road, straight = b_m - a_m, haversine_m(points[i], points[i + 1])
        piece = slice_line(line, a_m, b_m, plane)
        if (
            road > SEGMENT_DETOUR_MIN_M
            and straight > 0
            and road / straight > SEGMENT_DETOUR_RATIO_MAX
        ):
            time.sleep(1)
            best = min(
                osrm_routes(base_url, [points[i], points[i + 1]], True), key=lambda r: r.distance
            )
            if best.distance / straight <= SEGMENT_DETOUR_RATIO_MAX:
                piece, method = as_lonlats(best.geometry.coordinates), "osrm-pair"
            else:
                piece, method = [points[i], points[i + 1]], "straight"
            patched.append(
                {
                    "from_seq": i + 1,
                    "to_seq": i + 2,
                    "was_m": round(road),
                    "method": method,
                    "now_m": round(polyline_length_m(piece)),
                }
            )
        pieces.append(piece)
    total = planar_length_m(line, plane)
    pieces.append(slice_line(line, proj[-1].distance_along_m, total, plane))
    merged: list[LonLat] = []
    for piece in pieces:
        for p in piece:
            if not merged or haversine_m(merged[-1], p) > 0.5:
                merged.append(p)
    return merged, patched


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
        gtfs = GtfsZip(args.feed or config.feed_path)
        try:
            feed = gtfs.load({d.gtfs_route_id for d in config.directions})
        finally:
            gtfs.close()
        for d in config.directions:
            pattern, _, _ = dominant_pattern(feed.trips_by_route[d.gtfs_route_id])
            points = [(feed.stops[s].stop_lon, feed.stops[s].stop_lat) for s in pattern]
            snapped = osrm_routes(args.osrm, points)[0]
            line, patched = repair_detours(
                args.osrm, as_lonlats(snapped.geometry.coordinates), points
            )
            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(x, 6), round(y, 6)] for x, y in line],
                },
                "properties": {
                    "gtfs_route_id": d.gtfs_route_id,
                    "stop_ids": list(pattern),
                    "source": f"OSRM driving route via {args.osrm}",
                    "attribution": "Map data (c) OpenStreetMap contributors",
                    "license": "ODbL-1.0",
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "patched_segments": patched,
                },
            }
            out = ROUTES_DIR / d.shape_file
            out.write_text(json.dumps(feature, indent=1) + "\n", encoding="utf-8")
            print(
                f"dir {d.direction_id}: {len(points)} stops -> {len(line)} points, "
                f"{polyline_length_m(line) / 1000:.2f} km -> {out.name}; "
                f"patched: {patched or 'none'}"
            )
            time.sleep(1)  # be polite to the shared demo server
    except (ConfigError, FeedError, SnapError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
