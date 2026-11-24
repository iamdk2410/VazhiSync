import uuid
from datetime import datetime

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utc_now_column, uuid_pk


class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (CheckConstraint("capacity > 0", name="capacity_positive"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    fleet_number: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. "TN01AN1234"
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"), index=True)
    capacity: Mapped[int] = mapped_column(server_default=text("80"))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = utc_now_column()


class DriverAssignment(Base):
    __tablename__ = "driver_assignments"
    __table_args__ = (
        # At most one open assignment per driver and per vehicle.
        Index(
            "uq_driver_assignments_open_driver",
            "driver_id",
            unique=True,
            postgresql_where=text("ends_at IS NULL"),
        ),
        Index(
            "uq_driver_assignments_open_vehicle",
            "vehicle_id",
            unique=True,
            postgresql_where=text("ends_at IS NULL"),
        ),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="ends_after_start"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    driver_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime] = utc_now_column()
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VehiclePosition(Base):
    """What GPS reported (written by the world runner, honouring dropouts)."""

    __tablename__ = "vehicle_positions"
    __table_args__ = (
        Index("ix_vehicle_positions_vehicle_recorded", "vehicle_id", text("recorded_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id", ondelete="CASCADE"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # GPS fix time
    received_at: Mapped[datetime] = utc_now_column()
    geom: Mapped[WKBElement] = mapped_column(Geometry("POINT", srid=4326, spatial_index=True))
    distance_along_m: Mapped[float | None]
    speed_mps: Mapped[float | None]
    heading_deg: Mapped[float | None]
    load_count: Mapped[int | None]


class StopQueue(Base):
    __tablename__ = "stop_queues"
    __table_args__ = (
        Index("ix_stop_queues_stop_recorded", "stop_id", text("recorded_at DESC")),
        CheckConstraint("waiting_count >= 0", name="waiting_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    route_id: Mapped[str] = mapped_column(ForeignKey("routes.id"))
    stop_id: Mapped[str] = mapped_column(ForeignKey("stops.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    waiting_count: Mapped[int]
