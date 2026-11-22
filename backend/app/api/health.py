"""`GET /health`: liveness plus dependency checks (Postgres, Redis)."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine

log = structlog.get_logger()
router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 2.0

CheckStatus = Literal["ok", "error"]


class HealthResponse(BaseModel):
    status: CheckStatus
    checks: dict[str, CheckStatus]


async def check_database() -> None:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_redis() -> None:
    client: Redis = Redis.from_url(get_settings().redis_url)
    try:
        await client.ping()
    finally:
        await client.aclose()


CHECKS: dict[str, Callable[[], Awaitable[None]]] = {
    "database": check_database,
    "redis": check_redis,
}


async def _run_check(name: str, check: Callable[[], Awaitable[None]]) -> CheckStatus:
    try:
        await asyncio.wait_for(check(), timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:  # health must report, never raise
        log.warning("health_check_failed", check=name, error=repr(exc))
        return "error"
    return "ok"


@router.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    results = await asyncio.gather(*(_run_check(n, c) for n, c in CHECKS.items()))
    checks = dict(zip(CHECKS, results, strict=True))
    overall: CheckStatus = "ok" if all(r == "ok" for r in results) else "error"
    if overall == "error":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status=overall, checks=checks)
