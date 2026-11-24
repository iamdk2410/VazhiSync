"""Reading a GTFS zip. The feed is untrusted input: every row we use is validated (rule 6).

Only the rows for the requested GTFS route ids are kept in memory; stop_times.txt is
streamed because it is ~50 MB for the whole city.
"""

import csv
import io
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.gtfs.schedule import parse_gtfs_time

# Generous Chennai metropolitan bounding box: anything outside is a data error.
CHENNAI_LAT = (12.5, 13.5)
CHENNAI_LON = (79.8, 80.5)

GtfsId = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class FeedError(Exception):
    """The feed is missing data we need, or contains invalid rows for our route."""


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GtfsRoute(_Row):
    route_id: GtfsId
    route_short_name: Text
    route_long_name: Text
    route_type: int
    agency_id: Text = ""


class GtfsTrip(_Row):
    trip_id: GtfsId
    route_id: GtfsId
    direction_id: Annotated[int, Field(ge=0, le=1)] = 0


class GtfsStop(_Row):
    stop_id: GtfsId
    # Names are stored and returned as plain text, never interpreted as markup.
    stop_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
    ]
    stop_lat: Annotated[float, Field(ge=CHENNAI_LAT[0], le=CHENNAI_LAT[1])]
    stop_lon: Annotated[float, Field(ge=CHENNAI_LON[0], le=CHENNAI_LON[1])]


class GtfsStopTime(_Row):
    trip_id: GtfsId
    stop_id: GtfsId
    stop_sequence: Annotated[int, Field(ge=0)]
    departure_time: Annotated[str, StringConstraints(strip_whitespace=True)]


@dataclass(frozen=True)
class TripTimes:
    trip_id: str
    stop_ids: tuple[str, ...]
    departures: tuple[int, ...]  # seconds after midnight, one per stop


@dataclass(frozen=True)
class RouteFeed:
    """Everything the importer needs for a set of GTFS route ids."""

    feed_version: str
    agency_names: dict[str, str]
    routes: dict[str, GtfsRoute]
    trips_by_route: dict[str, list[TripTimes]]
    stops: dict[str, GtfsStop]


class GtfsZip:
    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FeedError(f"GTFS zip not found: {path}")
        self._zip = zipfile.ZipFile(path)

    def close(self) -> None:
        self._zip.close()

    def rows(self, name: str) -> Iterator[dict[str, str]]:
        if name not in self._zip.namelist():
            raise FeedError(f"{name} missing from the feed")
        with self._zip.open(name) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))

    def load(self, gtfs_route_ids: set[str]) -> RouteFeed:
        routes = {
            r["route_id"]: _parse(GtfsRoute, r, "routes.txt")
            for r in self.rows("routes.txt")
            if r.get("route_id") in gtfs_route_ids
        }
        if missing := gtfs_route_ids - routes.keys():
            raise FeedError(f"route ids not in routes.txt: {sorted(missing)}")

        trip_route: dict[str, str] = {}
        for row in self.rows("trips.txt"):
            if row.get("route_id") in gtfs_route_ids:
                trip = _parse(GtfsTrip, row, "trips.txt")
                trip_route[trip.trip_id] = trip.route_id

        stop_times: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
        for row in self.rows("stop_times.txt"):
            if row.get("trip_id") in trip_route:
                st = _parse(GtfsStopTime, row, "stop_times.txt")
                try:
                    dep = parse_gtfs_time(st.departure_time)
                except ValueError as exc:
                    raise FeedError(f"stop_times.txt trip {st.trip_id}: {exc}") from None
                stop_times[st.trip_id].append((st.stop_sequence, st.stop_id, dep))

        trips_by_route: dict[str, list[TripTimes]] = defaultdict(list)
        for trip_id, entries in stop_times.items():
            entries.sort()
            trips_by_route[trip_route[trip_id]].append(
                TripTimes(
                    trip_id=trip_id,
                    stop_ids=tuple(s for _, s, _ in entries),
                    departures=tuple(d for _, _, d in entries),
                )
            )
        for route_id in gtfs_route_ids:
            if not trips_by_route.get(route_id):
                raise FeedError(f"route {route_id} has no trips with stop times")

        needed = {s for trips in trips_by_route.values() for t in trips for s in t.stop_ids}
        stops = {
            r["stop_id"]: _parse(GtfsStop, r, "stops.txt")
            for r in self.rows("stops.txt")
            if r.get("stop_id") in needed
        }
        if missing_stops := needed - stops.keys():
            raise FeedError(f"stops referenced but not defined: {sorted(missing_stops)[:10]}")

        return RouteFeed(
            feed_version=self._feed_version(),
            agency_names={
                a.get("agency_id", ""): a.get("agency_name", "").strip()
                for a in self.rows("agency.txt")
            },
            routes=routes,
            trips_by_route=dict(trips_by_route),
            stops=stops,
        )

    def _feed_version(self) -> str:
        try:
            info = next(self.rows("feed_info.txt"), {})
        except FeedError:
            return "unknown"
        return (info.get("feed_version") or "unknown").strip()[:32]


def _parse[M: _Row](model: type[M], row: dict[str, str], file: str) -> M:
    try:
        return model.model_validate(row)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise FeedError(
            f"{file}: invalid row ({'.'.join(map(str, first['loc']))}: {first['msg']})"
        ) from None
