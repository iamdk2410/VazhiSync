"""The per-role endpoints return only the caller's own data."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.enums import BunchingEventStatus, BunchingSeverity
from app.db.models import BunchingEvent, DriverAssignment, User, Vehicle
from app.db.session import get_sessionmaker
from tests.conftest import Account
from tests.integration.conftest import bearer, login

pytestmark = pytest.mark.services


async def test_passenger_favourites_round_trip(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    h = bearer(await login(client, accounts["passenger"]))
    put = await client.put("/passenger/favourites/STOP_1", json={"alert_minutes": 7}, headers=h)
    assert put.status_code == 200
    assert put.json()["alert_minutes"] == 7
    # Saving again updates instead of duplicating.
    await client.put("/passenger/favourites/STOP_1", json={"alert_minutes": 3}, headers=h)
    favs = (await client.get("/passenger/favourites", headers=h)).json()["favourites"]
    assert [(f["stop_id"], f["alert_minutes"]) for f in favs] == [("STOP_1", 3)]
    assert (await client.delete("/passenger/favourites/STOP_1", headers=h)).status_code == 204
    assert (await client.get("/passenger/favourites", headers=h)).json()["favourites"] == []


async def test_invalid_stop_id_rejected(client: AsyncClient, accounts: dict[str, Account]) -> None:
    h = bearer(await login(client, accounts["passenger"]))
    resp = await client.put("/passenger/favourites/bad%20id!", json={}, headers=h)
    assert resp.status_code == 422


async def test_driver_sees_only_own_assignment(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    async with get_sessionmaker()() as session:
        driver = await session.scalar(select(User).where(User.email == accounts["driver"].email))
        assert driver is not None
        bus = Vehicle(fleet_number="TN01AN0001", route_id="R21G")
        session.add(bus)
        await session.flush()
        session.add(DriverAssignment(driver_id=driver.id, vehicle_id=bus.id))
        await session.commit()
    resp = await client.get(
        "/driver/assignment", headers=bearer(await login(client, accounts["driver"]))
    )
    assert resp.json()["assignment"]["fleet_number"] == "TN01AN0001"


async def test_driver_without_assignment_gets_null(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    resp = await client.get(
        "/driver/assignment", headers=bearer(await login(client, accounts["driver"]))
    )
    assert resp.json() == {"assignment": None}


async def test_officer_alerts_list_only_open_events(
    client: AsyncClient, accounts: dict[str, Account]
) -> None:
    async with get_sessionmaker()() as session:
        a = Vehicle(fleet_number="TN01AN0002", route_id="R21G")
        b = Vehicle(fleet_number="TN01AN0003", route_id="R21G")
        session.add_all([a, b])
        await session.flush()
        for status in (BunchingEventStatus.OPEN, BunchingEventStatus.RESOLVED):
            session.add(
                BunchingEvent(
                    route_id="R21G",
                    leader_vehicle_id=a.id,
                    follower_vehicle_id=b.id,
                    headway_seconds=120,
                    scheduled_headway_seconds=600,
                    severity=BunchingSeverity.BUNCHED,
                    status=status,
                )
            )
        await session.commit()
    resp = await client.get(
        "/officer/alerts", headers=bearer(await login(client, accounts["officer"]))
    )
    assert [a["status"] for a in resp.json()["alerts"]] == ["open"]
