"""Route network for the map: stops and road geometry as GeoJSON (any signed-in role)."""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    HeadwayHourOut,
    LineStringGeometry,
    PointGeometry,
    RouteDirectionOut,
    RouteOut,
    RoutesResponse,
    RouteSummary,
    ShapeFeature,
    ShapeProperties,
    StopFeature,
    StopFeatureCollection,
    StopProperties,
)
from app.auth.deps import ANY_ROLE, require_role
from app.db.models import Route, RouteDirection, RouteDirectionStop, Stop, User
from app.db.session import get_session
from app.gtfs.builder import SHAPE_SNAPPED

router = APIRouter(prefix="/routes", tags=["routes"])

AnyUser = Annotated[User, Depends(require_role(*ANY_ROLE))]
Session = Annotated[AsyncSession, Depends(get_session)]
RouteIdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,32}$")]

OSM_ATTRIBUTION = "Route geometry (c) OpenStreetMap contributors (ODbL), snapped with OSRM"


@router.get("", response_model=RoutesResponse)
async def list_routes(_: AnyUser, session: Session) -> RoutesResponse:
    rows = await session.scalars(select(Route).order_by(Route.id))
    return RoutesResponse(routes=[RouteSummary.model_validate(r) for r in rows])


@router.get("/{route_id}", response_model=RouteOut)
async def get_route(route_id: RouteIdPath, _: AnyUser, session: Session) -> RouteOut:
    route = await session.get(Route, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Route not found")

    directions = (
        await session.execute(
            select(RouteDirection, func.ST_AsGeoJSON(RouteDirection.geom))
            .where(RouteDirection.route_id == route_id)
            .order_by(RouteDirection.direction_id)
        )
    ).all()
    stop_rows = (
        await session.execute(
            select(
                RouteDirectionStop,
                Stop.name,
                func.ST_X(Stop.geom),
                func.ST_Y(Stop.geom),
            )
            .join(Stop, Stop.id == RouteDirectionStop.stop_id)
            .where(RouteDirectionStop.route_direction_id.in_([d.id for d, _ in directions]))
            .order_by(RouteDirectionStop.route_direction_id, RouteDirectionStop.sequence)
        )
    ).all()

    out: list[RouteDirectionOut] = []
    for direction, geojson in directions:
        line = LineStringGeometry.model_validate(json.loads(geojson))
        features = [
            StopFeature(
                id=rds.stop_id,
                geometry=PointGeometry(coordinates=(lon, lat)),
                properties=StopProperties(
                    stop_id=rds.stop_id,
                    name=name,
                    sequence=rds.sequence,
                    distance_along_m=rds.distance_along_m,
                    scheduled_offset_seconds=rds.scheduled_offset_seconds,
                ),
            )
            for rds, name, lon, lat in stop_rows
            if rds.route_direction_id == direction.id
        ]
        out.append(
            RouteDirectionOut(
                direction_id=direction.direction_id,
                headsign=direction.headsign,
                length_m=direction.length_m,
                scheduled_run_seconds=direction.scheduled_run_seconds,
                peak_headway_seconds=direction.peak_headway_seconds,
                trips_per_day=direction.trips_per_day,
                first_departure=direction.first_departure,
                last_departure=direction.last_departure,
                headway_profile=[HeadwayHourOut(**h) for h in direction.headway_profile],
                shape=ShapeFeature(
                    geometry=line,
                    properties=ShapeProperties(
                        direction_id=direction.direction_id,
                        headsign=direction.headsign,
                        length_m=direction.length_m,
                        shape_source=direction.shape_source,
                    ),
                ),
                stops=StopFeatureCollection(features=features),
            )
        )
    snapped = any(d.shape_source == SHAPE_SNAPPED for d, _ in directions)
    return RouteOut(
        id=route.id,
        short_name=route.short_name,
        long_name=route.long_name,
        agency=route.agency,
        attribution=OSM_ATTRIBUTION if snapped else None,
        directions=out,
    )
