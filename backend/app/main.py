"""FastAPI application entry point: `uvicorn app.main:app`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api import auth, driver, live, officer, passenger, routes
from app.api.health import router as health_router
from app.auth.rate_limit import RedisRateLimiter
from app.config import get_settings
from app.db.session import get_engine
from app.live.hub import LiveHub
from app.observability.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    redis = Redis.from_url(get_settings().redis_url)
    app.state.redis = redis
    app.state.live_hub = LiveHub(redis)
    await app.state.live_hub.start()
    yield
    await app.state.live_hub.stop()
    await redis.aclose()
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
    return app


app = create_app()
