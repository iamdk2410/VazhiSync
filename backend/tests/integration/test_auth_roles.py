"""B3 acceptance: each role logs in and gets a 403 on another role's endpoints, and
registering cannot create an officer."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update

from app.config import get_settings
from app.db.enums import Role
from app.db.models import User
from app.db.session import get_sessionmaker
from tests.conftest import Account
from tests.integration.conftest import bearer, login

pytestmark = pytest.mark.services

ROLE_ENDPOINTS = {
    "officer": "/officer/alerts",
    "driver": "/driver/assignment",
    "passenger": "/passenger/favourites",
}


async def test_each_role_logs_in_with_its_role(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    for role, account in accounts.items():
        resp = await client.post(
            "/auth/login", json={"email": account.email, "password": account.password}
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == role
        me = await client.get("/auth/me", headers=bearer(resp.json()["access_token"]))
        assert me.json()["role"] == role


@pytest.mark.parametrize("role", list(ROLE_ENDPOINTS))
async def test_role_reaches_own_endpoint_and_is_forbidden_elsewhere(
    client: AsyncClient, accounts: dict[str, Account], role: str
) -> None:
    token = await login(client, accounts[role])
    for owner, path in ROLE_ENDPOINTS.items():
        resp = await client.get(path, headers=bearer(token))
        expected = 200 if owner == role else 403
        assert resp.status_code == expected, f"{role} -> {path}: {resp.status_code}"


async def test_register_creates_a_passenger(client: AsyncClient, db: None) -> None:
    resp = await client.post(
        "/auth/register",
        json={"email": "New.Rider@Example.com", "password": "rider-pass-1", "display_name": "R"},
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "passenger"
    assert resp.json()["email"] == "new.rider@example.com"


@pytest.mark.parametrize(
    "extra",
    [{"role": "officer"}, {"role": "driver"}, {"role": "passenger"}, {"is_admin": True}],
)
async def test_register_cannot_choose_a_role(
    client: AsyncClient, db: None, extra: dict[str, object]
) -> None:
    body = {"email": "sneaky@example.com", "password": "sneaky-pass-1", "display_name": "S"}
    resp = await client.post("/auth/register", json=body | extra)
    assert resp.status_code == 422
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(User)) == 0


async def test_no_officer_can_ever_come_from_register(client: AsyncClient, db: None) -> None:
    for i in range(3):
        await client.post(
            "/auth/register",
            json={"email": f"u{i}@example.com", "password": "password-123", "display_name": "U"},
        )
    async with get_sessionmaker()() as session:
        roles = set(await session.scalars(select(User.role)))
    assert roles == {Role.PASSENGER}


async def test_duplicate_email_is_case_insensitive(client: AsyncClient, db: None) -> None:
    body = {"email": "dup@example.com", "password": "password-123", "display_name": "D"}
    assert (await client.post("/auth/register", json=body)).status_code == 201
    again = body | {"email": "DUP@example.com"}
    assert (await client.post("/auth/register", json=again)).status_code == 409


async def test_missing_or_bad_token_is_401(client: AsyncClient, db: None) -> None:
    assert (await client.get("/officer/alerts")).status_code == 401
    bad = await client.get("/officer/alerts", headers=bearer("not-a-jwt"))
    assert bad.status_code == 401


async def test_token_is_rejected_after_role_change(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    token = await login(client, accounts["driver"])
    async with get_sessionmaker()() as session:
        await session.execute(
            update(User).where(User.email == accounts["driver"].email).values(role=Role.OFFICER)
        )
        await session.commit()
    # The old token says "driver"; the database now says "officer": neither role is trusted.
    assert (await client.get("/officer/alerts", headers=bearer(token))).status_code == 401
    assert (await client.get("/driver/assignment", headers=bearer(token))).status_code == 401


async def test_wrong_password_and_unknown_email_look_the_same(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    wrong = await client.post(
        "/auth/login", json={"email": accounts["officer"].email, "password": "nope-nope"}
    )
    unknown = await client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": "nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_login_never_rejects_an_existing_address_format(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    """Accounts use reserved test domains; login must still work (bug found in live run)."""
    assert accounts["officer"].email.endswith(".vazhisync")
    resp = await client.post(
        "/auth/login",
        json={"email": accounts["officer"].email, "password": accounts["officer"].password},
    )
    assert resp.status_code == 200


async def test_inactive_user_cannot_log_in(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(User).where(User.email == accounts["officer"].email).values(is_active=False)
        )
        await session.commit()
    resp = await client.post(
        "/auth/login",
        json={"email": accounts["officer"].email, "password": accounts["officer"].password},
    )
    assert resp.status_code == 401


async def test_login_is_rate_limited(
    client: AsyncClient, accounts: dict[str, Account], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    body = {"email": accounts["passenger"].email, "password": "wrong-password"}
    codes = [(await client.post("/auth/login", json=body)).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    limited = await client.post("/auth/login", json=body)
    assert int(limited.headers["Retry-After"]) >= 1


async def test_register_is_rate_limited(
    client: AsyncClient, db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTH_RATE_LIMIT_PER_MINUTE", "2")
    get_settings.cache_clear()
    codes = []
    for i in range(3):
        body = {"email": f"r{i}@example.com", "password": "password-123", "display_name": "R"}
        codes.append((await client.post("/auth/register", json=body)).status_code)
    assert codes == [201, 201, 429]
