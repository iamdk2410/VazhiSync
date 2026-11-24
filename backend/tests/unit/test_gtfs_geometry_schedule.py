import pytest

from app.gtfs.geometry import (
    LocalPlane,
    _tail_from,
    haversine_m,
    planar_length_m,
    polyline_length_m,
    project_in_order,
    project_onto,
    slice_line,
)
from app.gtfs.schedule import (
    format_gtfs_time,
    headway_profile,
    median_offsets,
    parse_gtfs_time,
    peak_headway_seconds,
)

# ~1.1 km straight north-south street in Chennai.
LINE = [(80.25, 13.05), (80.25, 13.06)]
PLANE = LocalPlane.around(LINE)


def test_haversine_one_hundredth_degree_latitude() -> None:
    assert haversine_m(LINE[0], LINE[1]) == pytest.approx(1112, abs=2)


def test_planar_and_haversine_lengths_agree_at_city_scale() -> None:
    line = [(80.25, 13.05), (80.27, 13.08), (80.29, 13.10)]
    assert planar_length_m(line, LocalPlane.around(line)) == pytest.approx(
        polyline_length_m(line), rel=1e-3
    )


def test_projection_reports_distance_along_and_offset() -> None:
    # A point ~100 m east of the street, a quarter of the way up.
    p = project_onto(LINE, (80.25 + 0.000925, 13.0525), PLANE)
    assert p.distance_along_m == pytest.approx(278, abs=3)
    assert p.offset_m == pytest.approx(100, abs=3)


def test_projection_clamps_before_the_start() -> None:
    p = project_onto(LINE, (80.25, 13.049), PLANE)
    assert p.distance_along_m == 0
    assert p.offset_m == pytest.approx(111, abs=2)


def test_in_order_projection_follows_a_street_used_twice() -> None:
    """Out along the street, loop, and back: later stops must match the return leg."""
    line = [(80.25, 13.05), (80.25, 13.06), (80.2505, 13.06), (80.2505, 13.05)]
    plane = LocalPlane.around(line)
    stops = [(80.25, 13.052), (80.25, 13.058), (80.2505, 13.058), (80.2505, 13.052)]
    along = [p.distance_along_m for p in project_in_order(line, stops, plane)]
    assert along == sorted(along)
    assert along[3] > along[2] > 1112  # matched on the way back, not the way out


def test_tail_past_the_end_reports_the_last_segment_start() -> None:
    """Regression: the fallback used to report the line's end as the segment start."""
    line = [(80.25, 13.05), (80.25, 13.06), (80.25, 13.07)]
    plane = LocalPlane.around(line)
    sub, start = _tail_from(line, plane, 1e9)
    assert sub == line[-2:]
    assert start == pytest.approx(planar_length_m(line[:2], plane))


def test_slice_line_interpolates_endpoints() -> None:
    piece = slice_line(LINE, 278, 556, PLANE)
    assert len(piece) == 2
    assert piece[0][1] == pytest.approx(13.0525, abs=1e-5)
    assert piece[1][1] == pytest.approx(13.055, abs=1e-5)


def test_slice_line_tolerates_rounding_but_not_reversal() -> None:
    total = planar_length_m(LINE, PLANE)
    assert slice_line(LINE, total + 1e-9, total, PLANE)  # float noise is fine
    with pytest.raises(ValueError):
        slice_line(LINE, 500, 100, PLANE)


@pytest.mark.parametrize(("text", "seconds"), [("06:20:00", 22800), ("25:10:05", 90605)])
def test_parse_gtfs_time_including_after_midnight(text: str, seconds: int) -> None:
    assert parse_gtfs_time(text) == seconds
    assert format_gtfs_time(seconds) == text


@pytest.mark.parametrize("bad", ["6:20", "aa:bb:cc", "10:61:00", "48:00:00", ""])
def test_parse_gtfs_time_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_gtfs_time(bad)


def test_headway_profile_ignores_duplicate_departures() -> None:
    departures = [7 * 3600 + m * 60 for m in (0, 5, 5, 10, 15)]  # 07:05 listed twice
    profile = headway_profile(departures)
    assert [(h.hour, h.headway_seconds, h.departures) for h in profile] == [(7, 300, 3)]


def test_peak_headway_uses_morning_peak_only() -> None:
    early = [5 * 3600 + m * 60 for m in range(0, 60, 20)]  # every 20 min at 05:00
    peak = [7 * 3600 + m * 60 for m in range(0, 180, 6)]  # every 6 min 07:00-10:00
    assert peak_headway_seconds(headway_profile(early + peak)) == 360


def test_median_offsets() -> None:
    trips = [(100, 160, 260), (200, 260, 380), (300, 360, 460)]
    assert median_offsets(trips) == [0, 60, 160]
    with pytest.raises(ValueError):
        median_offsets([(1, 2), (1, 2, 3)])
