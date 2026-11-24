"""Committed per-route config and the cached road-snapped shapes (backend/data/gtfs/routes/)."""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from app.gtfs.feed import GtfsId

BACKEND_DIR = Path(__file__).resolve().parents[2]
GTFS_DIR = BACKEND_DIR / "data" / "gtfs"
ROUTES_DIR = GTFS_DIR / "routes"

RouteId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
FileName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}\.geojson$")]
FeedFile = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}\.zip$")]


class ConfigError(Exception):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DirectionConfig(_Strict):
    direction_id: Literal[0, 1]
    gtfs_route_id: GtfsId
    headsign: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    shape_file: FileName


class RouteConfig(_Strict):
    id: RouteId
    short_name: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    long_name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    feed_file: FeedFile
    directions: Annotated[list[DirectionConfig], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def _both_directions(self) -> "RouteConfig":
        if {d.direction_id for d in self.directions} != {0, 1}:
            raise ValueError("directions must be exactly one 0 and one 1")
        return self

    @property
    def feed_path(self) -> Path:
        return GTFS_DIR / self.feed_file


def load_route_config(route_id: str, routes_dir: Path = ROUTES_DIR) -> RouteConfig:
    try:
        safe_id = _validate_route_id(route_id)
        raw = (routes_dir / f"{safe_id}.json").read_text(encoding="utf-8")
        return RouteConfig.model_validate_json(raw)
    except FileNotFoundError:
        raise ConfigError(f"no route config at {routes_dir / (route_id + '.json')}") from None
    except ValidationError as exc:
        raise ConfigError(f"invalid route config for {route_id}: {exc}") from None


def _validate_route_id(route_id: str) -> str:
    class _Id(BaseModel):
        value: RouteId

    try:
        return _Id(value=route_id).value  # rejects "../", slashes, etc.
    except ValidationError:
        raise ConfigError(f"invalid route id: {route_id!r}") from None


# ------------------------------------------------------------------ cached snapped shapes


class LineStringGeometry(_Strict):
    type: Literal["LineString"]
    coordinates: Annotated[list[tuple[float, float]], Field(min_length=2)]


GeometrySource = Literal["osrm", "osrm-pair", "waypoints", "straight"]


class SegmentInfo(_Strict):
    """One stop-to-stop stretch of a snapped shape (B6 geometry follow-up)."""

    from_seq: Annotated[int, Field(ge=1)]
    to_seq: Annotated[int, Field(ge=2)]
    road_m: Annotated[float, Field(ge=0)]
    duration_s: Annotated[float, Field(gt=0)]  # OSRM free-flow car time
    straight_m: Annotated[float, Field(ge=0)]
    ratio: Annotated[float, Field(ge=0)]  # road_m / straight_m
    geometry_source: GeometrySource
    flagged: bool  # road/straight ratio above the detour limit when routed

    @model_validator(mode="after")
    def _consecutive(self) -> "SegmentInfo":
        if self.to_seq != self.from_seq + 1:
            raise ValueError("a segment joins consecutive stops")
        return self


class ShapeProperties(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    gtfs_route_id: GtfsId
    stop_ids: list[GtfsId]  # the stop pattern the shape was snapped through
    source: str
    attribution: str
    license: str
    generated_at: datetime
    segments: list[SegmentInfo] = []  # empty for shapes snapped before B6

    @model_validator(mode="after")
    def _one_segment_per_stop_pair(self) -> "ShapeProperties":
        if self.segments and [s.from_seq for s in self.segments] != list(
            range(1, len(self.stop_ids))
        ):
            raise ValueError("segments must cover every consecutive stop pair, in order")
        return self


class CachedShape(_Strict):
    type: Literal["Feature"]
    geometry: LineStringGeometry
    properties: ShapeProperties


def load_cached_shape(path: Path) -> CachedShape | None:
    """The cached shape, or None if absent. A present-but-invalid file is an error."""
    if not path.is_file():
        return None
    try:
        return CachedShape.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid cached shape {path.name}: {exc}") from None


# ------------------------------------------------------------------ optional waypoints

Coordinate = tuple[
    Annotated[float, Field(ge=79.9, le=80.5)], Annotated[float, Field(ge=12.7, le=13.4)]
]  # inside Chennai: a typo can't send the router across the country


class Waypoints(_Strict):
    """`<route>.waypoints.json`: via points that force a stop-to-stop stretch onto a road.

    {"directions": {"0": {"3": [[80.27, 13.08]]}}}  ->  direction 0, stop 3 -> stop 4.
    """

    directions: dict[Literal["0", "1"], dict[str, Annotated[list[Coordinate], Field(min_length=1)]]]

    def via(self, direction_id: int, from_seq: int) -> list[tuple[float, float]]:
        key: Literal["0", "1"] = "1" if direction_id else "0"
        return list(self.directions.get(key, {}).get(str(from_seq), []))


def load_waypoints(route_id: str, routes_dir: Path = ROUTES_DIR) -> Waypoints:
    """The route's waypoints file, or an empty set if there is none."""
    path = routes_dir / f"{_validate_route_id(route_id)}.waypoints.json"
    if not path.is_file():
        return Waypoints(directions={})
    try:
        return Waypoints.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise ConfigError(f"invalid waypoints file {path.name}: {exc}") from None
