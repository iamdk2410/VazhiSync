"""FastAPI application entry point: `uvicorn app.main:app`."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api import agui, auth, driver, lab, live, officer, passenger, routes
from app.api.health import router as health_router
from app.auth.rate_limit import RedisRateLimiter
from app.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.harness.harness import ToolHarness
from app.live.hub import LiveHub
from app.observability.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    redis = Redis.from_url(get_settings().redis_url)
    app.state.redis = redis
    app.state.live_hub = LiveHub(redis)
    await app.state.live_hub.start()
    # The agent tool harness (B8); discovery is logged in the background so a down MCP
    # server never blocks API startup (it is simply marked unavailable).
    app.state.harness = ToolHarness.from_settings(get_settings(), get_sessionmaker())
    discovery = asyncio.create_task(app.state.harness.discover())
    yield
    discovery.cancel()
    await app.state.live_hub.stop()
    await redis.aclose()
    job_queue = getattr(app.state, "job_queue", None)
    if job_queue is not None:
        await job_queue.aclose()
    limiter = getattr(app.state, "rate_limiter", None)
    if isinstance(limiter, RedisRateLimiter):
        await limiter.aclose()
    await get_engine().dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging("api")
    app = FastAPI(title="VazhiSync API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(auth.router)
    app.include_router(officer.router)
    app.include_router(driver.router)
    app.include_router(passenger.router)
    app.include_router(routes.router)
    app.include_router(live.router)
    app.include_router(lab.router)
    app.include_router(agui.router)
    return app


app = create_app()
