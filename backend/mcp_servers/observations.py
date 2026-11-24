"""Reads of what the AVL system observed (vehicle_positions, stop_queues): never the world
simulator's state (rule 1). Age and staleness are computed here, server-side (rule 4)."""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Route, Vehicle, VehiclePosition
from app.live.headways import TERMINAL_ZONE_M, Fix, compute_headways
from app.runner.route_loader import RouteNotImported, WorldRoute, load_world_route
from mcp_servers.errors import tool_error
from mcp_servers.schemas import HeadwayPair, Headways, VehiclePositions
from mcp_servers.schemas import VehiclePosition as PositionOut

Clock = Callable[[], datetime]
ROUTE_CACHE_SECONDS = 300.0
_route_cache: dict[str, tuple[float, WorldRoute]] = {}


def utc_now() -> datetime:
    return datetime.now(UTC)


async def route_model(session: AsyncSession, route_id: str) -> WorldRoute:
    """The imported route (stops, geometry, timetable), cached for a few minutes."""
    cached = _route_cache.get(route_id)
    if cached and time.monotonic() - cached[0] < ROUTE_CACHE_SECONDS:
        return cached[1]
    try:
        route = await load_world_route(session, route_id)
    except RouteNotImported:
        raise tool_error("route_not_found", f"route {route_id} is not available") from None
    _route_cache[route_id] = (time.monotonic(), route)
    return route


@dataclass(frozen=True)
class Observed:
    vehicle: Vehicle
    fix: VehiclePosition | None
    lon: float | None
    lat: float | None


async def observed_vehicles(session: AsyncSession, route_id: str) -> list[Observed]:
    """Every active vehicle on the route with its latest GPS fix (if it ever reported)."""
    if await session.get(Route, route_id) is None:
        raise tool_error("route_not_found", f"unknown route {route_id}")
    vehicles = (
        await session.scalars(
            select(Vehicle)
            .where(Vehicle.route_id == route_id, Vehicle.is_active.is_(True))
            .order_by(Vehicle.fleet_number)
        )
    ).all()
    latest = (
        await session.execute(
            select(
                VehiclePosition, func.ST_X(VehiclePosition.geom), func.ST_Y(VehiclePosition.geom)
            )
            .where(VehiclePosition.vehicle_id.in_([v.id for v in vehicles]))
            .ext(distinct_on(VehiclePosition.vehicle_id))
            .order_by(VehiclePosition.vehicle_id, VehiclePosition.recorded_at.desc())
        )
    ).all()
    by_vehicle = {pos.vehicle_id: (pos, lon, lat) for pos, lon, lat in latest}
    out = []
    for v in vehicles:
        pos, lon, lat = by_vehicle.get(v.id, (None, None, None))
        out.append(Observed(v, pos, lon, lat))
    return out


def age_seconds(recorded_at: datetime, now: datetime) -> float:
    return round(max((now - recorded_at).total_seconds(), 0.0), 1)


async def vehicle_positions(
    session: AsyncSession, route_id: str, now: datetime, stale_after_s: int
) -> VehiclePositions:
    observed = await observed_vehicles(session, route_id)
    vehicles: list[PositionOut] = []
    for o in observed:
        if o.fix is None or o.lon is None or o.lat is None or o.fix.direction_id is None:
            continue
        age = age_seconds(o.fix.recorded_at, now)
        vehicles.append(
            PositionOut(
                vehicle_id=o.vehicle.id,
                fleet_number=o.vehicle.fleet_number,
                direction_id=o.fix.direction_id,
                lon=o.lon,
                lat=o.lat,
                distance_m=o.fix.distance_along_m,
                speed_mps=o.fix.speed_mps,
                heading_deg=o.fix.heading_deg,
                load=o.fix.load_count,
                recorded_at=o.fix.recorded_at,
                world_time_s=o.fix.sim_time_s,
                age_seconds=age,
                is_stale=age > stale_after_s,
            )
        )
    return VehiclePositions(
        route_id=route_id,
        server_time=now,
        stale_after_seconds=stale_after_s,
        vehicles=vehicles,
        missing=[
            o.vehicle.fleet_number for o in observed if o.fix is None or o.fix.direction_id is None
        ],
    )


async def headways(
    session: AsyncSession,
    route_id: str,
    now: datetime,
    stale_after_s: int,
    bunching_ratio: float,
    closing_ratio: float,
) -> Headways:
    route = await route_model(session, route_id)
    positions = await vehicle_positions(session, route_id, now, stale_after_s)
    by_id = {str(v.vehicle_id): v for v in positions.vehicles}
    fixes = [
        Fix(str(v.vehicle_id), v.direction_id, v.distance_m or 0.0, v.age_seconds)
        for v in positions.vehicles
    ]
    pairs = []
    for h in compute_headways(
        fixes, route.directions, stale_after_s, bunching_ratio, closing_ratio
    ):
        leader, follower = by_id[h.leader_id], by_id[h.follower_id]
        pairs.append(
            HeadwayPair(
                direction_id=h.direction_id,
                leader_vehicle_id=uuid.UUID(h.leader_id),
                leader_fleet_number=leader.fleet_number,
                follower_vehicle_id=uuid.UUID(h.follower_id),
                follower_fleet_number=follower.fleet_number,
                gap_m=h.gap_m,
                headway_seconds=h.headway_s,
                scheduled_headway_seconds=h.scheduled_headway_s,
                ratio=h.ratio,
                status=h.status,
                data_quality="stale" if h.status == "stale" else "ok",
                leader_age_seconds=leader.age_seconds,
                follower_age_seconds=follower.age_seconds,
            )
        )
    terminal = [
        v.fleet_number
        for v in positions.vehicles
        if (v.distance_m or 0.0) <= route.directions[v.direction_id].first_stop_m + TERMINAL_ZONE_M
    ]
    return Headways(
        route_id=route_id,
        server_time=now,
        bunching_ratio=bunching_ratio,
        closing_ratio=closing_ratio,
        stale_after_seconds=stale_after_s,
        pairs=pairs,
        not_in_service=terminal,
        missing=positions.missing,
    )
