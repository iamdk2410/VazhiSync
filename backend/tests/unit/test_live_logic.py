"""B6 pure logic: headways from GPS fixes, bunching hysteresis, role filtering, positioning."""

import uuid
from datetime import UTC, datetime

import pytest

from app.db.enums import BunchingSeverity, Role
from app.gtfs.geometry import LocalPlane
from app.live.headways import TERMINAL_ZONE_M, DirectionInfo, Fix, Headway, compute_headways
from app.live.schemas import (
    LiveEvent,
    LiveFrame,
    LiveHeadway,
    LiveQueue,
    LiveVehicle,
    crowding,
    project,
)
from app.runner.bunching import BunchingTracker
from app.runner.geo import LineIndex
from app.runner.world import clock_time, parse_speed

# 300 s scheduled headway at 5 m/s: a 1500 m gap is exactly on schedule.
DIR = {0: DirectionInfo(first_stop_m=0.0, scheduled_headway_s=300, avg_speed_mps=5.0)}


def _headways(*fixes: Fix) -> list[Headway]:
    return compute_headways(fixes, DIR, stale_after_s=60, bunching_ratio=0.5, closing_ratio=0.75)


def test_headway_is_the_gap_to_the_bus_ahead_in_time() -> None:
    (h,) = _headways(Fix("A", 0, 3000, 1), Fix("B", 0, 1500, 1))
    assert (h.leader_id, h.follower_id) == ("A", "B")
    assert h.gap_m == 1500 and h.headway_s == 300 and h.ratio == 1.0 and h.status == "ok"


@pytest.mark.parametrize(
    ("gap_m", "status"), [(1500, "ok"), (1100, "closing"), (740, "bunched"), (100, "bunched")]
)
def test_headway_classification(gap_m: float, status: str) -> None:
    (h,) = _headways(Fix("A", 0, 4000, 1), Fix("B", 0, 4000 - gap_m, 1))
    assert h.status == status


def test_stale_pairs_are_never_classified() -> None:
    (h,) = _headways(Fix("A", 0, 4000, 61), Fix("B", 0, 3950, 1))
    assert h.status == "stale"


def test_buses_waiting_at_the_terminal_are_not_in_service() -> None:
    hs = _headways(Fix("A", 0, 3000, 1), Fix("B", 0, TERMINAL_ZONE_M, 1), Fix("C", 0, 0.0, 1))
    assert hs == []


def test_directions_are_independent() -> None:
    dirs = {**DIR, 1: DirectionInfo(0.0, 300, 5.0)}
    hs = compute_headways(
        [Fix("A", 0, 3000, 1), Fix("B", 1, 2990, 1)], dirs, 60, 0.5, 0.75
    )  # same distance, different directions: not a pair
    assert hs == []


def _h(ratio: float, status: str = "") -> Headway:
    status = status or ("bunched" if ratio < 0.5 else "closing" if ratio < 0.75 else "ok")
    return Headway(0, "A", "B", ratio * 1500, ratio * 300, 300, ratio, status)  # type: ignore[arg-type]


def test_bunching_opens_once_and_resolves_with_hysteresis() -> None:
    tracker = BunchingTracker(closing_ratio=0.75)
    first = tracker.update([_h(0.4)])
    assert len(first.open) == 1
    tracker.opened(("A", "B"), event := uuid.uuid4())
    assert tracker.update([_h(0.3)]).open == []  # still bunched: no duplicate
    assert tracker.update([_h(0.6)]).resolve == []  # closing, not yet recovered
    assert tracker.update([_h(0.8)]).resolve == [event]
    assert tracker.open_events == {}


def test_stale_data_neither_opens_nor_resolves() -> None:
    tracker = BunchingTracker(closing_ratio=0.75)
    assert tracker.update([_h(0.2, "stale")]).open == []
    tracker.opened(("A", "B"), uuid.uuid4())
    assert tracker.update([_h(0.9, "stale")]).resolve == []


