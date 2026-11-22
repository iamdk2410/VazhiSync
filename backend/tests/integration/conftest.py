from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.rate_limit import MemoryRateLimiter, get_rate_limiter
from app.main import create_app
from tests.conftest import Account


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
