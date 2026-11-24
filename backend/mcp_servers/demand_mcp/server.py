"""demand-mcp: passenger demand and the (guarded) channel to drivers.

Tools: get_boarding_estimates (read-only), send_driver_instruction (only the post-approval
dispatch node may call it, rule 5; it enforces its own guards regardless, rule 3).

Run: `python -m mcp_servers.demand_mcp.server` (port 8102).
"""

import uuid
from typing import Annotated

import structlog
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.live.channels import INSTRUCTION_CHANNEL
from mcp_servers import observations
from mcp_servers.common import add_health_route, serve
from mcp_servers.demand import boarding_estimates
from mcp_servers.dispatch import send_instruction
from mcp_servers.schemas import Action, BoardingEstimates, InstructionId, InstructionSent

log = structlog.get_logger()

SERVICE_NAME = "demand-mcp"
PORT = 8102

server = MCPServer(
    SERVICE_NAME,
    instructions=(
        "Passenger demand estimates per stop, and send_driver_instruction, which only "
        "accepts an officer-approved action for a bus with fresh GPS."
    ),
)
add_health_route(server)

_redis: Redis | None = None


async def _publish(sent: InstructionSent) -> None:
    """Best effort: the instruction is already stored; B12 also delivers from the table."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url)
    try:
        await _redis.publish(INSTRUCTION_CHANNEL, sent.model_dump_json())
    except RedisError as exc:
        log.warning(
            "instruction_publish_failed", instruction_id=sent.instruction_id, error=repr(exc)
        )


@server.tool(
    annotations=ToolAnnotations(
        title="Boarding estimates", read_only_hint=True, idempotent_hint=True, open_world_hint=False
    ),
    description=(
        "Per stop: people waiting now (counted), arrivals per minute estimated from recent "
        "counts, and the boardings expected within `horizon_minutes`. data_quality is "
        "'stale' when the latest count is too old and 'no_data' when there is none."
    ),
)
async def get_boarding_estimates(
    route_id: Annotated[
        str | None,
        Field(
            description="Route id; defaults to the live world's route.",
            pattern=r"^[A-Za-z0-9_-]{1,32}$",
        ),
    ] = None,
    direction_id: Annotated[
        int | None, Field(ge=0, le=1, description="Only this direction")
    ] = None,
    horizon_minutes: Annotated[int, Field(ge=1, le=30, description="Look-ahead window")] = 10,
) -> BoardingEstimates:
    settings = get_settings()
    async with get_sessionmaker()() as session:
        return await boarding_estimates(
            session,
            route_id or settings.world_route_id,
            direction_id,
            horizon_minutes,
            observations.utc_now(),
            settings.stale_gps_seconds,
        )


@server.tool(
    annotations=ToolAnnotations(
        title="Send driver instruction",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,  # replaying the same instruction_id never sends twice
        open_world_hint=True,  # it reaches a real driver
    ),
    description=(
        "Send an officer-approved action (hold / skip_stop / short_turn) to a bus's driver. "
        "Refused unless: approval_id is an approved decision for exactly this bus and action; "
        "the approval hasn't been used; the bus's GPS is fresh; and the action is valid for "
        "where the bus is now. instruction_id is an idempotency key: replaying the same "
        "request returns the original with status 'already_sent'."
    ),
)
async def send_driver_instruction(
    instruction_id: Annotated[InstructionId, Field(description="Unique idempotency key")],
    approval_id: Annotated[uuid.UUID, Field(description="The officer's approval")],
    vehicle_id: Annotated[uuid.UUID, Field(description="The bus to instruct")],
    action: Annotated[Action, Field(description="The approved action, exactly as approved")],
) -> InstructionSent:
    async with get_sessionmaker()() as session:
        return await send_instruction(
            session,
            instruction_id=instruction_id,
            approval_id=approval_id,
            vehicle_id=vehicle_id,
            action=action,
            now=observations.utc_now(),
            stale_after_s=get_settings().stale_gps_seconds,
            publish=_publish,
        )


if __name__ == "__main__":
    serve(server, SERVICE_NAME, PORT)
