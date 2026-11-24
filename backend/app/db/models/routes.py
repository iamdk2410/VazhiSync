"""Static network data imported from GTFS (B2): one route, its two directions and stops."""

from datetime import datetime
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now_column


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "32B"
    short_name: Mapped[str] = mapped_column(String(32))
    long_name: Mapped[str] = mapped_column(String(200))
    agency: Mapped[str] = mapped_column(String(100))
    source: Mapped[dict[str, Any]] = mapped_column(JSONB)  # feed file, version, GTFS route ids
    imported_at: Mapped[datetime] = utc_now_column()


class Stop(Base):
    __tablename__ = "stops"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # GTFS stop_id
    name: Mapped[str] = mapped_column(String(200))
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326, spatial_index=True))


class RouteDirection(Base):
    """One direction of a route: ordered stops along a road geometry, plus its schedule."""

    __tablename__ = "route_directions"
    __table_args__ = (
        UniqueConstraint("route_id", "direction_id"),
        CheckConstraint("direction_id IN (0, 1)", name="direction_0_or_1"),
        CheckConstraint("length_m > 0", name="length_positive"),
        CheckConstraint("peak_headway_seconds > 0", name="headway_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id", ondelete="CASCADE"))
    direction_id: Mapped[int]
    gtfs_route_id: Mapped[str] = mapped_column(String(64))
    headsign: Mapped[str] = mapped_column(String(200))
    geom: Mapped[WKBElement] = mapped_column(Geometry("LINESTRING", srid=4326))
    shape_source: Mapped[str] = mapped_column(String(32))  # "osrm-snapped" | "stop-to-stop"
    length_m: Mapped[float]
    scheduled_run_seconds: Mapped[int]
    peak_headway_seconds: Mapped[int]  # median 07:00-10:00
    # [{"hour": 7, "headway_seconds": 300, "departures": 12}, ...]
    headway_profile: Mapped[list[dict[str, int]]] = mapped_column(JSONB)
    trips_per_day: Mapped[int]
    first_departure: Mapped[str] = mapped_column(String(8))  # GTFS time, may exceed 24:00
    last_departure: Mapped[str] = mapped_column(String(8))


class RouteDirectionStop(Base):
    __tablename__ = "route_direction_stops"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("distance_along_m >= 0", name="distance_non_negative"),
        CheckConstraint(
            "segment_duration_s IS NULL OR segment_duration_s > 0",
            name="segment_duration_positive",
        ),
    )

    route_direction_id: Mapped[int] = mapped_column(
        ForeignKey("route_directions.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(primary_key=True)
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.id"), index=True)
    distance_along_m: Mapped[float]  # stop projected onto the direction's geometry
    offset_from_shape_m: Mapped[float]  # how far the stop sits from the line
    scheduled_offset_seconds: Mapped[int]  # time from the first stop, per the timetable
    # The road stretch from the previous stop (B6): OSRM free-flow duration and how the
    # geometry was made (osrm-pair | waypoints | straight). NULL for the first stop.
    segment_duration_s: Mapped[float | None]
    segment_geometry_source: Mapped[str | None] = mapped_column(String(16))
