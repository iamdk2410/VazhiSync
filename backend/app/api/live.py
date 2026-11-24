"""Live WebSocket: `/ws/live`, with role-based filtering on the server (rule 10).

Protocol (JSON text messages):
1. client connects, then sends `{"type": "auth", "token": "<access token>"}` within 5 s.
   The token is never put in the URL, so it can't end up in access logs or browser history.
2. server answers `{"type": "ready", "role": ..., "vehicle_id": ...}` and then streams
   frames filtered for that role (`app.live.schemas.project`).
3. the server closes with 4401 on a missing/invalid/expired token or an inactive user.
"""

import asyncio
import contextlib
import time
import uuid
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.auth.security import InvalidTokenError, TokenPayload, decode_access_token
from app.db.enums import Role
from app.db.models import DriverAssignment, User
from app.db.session import get_sessionmaker
from app.live.hub import LiveHub, Subscriber
from app.live.schemas import project

log = structlog.get_logger()

router = APIRouter(tags=["live"])

AUTH_TIMEOUT_SECONDS = 5.0
CLOSE_UNAUTHORIZED = 4401
CLOSE_UNAVAILABLE = 1011


class AuthMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["auth"]
    token: Annotated[str, Field(min_length=1, max_length=4096)]


class ReadyMessage(BaseModel):
    type: Literal["ready"] = "ready"
    role: Role
    vehicle_id: uuid.UUID | None


async def _authenticate(ws: WebSocket) -> tuple[User, TokenPayload] | None:
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
        payload = decode_access_token(AuthMessage.model_validate_json(raw).token)
    except (TimeoutError, ValidationError, InvalidTokenError):
        return None
    async with get_sessionmaker()() as session:
        user = await session.get(User, payload.sub)
    # Same checks as the HTTP dependency: active user whose role still matches the token.
    if user is None or not user.is_active or user.role != payload.role:
        return None
    return user, payload


async def _driver_vehicle(user: User) -> uuid.UUID | None:
    async with get_sessionmaker()() as session:
        vehicle: uuid.UUID | None = await session.scalar(
            select(DriverAssignment.vehicle_id).where(
                DriverAssignment.driver_id == user.id, DriverAssignment.ends_at.is_(None)
            )
        )
    return vehicle


async def _stream(ws: WebSocket, sub: Subscriber, hub: LiveHub, expires_at: int) -> None:
    latest = await hub.latest()
    if latest is not None:
        sub.offer(latest)
    while True:
        remaining = expires_at - time.time()
        if remaining <= 0:
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="token expired")
            return
        try:
            frame = await asyncio.wait_for(sub.queue.get(), timeout=min(remaining, 30))
        except TimeoutError:
            continue
        await ws.send_text(project(frame, sub.role, sub.vehicle_id).model_dump_json())


async def _wait_for_disconnect(ws: WebSocket) -> None:
    """Clients send nothing after auth; reading detects the disconnect promptly."""
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            await ws.receive_text()


@router.websocket("/ws/live")
async def live(ws: WebSocket) -> None:
    await ws.accept()
    hub: LiveHub | None = getattr(ws.app.state, "live_hub", None)
    if hub is None:
        await ws.close(code=CLOSE_UNAVAILABLE, reason="live feed unavailable")
        return
    try:
        auth = await _authenticate(ws)
    except WebSocketDisconnect:
        return
    if auth is None:
        log.info("live_auth_failed")
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="unauthorized")
        return
    user, payload = auth
    vehicle_id = await _driver_vehicle(user) if user.role == Role.DRIVER else None
    await ws.send_text(ReadyMessage(role=user.role, vehicle_id=vehicle_id).model_dump_json())
    sub = hub.subscribe(user.role, vehicle_id)
    log.info("live_connected", user_id=str(user.id), role=user.role)
    tasks = [
        asyncio.create_task(_stream(ws, sub, hub, payload.exp)),
        asyncio.create_task(_wait_for_disconnect(ws)),
    ]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await task
    finally:
        hub.unsubscribe(sub)
        log.info("live_disconnected", user_id=str(user.id))
