"""Turn a GTFS feed + route config (+ optional cached snapped shapes) into a validated route.

Pure: no database, no network. `build_route` raises `RouteValidationError` listing every
problem it found, so a bad import never reaches the database.
"""

from collections import Counter
from dataclasses import dataclass, field

from app.gtfs.feed import GtfsStop, RouteFeed, TripTimes
from app.gtfs.geometry import (
    LocalPlane,
    LonLat,
    as_lonlats,
    haversine_m,
    polyline_length_m,
    project_in_order,
)
from app.gtfs.route_config import CachedShape, DirectionConfig, RouteConfig
from app.gtfs.schedule import (
    HourHeadway,
    format_gtfs_time,
    headway_profile,
    median_offsets,
    peak_headway_seconds,
    unique_sorted,
)

# Validation thresholds (B2: "stops in order, shape passes near each stop, headway sensible").
STOP_COUNT_RANGE = (15, 25)
MAX_STOP_OFFSET_M = 75.0  # stop to road centreline (GTFS stop positions are approximate)
MAX_ENDPOINT_GAP_M = 150.0  # shape start/end to first/last stop
DETOUR_RATIO_MAX = 1.6  # shape length vs straight stop-to-stop length
SEGMENT_DETOUR_RATIO_MAX = 2.5  # one stop-to-stop stretch vs its straight distance
SEGMENT_DETOUR_MIN_M = 200.0  # short hops around a corner are allowed to look "bendy"
STOP_SPACING_RANGE_M = (50.0, 3000.0)
PEAK_HEADWAY_RANGE_S = (120, 1200)
AVG_SPEED_RANGE_KMH = (8.0, 40.0)
MIN_PATTERN_SHARE = 0.8

SHAPE_SNAPPED = "osrm-snapped"
SHAPE_STOP_TO_STOP = "stop-to-stop"


class RouteValidationError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class StopOnRoute:
    sequence: int
    stop_id: str
    name: str
    lon_lat: LonLat
    distance_along_m: float
    offset_from_shape_m: float
    scheduled_offset_seconds: int
    # The road stretch from the previous stop (None for the first stop or unsnapped shapes).
    segment_duration_s: float | None = None
    segment_geometry_source: str | None = None


@dataclass(frozen=True)
class DirectionImport:
    direction_id: int
    gtfs_route_id: str
    headsign: str
    line: list[LonLat]
    shape_source: str
    length_m: float
    stops: list[StopOnRoute]
    scheduled_run_seconds: int
    peak_headway_seconds: int
    headway_profile: list[HourHeadway]
    trips_per_day: int
    first_departure: str
    last_departure: str


@dataclass(frozen=True)
class RouteImport:
    id: str
    short_name: str
    long_name: str
    agency: str
    feed_version: str
    feed_file: str
    directions: list[DirectionImport]
    warnings: list[str] = field(default_factory=list)

    @property
    def stops(self) -> dict[str, StopOnRoute]:
        return {s.stop_id: s for d in self.directions for s in d.stops}


def build_route(
    feed: RouteFeed, config: RouteConfig, shapes: dict[int, CachedShape | None]
) -> RouteImport:
    problems: list[str] = []
    warnings: list[str] = []
    directions: list[DirectionImport] = []
    for dcfg in sorted(config.directions, key=lambda d: d.direction_id):
        label = f"direction {dcfg.direction_id} ({dcfg.gtfs_route_id})"
        try:
            direction, dir_warnings = _build_direction(feed, dcfg, shapes.get(dcfg.direction_id))
        except RouteValidationError as exc:
            problems.extend(f"{label}: {p}" for p in exc.problems)
            continue
        warnings.extend(f"{label}: {w}" for w in dir_warnings)
        problems.extend(f"{label}: {p}" for p in validate_direction(direction))
        directions.append(direction)
    if problems:
        raise RouteValidationError(problems)

    first = feed.routes[config.directions[0].gtfs_route_id]
    return RouteImport(
        id=config.id,
        short_name=config.short_name,
        long_name=config.long_name,
        agency=feed.agency_names.get(first.agency_id, first.agency_id) or "unknown",
        feed_version=feed.feed_version,
        feed_file=config.feed_file,
        directions=directions,
        warnings=warnings,
    )


def dominant_pattern(trips: list[TripTimes]) -> tuple[tuple[str, ...], list[TripTimes], float]:
    """The most common stop sequence, the trips that follow it, and its share of all trips."""
    counts = Counter(t.stop_ids for t in trips)
    pattern, n = counts.most_common(1)[0]
    return pattern, [t for t in trips if t.stop_ids == pattern], n / len(trips)


