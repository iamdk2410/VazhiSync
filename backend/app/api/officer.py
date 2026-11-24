from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    AlertsResponse,
    BunchingEventOut,
    WorldSpeedRequest,
    WorldStatus,
)
from app.auth.deps import require_role
from app.config import get_settings
from app.db.enums import BunchingEventStatus, Role
from app.db.models import BunchingEvent, User
from app.db.session import get_session
from app.live.channels import SPEED_KEY
from app.live.hub import LiveHub
from app.runner.world import parse_speed

log = structlog.get_logger()

router = APIRouter(prefix="/officer", tags=["officer"])

Officer = Annotated[User, Depends(require_role(Role.OFFICER))]


@router.get("/alerts", response_model=AlertsResponse)
async def list_alerts(
    _: Officer, session: Annotated[AsyncSession, Depends(get_session)]
) -> AlertsResponse:
    """Open bunching events, newest first (the officer's alert queue)."""
    rows = await session.scalars(
        select(BunchingEvent)
        .where(BunchingEvent.status.in_([BunchingEventStatus.OPEN, BunchingEventStatus.HANDLING]))
        .order_by(BunchingEvent.detected_at.desc())
        .limit(100)
    )
    return AlertsResponse(alerts=[BunchingEventOut.model_validate(r) for r in rows])


def get_redis(request: Request) -> Redis:
    """The app's shared Redis client (created on first use)."""
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if redis is None:
        redis = Redis.from_url(get_settings().redis_url)
        request.app.state.redis = redis
    return redis


_LIVE_UNAVAILABLE = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Live world unavailable"
)


@router.get("/world", response_model=WorldStatus)
async def world_status(
    request: Request, _: Officer, redis: Annotated[Redis, Depends(get_redis)]
) -> WorldStatus:
    """The live world at a glance: simulated clock, speed, vehicles and open events."""
    try:
        speed = parse_speed(await redis.get(SPEED_KEY))
    except RedisError as exc:
        log.warning("world_status_unavailable", error=repr(exc))
        raise _LIVE_UNAVAILABLE from None
    hub: LiveHub | None = getattr(request.app.state, "live_hub", None)
    frame = await hub.latest() if hub else None
    return WorldStatus(
        route_id=frame.route_id if frame else None,
        sim_time=frame.sim_time if frame else None,
        speed=speed,
        vehicles=len(frame.vehicles) if frame else 0,
        stale_vehicles=sum(v.is_stale for v in frame.vehicles) if frame else 0,
        open_events=len(frame.events) if frame else 0,
    )


@router.put("/world/speed", response_model=WorldSpeedRequest)
async def set_world_speed(
    body: WorldSpeedRequest, officer: Officer, redis: Annotated[Redis, Depends(get_redis)]
) -> WorldSpeedRequest:
    """Run the live world at 1x, 5x or 10x; the runner picks it up on its next tick."""
    try:
        await redis.set(SPEED_KEY, str(body.speed))
    except RedisError as exc:
        log.warning("world_speed_unavailable", error=repr(exc))
        raise _LIVE_UNAVAILABLE from None
    log.info("world_speed_set", speed=body.speed, officer_id=str(officer.id))
    return body
