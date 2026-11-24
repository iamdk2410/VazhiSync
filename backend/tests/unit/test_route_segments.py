"""B6 geometry follow-up: per-stop-pair snapping, waypoints, segment metadata and its use in the
simulator. A fake router stands in for OSRM: tests never call the network."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.gtfs.geometry import LonLat, haversine_m
from app.gtfs.import_gtfs import prepare
from app.gtfs.route_config import (
    ConfigError,
    SegmentInfo,
    ShapeProperties,
    Waypoints,
    load_waypoints,
)
from app.gtfs.sim_route import to_route_model
from app.gtfs.snap_shape import OsrmRoute, snap_direction, turn_points
from simulator import RouteModel, RouteModelError, SimConfig, Simulation
from simulator.model import DirectionModel
from tests.gtfs_fixtures import FeedSpec, cached_shape, write_feed, write_route_config
from tests.sim_fixtures import line_route

POINTS: list[LonLat] = [(80.25, 13.05), (80.25, 13.054), (80.25, 13.058), (80.25, 13.062)]


def _route(points: list[LonLat], distance: float | None = None) -> OsrmRoute:
    length = distance if distance is not None else haversine_m(points[0], points[-1])
    return OsrmRoute.model_validate(
        {
            "geometry": {"type": "LineString", "coordinates": [list(p) for p in points]},
            "distance": length,
            "duration": length / 10,  # 10 m/s
        }
    )


class FakeRouter:
    """Straight roads, except where told to detour; records every request."""

    def __init__(self, detours: dict[int, float] | None = None) -> None:
        self.calls: list[list[LonLat]] = []
        self._detours = detours or {}  # segment index -> road/straight ratio

    def __call__(self, points: list[LonLat], alternatives: bool) -> list[OsrmRoute]:
        self.calls.append(points)
        index = POINTS.index(points[0]) if points[0] in POINTS else -1
        ratio = self._detours.get(index)
        if ratio and len(points) == 2:
            return [_route(points, haversine_m(points[0], points[-1]) * ratio)]
        return [_route(points)]


def test_each_stop_pair_is_snapped_on_its_own() -> None:
    router = FakeRouter()
    line, segments = snap_direction(router, POINTS, lambda seq: [])
    assert len(router.calls) == 3  # one request per pair
    assert [(s.from_seq, s.to_seq) for s in segments] == [(1, 2), (2, 3), (3, 4)]
    assert all(s.geometry_source == "osrm-pair" and not s.flagged for s in segments)
    assert line == POINTS  # shared endpoints are not duplicated
    assert segments[0].road_m == pytest.approx(445, abs=2)
    assert segments[0].duration_s == pytest.approx(segments[0].road_m / 10, abs=0.2)


def test_detour_is_flagged_and_drawn_straight() -> None:
    _, segments = snap_direction(FakeRouter(detours={1: 4.0}), POINTS, lambda seq: [])
    bad = segments[1]
    assert bad.flagged and bad.geometry_source == "straight"
    assert bad.ratio == pytest.approx(1.0)
    assert bad.duration_s == pytest.approx(bad.straight_m / 10, abs=0.2)  # same speed


def test_waypoints_route_through_the_via_points() -> None:
    via = [(80.2505, 13.056)]
    router = FakeRouter(detours={1: 4.0})
    _, segments = snap_direction(router, POINTS, lambda seq: via if seq == 2 else [])
    assert router.calls[1] == [POINTS[1], *via, POINTS[2]]
    assert segments[1].geometry_source == "waypoints"
    assert not segments[1].flagged


def test_turn_points_pick_mid_route_stops_with_an_opposite_stop_nearby() -> None:
    out = [(80.25 + i * 0.004, 13.05) for i in range(10)]
    back = list(reversed(out))
    turns = turn_points({0: out, 1: back})
    assert len(turns) == 4
    for d, i, j in turns:
        assert 0 < i < 9 and j < 9  # never at a terminal
        own, other = (out, back) if d == 0 else (back, out)
        assert own[i] == other[j]


def test_segments_must_cover_every_stop_pair() -> None:
    seg = {
        "road_m": 400.0,
        "duration_s": 40.0,
        "straight_m": 400.0,
        "ratio": 1.0,
        "geometry_source": "osrm-pair",
        "flagged": False,
    }
    base = {
        "gtfs_route_id": "R-OUT",
        "stop_ids": ["S0", "S1", "S2"],
        "source": "t",
        "attribution": "t",
        "license": "t",
        "generated_at": "2026-01-01T00:00:00+00:00",
    }
    ShapeProperties.model_validate(
        {
            **base,
            "segments": [{**seg, "from_seq": 1, "to_seq": 2}, {**seg, "from_seq": 2, "to_seq": 3}],
        }
    )
    with pytest.raises(ValidationError, match="cover every"):
        ShapeProperties.model_validate({**base, "segments": [{**seg, "from_seq": 1, "to_seq": 2}]})
    with pytest.raises(ValidationError, match="consecutive"):
        SegmentInfo.model_validate({**seg, "from_seq": 1, "to_seq": 3})


def test_waypoints_file_is_optional_and_validated(tmp_path: Path) -> None:
    assert load_waypoints("T1", tmp_path).via(0, 3) == []
    (tmp_path / "T1.waypoints.json").write_text(
        json.dumps({"directions": {"1": {"4": [[80.27, 13.08]]}}}), encoding="utf-8"
    )
    assert load_waypoints("T1", tmp_path).via(1, 4) == [(80.27, 13.08)]
    (tmp_path / "T1.waypoints.json").write_text(
        json.dumps({"directions": {"0": {"1": [[77.59, 12.97]]}}}),  # Bengaluru: a typo
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="invalid waypoints"):
        load_waypoints("T1", tmp_path)
    with pytest.raises(ValidationError):
        Waypoints.model_validate({"directions": {"2": {}}})


def test_importer_carries_segment_durations_to_the_simulator(tmp_path: Path) -> None:
    routes_dir = tmp_path / "routes"
    write_route_config(routes_dir)
    spec = FeedSpec()
    pts = spec.points()
    stop_ids = [f"S{i}" for i in range(16)]
    shape = cached_shape(pts, "R-OUT", stop_ids)
    shape["properties"]["segments"] = [  # type: ignore[index]
        {
            "from_seq": i + 1,
            "to_seq": i + 2,
            "road_m": 400.0,
            "duration_s": 30.0 + i,
            "straight_m": 400.0,
            "ratio": 1.0,
            "geometry_source": "osrm-pair",
            "flagged": False,
        }
        for i in range(15)
    ]
    (routes_dir / "T1.dir0.geojson").write_text(json.dumps(shape), encoding="utf-8")
    route = prepare("T1", write_feed(tmp_path / "feed.zip", spec), routes_dir)
    out = route.directions[0]
    assert out.stops[0].segment_duration_s is None
    assert [s.segment_duration_s for s in out.stops[1:]] == [30.0 + i for i in range(15)]
    assert out.stops[1].segment_geometry_source == "osrm-pair"
    model = to_route_model(route)
    assert model.direction(0).segment_road_s == tuple(30.0 + i for i in range(15))
    assert model.direction(1).segment_road_s is None  # no cached shape for direction 1


def test_road_times_spread_running_time_in_their_proportions() -> None:
    base = line_route()
    d0 = base.direction(0)
    road = DirectionModel(
        d0.direction_id, d0.headsign, d0.length_m, d0.scheduled_headway_s, d0.stops,
        segment_road_s=(10.0, 30.0, 10.0, 30.0, 20.0),
    )  # fmt: skip
    assert [round(road.segment_share(i), 2) for i in range(5)] == [0.1, 0.3, 0.1, 0.3, 0.2]
    assert [d0.segment_share(i) for i in range(5)] == [0.2] * 5  # timetable fallback
    route = RouteModel("LINE", (road, base.direction(1)))
    cfg = SimConfig(run_share=0.7, max_speed_mps=100.0)
    sim = Simulation.new(route, cfg)
    assert sim._base_run_s(road, 1) == pytest.approx(300 * 0.7 * 0.3)
    assert sim._base_run_s(road, 0) == pytest.approx(300 * 0.7 * 0.1)
    assert RouteModel.from_dict(route.to_dict()) == route


@pytest.mark.parametrize("times", [(1.0, 2.0), (1.0, 2.0, 3.0, 4.0, 0.0)])
def test_segment_road_times_are_validated(times: tuple[float, ...]) -> None:
    d0 = line_route().direction(0)
    with pytest.raises(RouteModelError, match="segment_road_s"):
        DirectionModel(0, "E", d0.length_m, 300, d0.stops, segment_road_s=times)
