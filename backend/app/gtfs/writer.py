"""Write a validated RouteImport to Postgres in one transaction (re-runnable)."""

from dataclasses import asdict

from geoalchemy2 import WKTElement
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Route, RouteDirection, RouteDirectionStop, Stop
from app.gtfs.builder import RouteImport
from app.gtfs.geometry import LonLat


def _point(p: LonLat) -> WKTElement:
    return WKTElement(f"POINT({p[0]:.7f} {p[1]:.7f})", srid=4326)


def _linestring(line: list[LonLat]) -> WKTElement:
    return WKTElement(
        "LINESTRING(" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in line) + ")", srid=4326
    )


async def write_route(session: AsyncSession, route: RouteImport) -> None:
    """Upsert the route and its stops, then replace its directions.

    The route row is updated in place (not deleted) so vehicles, events and favourites that
    reference it stay valid across re-imports.
    """
    async with session.begin():
        route_values = {
            "short_name": route.short_name,
            "long_name": route.long_name,
            "agency": route.agency,
            "source": {
                "feed_file": route.feed_file,
                "feed_version": route.feed_version,
                "gtfs_route_ids": [d.gtfs_route_id for d in route.directions],
                "warnings": route.warnings,
            },
        }
        await session.execute(
            insert(Route)
            .values(id=route.id, **route_values)
            .on_conflict_do_update(index_elements=[Route.id], set_=route_values)
        )
        for stop in route.stops.values():
            values = {"name": stop.name, "geom": _point(stop.lon_lat)}
            await session.execute(
                insert(Stop)
                .values(id=stop.stop_id, **values)
                .on_conflict_do_update(index_elements=[Stop.id], set_=values)
            )
        await session.execute(delete(RouteDirection).where(RouteDirection.route_id == route.id))
        for d in route.directions:
            direction = RouteDirection(
                route_id=route.id,
                direction_id=d.direction_id,
                gtfs_route_id=d.gtfs_route_id,
                headsign=d.headsign,
                geom=_linestring(d.line),
                shape_source=d.shape_source,
                length_m=round(d.length_m, 1),
                scheduled_run_seconds=d.scheduled_run_seconds,
                peak_headway_seconds=d.peak_headway_seconds,
                headway_profile=[asdict(h) for h in d.headway_profile],
                trips_per_day=d.trips_per_day,
                first_departure=d.first_departure,
                last_departure=d.last_departure,
            )
            session.add(direction)
            await session.flush()
            session.add_all(
                RouteDirectionStop(
                    route_direction_id=direction.id,
                    sequence=s.sequence,
                    stop_id=s.stop_id,
                    distance_along_m=s.distance_along_m,
                    offset_from_shape_m=s.offset_from_shape_m,
                    scheduled_offset_seconds=s.scheduled_offset_seconds,
                    segment_duration_s=s.segment_duration_s,
                    segment_geometry_source=s.segment_geometry_source,
                )
                for s in d.stops
            )
