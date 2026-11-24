"""Feed parsing, route building and validation on synthetic GTFS (no database)."""

import json
import zipfile
from pathlib import Path

import pytest

from app.gtfs.builder import (
    SHAPE_SNAPPED,
    SHAPE_STOP_TO_STOP,
    RouteValidationError,
    validate_direction,
)
from app.gtfs.feed import FeedError, GtfsZip
from app.gtfs.import_gtfs import prepare
from app.gtfs.route_config import ConfigError, load_route_config
from tests.gtfs_fixtures import FeedSpec, cached_shape, write_feed, write_route_config


@pytest.fixture
def routes_dir(tmp_path: Path) -> Path:
    d = tmp_path / "routes"
    write_route_config(d)
    return d


def _prepare(tmp_path: Path, routes_dir: Path, spec: FeedSpec | None = None):  # type: ignore[no-untyped-def]
    feed = write_feed(tmp_path / "feed.zip", spec)
    return prepare("T1", feed, routes_dir)


def test_builds_both_directions_from_a_clean_feed(tmp_path: Path, routes_dir: Path) -> None:
    route = _prepare(tmp_path, routes_dir)
    assert [d.direction_id for d in route.directions] == [0, 1]
    out, back = route.directions
    assert len(out.stops) == 16
    assert out.peak_headway_seconds == 300
    assert out.scheduled_run_seconds == 15 * 90
    assert out.stops[0].stop_id == "S0"
    assert back.stops[0].stop_id == "S15"  # reverse direction starts at the far end
    assert out.shape_source == SHAPE_STOP_TO_STOP
    assert any("no cached shape" in w for w in route.warnings)
    along = [s.distance_along_m for s in out.stops]
    assert along == sorted(along)
    assert route.agency == "MTC"
    assert route.feed_version == "test-1"


def test_uses_a_matching_cached_shape(tmp_path: Path, routes_dir: Path) -> None:
    spec = FeedSpec()
    pts = spec.points()
    # A road line offset ~20 m east of the stops (stops sit on the kerb).
    line = [(lon + 0.00018, lat) for lon, lat in pts]
    (routes_dir / "T1.dir0.geojson").write_text(
        json.dumps(cached_shape(line, "R-OUT", [f"S{i}" for i in range(16)])), encoding="utf-8"
    )
    route = _prepare(tmp_path, routes_dir, spec)
    out = route.directions[0]
    assert out.shape_source == SHAPE_SNAPPED
    assert all(15 < s.offset_from_shape_m < 25 for s in out.stops)


def test_stale_cached_shape_falls_back_with_a_warning(tmp_path: Path, routes_dir: Path) -> None:
    (routes_dir / "T1.dir0.geojson").write_text(
        json.dumps(cached_shape(FeedSpec().points(), "R-OUT", ["S0", "S1"])), encoding="utf-8"
    )
    route = _prepare(tmp_path, routes_dir)
    assert route.directions[0].shape_source == SHAPE_STOP_TO_STOP
    assert any("stale" in w for w in route.warnings)


def test_cached_shape_for_another_route_is_rejected(tmp_path: Path, routes_dir: Path) -> None:
    (routes_dir / "T1.dir0.geojson").write_text(
        json.dumps(cached_shape(FeedSpec().points(), "SOMETHING-ELSE", [])), encoding="utf-8"
    )
    with pytest.raises(RouteValidationError, match="was snapped for SOMETHING-ELSE"):
        _prepare(tmp_path, routes_dir)


def test_stop_far_from_the_line_fails_validation(tmp_path: Path, routes_dir: Path) -> None:
    pts = FeedSpec().points()
    line = [(lon + 0.002, lat) for lon, lat in pts]  # ~215 m away: not this road
    (routes_dir / "T1.dir0.geojson").write_text(
        json.dumps(cached_shape(line, "R-OUT", [f"S{i}" for i in range(16)])), encoding="utf-8"
    )
    with pytest.raises(RouteValidationError) as exc:
        _prepare(tmp_path, routes_dir)
    assert any("from the route line" in p for p in exc.value.problems)


def test_local_detour_fails_validation(tmp_path: Path, routes_dir: Path) -> None:
    """A loop between two stops (car-routing artefact) is caught per segment."""
    pts = FeedSpec().points()
    a, b = pts[4], pts[5]
    loop = [a, (a[0] + 0.006, a[1]), (a[0] + 0.006, b[1]), b]  # ~1.3 km for a ~400 m hop
    line = pts[:4] + loop + pts[6:]
    (routes_dir / "T1.dir0.geojson").write_text(
        json.dumps(cached_shape(line, "R-OUT", [f"S{i}" for i in range(16)])), encoding="utf-8"
    )
    with pytest.raises(RouteValidationError) as exc:
        _prepare(tmp_path, routes_dir)
    assert any("local detour" in p for p in exc.value.problems)


def test_too_few_stops_fails_validation(tmp_path: Path, routes_dir: Path) -> None:
    with pytest.raises(RouteValidationError, match="expected 15-25"):
        _prepare(tmp_path, routes_dir, FeedSpec(n_stops=8))


def test_implausible_headway_fails_validation(tmp_path: Path, routes_dir: Path) -> None:
    with pytest.raises(RouteValidationError, match="peak headway"):
        _prepare(tmp_path, routes_dir, FeedSpec(headway_s=3600))


def test_out_of_order_stops_fail_validation(tmp_path: Path, routes_dir: Path) -> None:
    route = _prepare(tmp_path, routes_dir)
    d = route.directions[0]
    swapped = list(d.stops)
    swapped[3], swapped[4] = swapped[4], swapped[3]
    problems = validate_direction(type(d)(**{**d.__dict__, "stops": swapped}))
    assert any("is not after stop" in p for p in problems)


def test_markup_in_names_is_kept_as_inert_text(tmp_path: Path, routes_dir: Path) -> None:
    """The real feed contains a <script> tag; names are data, never interpreted."""
    name = "'/><script>alert(1)</script>"
    route = _prepare(tmp_path, routes_dir, FeedSpec(stop_names={2: name}))
    assert route.directions[0].stops[2].name == name


def test_coordinates_outside_chennai_are_rejected(tmp_path: Path, routes_dir: Path) -> None:
    with pytest.raises(FeedError, match="stop_lat"):
        _prepare(tmp_path, routes_dir, FeedSpec(start=(80.25, 28.6)))  # Delhi


def test_missing_route_id_is_reported(tmp_path: Path) -> None:
    gtfs = GtfsZip(write_feed(tmp_path / "feed.zip"))
    with pytest.raises(FeedError, match="NOPE"):
        gtfs.load({"NOPE"})
    gtfs.close()


def test_missing_file_in_zip_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("routes.txt", "route_id\n")
    with pytest.raises(FeedError, match=r"trips\.txt missing|not in routes\.txt"):
        GtfsZip(path).load({"R-OUT"})


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b", "", "x" * 40])
def test_route_config_ids_cannot_escape_the_routes_folder(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="invalid route id"):
        load_route_config(bad, tmp_path)
