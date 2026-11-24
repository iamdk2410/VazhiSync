"""Build small, valid GTFS zips for tests (the real Chennai feed is not in the repo/CI)."""

import csv
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.gtfs.geometry import LonLat


@dataclass
class FeedSpec:
    """A two-direction route along a straight-ish street in central Chennai."""

    n_stops: int = 16
    spacing_deg: float = 0.0036  # ~400 m of latitude
    start: LonLat = (80.2500, 13.0500)
    first_departure_s: int = 6 * 3600
    last_departure_s: int = 11 * 3600
    headway_s: int = 300
    seconds_per_stop: int = 90
    stop_names: dict[int, str] = field(default_factory=dict)
    extra_routes: list[dict[str, str]] = field(default_factory=list)

    def points(self) -> list[LonLat]:
        # Slight eastward drift so the line is not perfectly axis-aligned.
        return [
            (self.start[0] + i * self.spacing_deg * 0.2, self.start[1] + i * self.spacing_deg)
            for i in range(self.n_stops)
        ]


def _csv(rows: Sequence[Mapping[str, object]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _hms(s: int) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def write_feed(path: Path, spec: FeedSpec | None = None) -> Path:
    spec = spec or FeedSpec()
    pts = spec.points()
    stops = [
        {
            "stop_id": f"S{i}",
            "stop_name": spec.stop_names.get(i, f"Stop {i}"),
            "stop_lat": f"{lat:.6f}",
            "stop_lon": f"{lon:.6f}",
        }
        for i, (lon, lat) in enumerate(pts)
    ]
    routes = [
        {
            "route_id": rid,
            "route_short_name": "T1",
            "route_long_name": name,
            "route_type": 3,
            "agency_id": "69",
        }
        for rid, name in (("R-OUT", "South TO North"), ("R-BACK", "North TO South"))
    ] + spec.extra_routes
    trips, stop_times = [], []
    order = {"R-OUT": list(range(spec.n_stops)), "R-BACK": list(range(spec.n_stops))[::-1]}
    for direction, rid in enumerate(("R-OUT", "R-BACK")):
        for k, dep in enumerate(
            range(spec.first_departure_s, spec.last_departure_s + 1, spec.headway_s)
        ):
            tid = f"{rid}-T{k}"
            trips.append(
                {
                    "trip_id": tid,
                    "route_id": rid,
                    "service_id": "MTC-Regular",
                    "direction_id": direction,
                }
            )
            for seq, stop_index in enumerate(order[rid], start=1):
                t = _hms(dep + (seq - 1) * spec.seconds_per_stop)
                stop_times.append(
                    {
                        "trip_id": tid,
                        "arrival_time": t,
                        "departure_time": t,
                        "stop_id": f"S{stop_index}",
                        "stop_sequence": seq,
                    }
                )
    files = {
        "agency.txt": _csv([{"agency_id": "69", "agency_name": "MTC"}]),
        "feed_info.txt": _csv([{"feed_publisher_name": "test", "feed_version": "test-1"}]),
        "routes.txt": _csv(routes),
        "trips.txt": _csv(trips),
        "stops.txt": _csv(stops),
        "stop_times.txt": _csv(stop_times),
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return path


def write_route_config(routes_dir: Path, feed_file: str = "feed.zip") -> None:
    routes_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "id": "T1",
        "short_name": "T1",
        "long_name": "South - North",
        "feed_file": feed_file,
        "directions": [
            {
                "direction_id": 0,
                "gtfs_route_id": "R-OUT",
                "headsign": "North",
                "shape_file": "T1.dir0.geojson",
            },
            {
                "direction_id": 1,
                "gtfs_route_id": "R-BACK",
                "headsign": "South",
                "shape_file": "T1.dir1.geojson",
            },
        ],
    }
    (routes_dir / "T1.json").write_text(json.dumps(config), encoding="utf-8")


def cached_shape(
    line: Sequence[LonLat], gtfs_route_id: str, stop_ids: Sequence[str]
) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [list(p) for p in line]},
        "properties": {
            "gtfs_route_id": gtfs_route_id,
            "stop_ids": list(stop_ids),
            "source": "test",
            "attribution": "test",
            "license": "test",
            "generated_at": "2026-01-01T00:00:00+00:00",
        },
    }
