"""B2 acceptance: GET /routes/{id} returns the stops and shape as GeoJSON."""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db.models import RouteDirectionStop, Stop, Vehicle
from app.db.session import get_sessionmaker
from app.gtfs.import_gtfs import prepare
from app.gtfs.route_config import ROUTES_DIR
from app.gtfs.writer import write_route
from tests.conftest import Account
from tests.gtfs_fixtures import write_feed, write_route_config
from tests.integration.conftest import bearer, login

pytestmark = pytest.mark.services


async def _import(tmp_path: Path) -> None:
    routes_dir = tmp_path / "routes"
    write_route_config(routes_dir)
    route = prepare("T1", write_feed(tmp_path / "feed.zip"), routes_dir)
    async with get_sessionmaker()() as session:
        await write_route(session, route)


async def test_route_returns_stops_and_shape_as_geojson(
    client: AsyncClient, accounts: dict[str, Account], tmp_path: Path
) -> None:
    await _import(tmp_path)
    resp = await client.get(
        "/routes/T1", headers=bearer(await login(client, accounts["passenger"]))
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "T1"
    assert body["attribution"] is None  # stop-to-stop lines: no OSM data used
    out, back = body["directions"]
    assert out["shape"]["type"] == "Feature"
    assert out["shape"]["geometry"]["type"] == "LineString"
    assert len(out["shape"]["geometry"]["coordinates"]) >= 2
    stops = out["stops"]
    assert stops["type"] == "FeatureCollection"
    assert len(stops["features"]) == 16
    first = stops["features"][0]
    assert first["geometry"]["type"] == "Point"
    lon, lat = first["geometry"]["coordinates"]
    assert (lon, lat) == pytest.approx((80.25, 13.05), abs=1e-5)  # [lon, lat] order
    seqs = [f["properties"]["sequence"] for f in stops["features"]]
    assert seqs == list(range(1, 17))
    assert back["stops"]["features"][0]["id"] == "S15"
    assert out["peak_headway_seconds"] == 300


async def test_every_role_can_read_routes(
    client: AsyncClient, accounts: dict[str, Account], tmp_path: Path
) -> None:
    await _import(tmp_path)
    for account in accounts.values():
        h = bearer(await login(client, account))
        assert (await client.get("/routes", headers=h)).json()["routes"][0]["id"] == "T1"


async def test_unknown_route_is_404(client: AsyncClient, accounts: dict[str, Account]) -> None:
    resp = await client.get(
        "/routes/NOPE", headers=bearer(await login(client, accounts["officer"]))
    )
    assert resp.status_code == 404


async def test_reimport_replaces_directions_and_keeps_vehicles(db: None, tmp_path: Path) -> None:
    await _import(tmp_path)
    async with get_sessionmaker()() as session:
        session.add(Vehicle(fleet_number="TN-T1-01", route_id="T1"))
        await session.commit()
    await _import(tmp_path)  # must not fail on the vehicle's foreign key
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(RouteDirectionStop)) == 32
        assert await session.scalar(select(func.count()).select_from(Stop)) == 16
        assert await session.scalar(select(func.count()).select_from(Vehicle)) == 1


@pytest.mark.skipif(
    not (ROUTES_DIR.parent / "chennai-unified-gtfs.zip").is_file(),
    reason="real Chennai GTFS zip not downloaded (not in the repo/CI)",
)
def test_real_route_32b_passes_validation() -> None:
    route = prepare("32B")
    assert [len(d.stops) for d in route.directions] == [21, 22]
    assert all(240 <= d.peak_headway_seconds <= 420 for d in route.directions)
    assert all(d.shape_source == "osrm-snapped" for d in route.directions)
    assert max(s.offset_from_shape_m for d in route.directions for s in d.stops) < 40
