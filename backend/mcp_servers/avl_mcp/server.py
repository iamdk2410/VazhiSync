"""avl-mcp: what the vehicle-location (AVL) system observed.

Tools: get_vehicle_positions, get_headways. Resource: route://{route_id}/shape.
Age and staleness are computed here, server-side, at request time (rule 4).

Run: `python -m mcp_servers.avl_mcp.server` (port 8101).
"""

import json
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.api.routes import load_route_out
from app.config import get_settings
from app.db.session import get_sessionmaker
from mcp_servers import observations
from mcp_servers.common import add_health_route, serve
from mcp_servers.schemas import Headways, VehiclePositions

SERVICE_NAME = "avl-mcp"
PORT = 8101

READ_ONLY = ToolAnnotations(
    title=None, read_only_hint=True, idempotent_hint=True, open_world_hint=False
)
RouteIdArg = Annotated[
    str | None,
    Field(
        description="Route id, e.g. '32B'. Defaults to the route the live world runs.",
        pattern=r"^[A-Za-z0-9_-]{1,32}$",
    ),
]

server = MCPServer(
    SERVICE_NAME,
    instructions=(
        "Vehicle location (AVL) data for one Chennai MTC route: latest GPS fixes with their "
        "age and a server-computed is_stale flag, and headways between consecutive buses. "
        "Never instruct or rely on a stale vehicle."
    ),
)
add_health_route(server)


def _route(route_id: str | None) -> str:
    return route_id or get_settings().world_route_id


@server.tool(
    annotations=READ_ONLY.model_copy(update={"title": "Vehicle positions"}),
    description=(
        "Latest GPS fix for every bus on the route, with age_seconds and is_stale computed by "
        "the server at request time. Buses that never reported are listed in `missing`."
    ),
)
async def get_vehicle_positions(route_id: RouteIdArg = None) -> VehiclePositions:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        return await observations.vehicle_positions(
            session, _route(route_id), observations.utc_now(), settings.stale_gps_seconds
        )


@server.tool(
    annotations=READ_ONLY.model_copy(update={"title": "Headways"}),
    description=(
        "Headway between each pair of consecutive in-service buses, per direction: gap, "
        "headway vs scheduled, status (ok / closing / bunched / stale) and data quality. "
        "Pairs involving a stale fix are never classified as bunched."
    ),
)
async def get_headways(route_id: RouteIdArg = None) -> Headways:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        return await observations.headways(
            session,
            _route(route_id),
            observations.utc_now(),
            settings.stale_gps_seconds,
            settings.bunching_ratio,
            settings.closing_ratio,
        )


@server.resource(
    "route://{route_id}/shape",
    name="route_shape",
    title="Route shape",
    description="The route's two directions and stops as GeoJSON (with OSM attribution).",
    mime_type="application/geo+json",
)
async def route_shape(route_id: str) -> str:
    async with get_sessionmaker()() as session:
        route = await load_route_out(session, route_id)
    if route is None:
        raise ResourceNotFoundError(f"unknown route {route_id}")
    return json.dumps(route.model_dump(mode="json"))


if __name__ == "__main__":
    serve(server, SERVICE_NAME, PORT)
