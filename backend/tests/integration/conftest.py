from collections.abc import AsyncIterator

import pytest
from arq import ArqRedis, create_pool
from arq.worker import Worker
from httpx import ASGITransport, AsyncClient

from app.auth.rate_limit import MemoryRateLimiter, get_rate_limiter
from app.main import create_app
from app.workers.queue import arq_redis_settings
from tests.conftest import Account
from tests.integration.sim_worker import running_worker


@pytest.fixture
async def client(db: None) -> AsyncIterator[AsyncClient]:
    """API client on the test database, with an in-memory rate limiter per test."""
    app = create_app()
    limiter = MemoryRateLimiter()
    app.dependency_overrides[get_rate_limiter] = lambda: limiter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client: AsyncClient, account: Account) -> str:
    resp = await client.post(
        "/auth/login", json={"email": account.email, "password": account.password}
    )
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# Simulation jobs (B11): a real ARQ sim-worker in-process, and a queue to submit to.
@pytest.fixture
async def sim_worker(database_url: str) -> AsyncIterator[Worker]:
    async with running_worker() as worker:
        yield worker


@pytest.fixture
async def job_queue(database_url: str) -> AsyncIterator[ArqRedis]:
    pool = await create_pool(arq_redis_settings())
    yield pool
    await pool.aclose()