def _build_direction(
    feed: RouteFeed, cfg: DirectionConfig, cached: CachedShape | None
) -> tuple[DirectionImport, list[str]]:
    warnings: list[str] = []
    trips = feed.trips_by_route[cfg.gtfs_route_id]
    pattern, pattern_trips, share = dominant_pattern(trips)
    if share < MIN_PATTERN_SHARE:
        warnings.append(f"main stop pattern covers only {share:.0%} of trips")
    stops: list[GtfsStop] = [feed.stops[s] for s in pattern]
    points: list[LonLat] = [(s.stop_lon, s.stop_lat) for s in stops]
    if len(points) < 2:
        raise RouteValidationError(["needs at least two stops"])

    line, source = _choose_shape(cfg, cached, pattern, points, warnings)
    segments = cached.properties.segments if cached and source == SHAPE_SNAPPED else []
    seg_by_to = {s.to_seq: s for s in segments}
    plane = LocalPlane.around(points)
    projections = project_in_order(line, points, plane)
    offsets = median_offsets([t.departures for t in pattern_trips])
    departures = unique_sorted(t.departures[0] for t in trips)
    profile = headway_profile(departures)
    peak = peak_headway_seconds(profile)
    if peak is None:
        raise RouteValidationError(["no departures in the 07:00-10:00 peak to derive a headway"])

    return (
        DirectionImport(
            direction_id=cfg.direction_id,
            gtfs_route_id=cfg.gtfs_route_id,
            headsign=cfg.headsign,
            line=line,
            shape_source=source,
            length_m=polyline_length_m(line),
            stops=[
                StopOnRoute(
                    sequence=i + 1,
                    stop_id=stop.stop_id,
                    name=stop.stop_name,
                    lon_lat=points[i],
                    distance_along_m=round(proj.distance_along_m, 1),
                    offset_from_shape_m=round(proj.offset_m, 1),
                    scheduled_offset_seconds=offsets[i],
                    segment_duration_s=(
                        seg_by_to[i + 1].duration_s if i + 1 in seg_by_to else None
                    ),
                    segment_geometry_source=(
                        seg_by_to[i + 1].geometry_source if i + 1 in seg_by_to else None
                    ),
                )
                for i, (stop, proj) in enumerate(zip(stops, projections, strict=True))
            ],
            scheduled_run_seconds=offsets[-1],
            peak_headway_seconds=peak,
            headway_profile=profile,
            trips_per_day=len(departures),
            first_departure=format_gtfs_time(departures[0]),
            last_departure=format_gtfs_time(departures[-1]),
        ),
        warnings,
    )


def _choose_shape(
    cfg: DirectionConfig,
    cached: CachedShape | None,
    pattern: tuple[str, ...],
    points: list[LonLat],
    warnings: list[str],
) -> tuple[list[LonLat], str]:
    if cached is None:
        warnings.append(f"no cached shape ({cfg.shape_file}); using stop-to-stop lines")
        return list(points), SHAPE_STOP_TO_STOP
    if cached.properties.gtfs_route_id != cfg.gtfs_route_id:
        raise RouteValidationError(
            [f"{cfg.shape_file} was snapped for {cached.properties.gtfs_route_id}"]
        )
    if tuple(cached.properties.stop_ids) != pattern:
        warnings.append(
            f"{cfg.shape_file} is stale (stop pattern changed); using stop-to-stop lines. "
            "Re-run: python -m app.gtfs.snap_shape"
        )
        return list(points), SHAPE_STOP_TO_STOP
    return as_lonlats(cached.geometry.coordinates), SHAPE_SNAPPED


def validate_direction(d: DirectionImport) -> list[str]:
    """Every check from the build plan, as human-readable problems (empty = valid)."""
    problems: list[str] = []
    lo, hi = STOP_COUNT_RANGE
    if not lo <= len(d.stops) <= hi:
        problems.append(f"{len(d.stops)} stops, expected {lo}-{hi}")

    for prev, cur in zip(d.stops, d.stops[1:], strict=False):
        if cur.distance_along_m <= prev.distance_along_m:
            problems.append(
                f"stop {cur.sequence} ({cur.name}) is not after stop {prev.sequence} along the "
                f"route ({cur.distance_along_m:.0f} m <= {prev.distance_along_m:.0f} m)"
            )
        gap = cur.distance_along_m - prev.distance_along_m
        smin, smax = STOP_SPACING_RANGE_M
        if gap > 0 and not smin <= gap <= smax:
            problems.append(f"stops {prev.sequence}->{cur.sequence} are {gap:.0f} m apart")
        if cur.scheduled_offset_seconds < prev.scheduled_offset_seconds:
            problems.append(f"timetable goes backwards at stop {cur.sequence}")
        straight_gap = haversine_m(prev.lon_lat, cur.lon_lat)
        if (
            gap > SEGMENT_DETOUR_MIN_M
            and straight_gap > 0
            and gap / straight_gap > SEGMENT_DETOUR_RATIO_MAX
        ):
            problems.append(
                f"stops {prev.sequence}->{cur.sequence} ({prev.name} -> {cur.name}): line takes "
                f"{gap:.0f} m for a {straight_gap:.0f} m hop (local detour)"
            )

    for s in d.stops:
        if s.offset_from_shape_m > MAX_STOP_OFFSET_M:
            problems.append(
                f"stop {s.sequence} ({s.name}) is {s.offset_from_shape_m:.0f} m from the route line"
            )

    if d.stops:
        if d.stops[0].distance_along_m > MAX_ENDPOINT_GAP_M:
            problems.append(
                f"line starts {d.stops[0].distance_along_m:.0f} m before the first stop"
            )
        tail = d.length_m - d.stops[-1].distance_along_m
        if tail > MAX_ENDPOINT_GAP_M:
            problems.append(f"line continues {tail:.0f} m past the last stop")
        straight = polyline_length_m([s.lon_lat for s in d.stops])
        if straight > 0 and d.length_m / straight > DETOUR_RATIO_MAX:
            problems.append(
                f"line is {d.length_m / straight:.2f}x the stop-to-stop distance (detour?)"
            )

    hmin, hmax = PEAK_HEADWAY_RANGE_S
    if not hmin <= d.peak_headway_seconds <= hmax:
        problems.append(f"peak headway {d.peak_headway_seconds / 60:.1f} min is not sensible")

    if d.scheduled_run_seconds <= 0:
        problems.append("scheduled run time is zero")
    else:
        kmh = d.length_m / d.scheduled_run_seconds * 3.6
        vmin, vmax = AVG_SPEED_RANGE_KMH
        if not vmin <= kmh <= vmax:
            problems.append(f"average scheduled speed {kmh:.1f} km/h is not sensible")
    return problems
