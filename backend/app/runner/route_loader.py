"""Load an imported route from Postgres as the simulator's RouteModel plus its geometry."""

import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RouteDirection, RouteDirectionStop, Stop
from app.gtfs.geometry import LocalPlane, LonLat, as_lonlats
from app.live.headways import DirectionInfo
from app.runner.geo import LineIndex
from simulator import DirectionModel, RouteModel, StopModel


class RouteNotImported(Exception):
    pass


@dataclass(frozen=True)
class WorldRoute:
    model: RouteModel
    lines: dict[int, LineIndex]
    stop_ids: dict[int, list[str]]
    directions: dict[int, DirectionInfo]  # for headway calculations


async def load_world_route(session: AsyncSession, route_id: str) -> WorldRoute:
    rows = (
        await session.execute(
            select(RouteDirection, func.ST_AsGeoJSON(RouteDirection.geom))
            .where(RouteDirection.route_id == route_id)
            .order_by(RouteDirection.direction_id)
        )
    ).all()
    if [d.direction_id for d, _ in rows] != [0, 1]:
        raise RouteNotImported(
            f"route {route_id} is not imported; run: python -m app.gtfs.import_gtfs "
            f"--route {route_id}"
        )
    models: list[DirectionModel] = []
    lines: dict[int, LineIndex] = {}
    stop_ids: dict[int, list[str]] = {}
    infos: dict[int, DirectionInfo] = {}
    for d, geojson in rows:
        stop_rows = (
            await session.execute(
                select(RouteDirectionStop, Stop, func.ST_X(Stop.geom), func.ST_Y(Stop.geom))
                .join(Stop, Stop.id == RouteDirectionStop.stop_id)
                .where(RouteDirectionStop.route_direction_id == d.id)
                .order_by(RouteDirectionStop.sequence)
            )
        ).all()
        points: list[LonLat] = [(float(x), float(y)) for _, _, x, y in stop_rows]
        stops = tuple(
            StopModel(
                stop_id=stop.id,
                name=stop.name,
                lon=round(lon, 6),
                lat=round(lat, 6),
                distance_m=rds.distance_along_m,
                scheduled_offset_s=rds.scheduled_offset_seconds,
            )
            for (rds, stop, _, _), (lon, lat) in zip(stop_rows, points, strict=True)
        )
        road = [rds.segment_duration_s for rds, *_ in stop_rows[1:]]
        models.append(
            DirectionModel(
                direction_id=d.direction_id,
                headsign=d.headsign,
                length_m=d.length_m,
                scheduled_headway_s=d.peak_headway_seconds,
                stops=stops,
                segment_road_s=(
                    tuple(t for t in road if t is not None)
                    if all(t is not None for t in road)
                    else None
                ),
            )
        )
        line = as_lonlats(json.loads(geojson)["coordinates"])
        lines[d.direction_id] = LineIndex(line, LocalPlane.around(points))
        stop_ids[d.direction_id] = [s.stop_id for s in stops]
        run_m = stops[-1].distance_m - stops[0].distance_m
        infos[d.direction_id] = DirectionInfo(
            first_stop_m=stops[0].distance_m,
            scheduled_headway_s=float(d.peak_headway_seconds),
            avg_speed_mps=run_m / max(d.scheduled_run_seconds, 1),
        )
    return WorldRoute(
        model=RouteModel(route_id=route_id, directions=(models[0], models[1])),
        lines=lines,
        stop_ids=stop_ids,
        directions=infos,
    )