def test_event_resolves_when_the_pair_no_longer_exists() -> None:
    tracker = BunchingTracker(closing_ratio=0.75)
    tracker.opened(("A", "B"), event := uuid.uuid4())
    assert tracker.update([]).resolve == [event]


# ------------------------------------------------------------------ role filtering

NOW = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)
BUS_A, BUS_B = uuid.uuid4(), uuid.uuid4()


def _vehicle(vid: uuid.UUID, load: int) -> LiveVehicle:
    return LiveVehicle(
        vehicle_id=vid, fleet_number=str(vid)[:4], direction_id=0, lon=80.27, lat=13.08,
        heading_deg=0, distance_m=100, speed_mps=5, load=load, capacity=80,
        crowding=crowding(load, 80), last_fix_at=NOW, age_seconds=1, is_stale=False,
    )  # fmt: skip


FRAME = LiveFrame(
    route_id="32B", sim_time="08:00:00", sim_elapsed_s=0, speed=1, generated_at=NOW,
    vehicles=[_vehicle(BUS_A, 70), _vehicle(BUS_B, 10)],
    queues=[LiveQueue(direction_id=0, stop_id="S1", waiting=4)],
    headways=[LiveHeadway(
        direction_id=0, leader_vehicle_id=BUS_A, follower_vehicle_id=BUS_B, gap_m=200,
        headway_seconds=40, scheduled_headway_seconds=300, ratio=0.13, status="bunched",
    )],
    events=[LiveEvent(
        event_id=uuid.uuid4(), leader_vehicle_id=BUS_A, follower_vehicle_id=BUS_B,
        severity=BunchingSeverity.BUNCHED, detected_at=NOW, headway_seconds=40,
    )],
)  # fmt: skip


def test_officer_sees_everything() -> None:
    assert project(FRAME, Role.OFFICER) == FRAME


def test_driver_sees_only_their_own_bus() -> None:
    frame = project(FRAME, Role.DRIVER, BUS_B)
    assert [v.vehicle_id for v in frame.vehicles] == [BUS_B]
    assert frame.queues == [] and frame.events == []
    assert len(frame.headways) == 1  # the gap to the bus ahead
    assert project(FRAME, Role.DRIVER, None).vehicles == []  # no assignment: nothing


def test_passenger_sees_public_data_only() -> None:
    frame = project(FRAME, Role.PASSENGER)
    assert len(frame.vehicles) == 2
    assert all(v.load is None for v in frame.vehicles)
    assert [v.crowding for v in frame.vehicles] == ["high", "low"]
    assert frame.queues == [] and frame.headways == [] and frame.events == []


def test_crowding_levels() -> None:
    assert [crowding(n, 80) for n in (0, 39, 40, 67, 68, 80)] == [
        "low", "low", "medium", "medium", "high", "high",
    ]  # fmt: skip


# ------------------------------------------------------------------ positions


def test_line_index_places_points_and_headings() -> None:
    line = [(80.25, 13.05), (80.25, 13.06), (80.26, 13.06)]  # north, then east
    index = LineIndex(line, LocalPlane.around(line))
    north = index.locate(500)
    assert north.lon == pytest.approx(80.25) and 13.05 < north.lat < 13.06
    assert north.heading_deg == pytest.approx(0, abs=0.5)
    east = index.locate(index.length_m - 10)
    assert east.heading_deg == pytest.approx(90, abs=0.5)
    assert index.locate(-5) == index.locate(0)  # clamped to the route
    assert index.locate(1e9).lon == pytest.approx(80.26)


def test_speed_and_clock_parsing() -> None:
    assert [parse_speed(v) for v in (b"1", "5", b"10", b"2", b"x", None)] == [
        1, 5, 10, None, None, None,
    ]  # fmt: skip
    assert clock_time(8 * 3600 + 61) == "08:01:01"
    assert clock_time(25 * 3600) == "01:00:00"
