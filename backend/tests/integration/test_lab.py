"""B5 through the API, the worker job and the live world (fake LLM; Postgres + Redis).

The real-LLM acceptance test is tests/integration/test_scenario_llm.py (marker `ollama`).
"""

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.api.deps import get_job_queue
from app.auth.rate_limit import MemoryRateLimiter, get_rate_limiter
from app.config import get_settings
from app.db.enums import BunchingEventStatus, ScenarioStatus
from app.db.models import BunchingEvent, LlmUsage, ScenarioRecord
from app.db.session import get_sessionmaker
from app.gtfs.import_gtfs import prepare
from app.gtfs.writer import write_route
from app.lab.library import sync_golden
from app.live.channels import SCENARIO_KEY
from app.llm.ollama import LlmError
from app.main import create_app
from app.runner.route_loader import WorldRoute, load_world_route
from app.runner.world import WorldRunner
from app.workers.scenarios import run_generation
from scenario_gen.director import LlmReply
from tests.conftest import Account
from tests.gtfs_fixtures import write_feed, write_route_config
from tests.integration.conftest import bearer, login
from tests.integration.test_live_world import FakeClock

pytestmark = pytest.mark.services

# A valid plan for the synthetic T1 route (stops S0..S15; direction 1 runs S15 -> S0).
PLAN: dict[str, Any] = {
    "name": "Rainy match night",
    "summary": "Heavy rain and a crowd at stops S5 and S6 heading north.",
    "start_time": "18:00",
    "duration_min": 60,
    "demand_multiplier": 1.2,
    "weather": [
        {
            "kind": "heavy_rain",
            "start_min": 0,
            "end_min": 45,
            "speed_factor": 0.7,
            "demand_factor": 1.2,
        }
    ],
    "surges": [
        {
            "direction_id": 0,
            "stop_ids": ["S5", "S6"],
            "start_min": 5,
            "end_min": 40,
            "multiplier": 4,
            "reason": "match crowd",
        }
    ],
    "gps_dropouts": [{"bus_number": 2, "at_min": 0, "duration_min": 30}],
}


class FakeModel:
    def __init__(self, replies: list[str] | None = None, error: LlmError | None = None) -> None:
        self.replies = replies or [json.dumps(PLAN)]
        self.error = error

    async def chat(self, messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> LlmReply:
        if self.error:
            raise self.error
        return LlmReply(self.replies.pop(0), "ollama/fake", 900, 300, 1234)


class FakeQueue:
    def __init__(self, fail: bool = False) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        if self.fail:
            raise OSError("redis down")
        self.jobs.append((name, args))


@pytest.fixture
async def t1(db: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorldRoute:
    monkeypatch.setenv("WORLD_ROUTE_ID", "T1")
    get_settings.cache_clear()
    routes_dir = tmp_path / "routes"
    write_route_config(routes_dir)
    imported = prepare("T1", write_feed(tmp_path / "feed.zip"), routes_dir)
    async with get_sessionmaker()() as session:
        await write_route(session, imported)
        return await load_world_route(session, "T1")


@pytest.fixture
async def redis(database_url: str) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(get_settings().redis_url)
    await client.flushdb()  # database 15: test data only
    yield client
    await client.aclose()


@pytest.fixture
async def lab(t1: WorldRoute, redis: Redis) -> AsyncIterator[tuple[AsyncClient, FakeQueue]]:
    app = create_app()
    queue = FakeQueue()
    app.dependency_overrides[get_rate_limiter] = lambda: MemoryRateLimiter()
    app.dependency_overrides[get_job_queue] = lambda: queue
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, queue


async def _record(scenario_id: str) -> ScenarioRecord:
    async with get_sessionmaker()() as session:
        record = await session.get(ScenarioRecord, uuid.UUID(scenario_id))
    assert record is not None
    return record


async def test_generate_preview_and_fetch(
    lab: tuple[AsyncClient, FakeQueue], accounts: dict[str, Account]
) -> None:
    client, queue = lab
    officer = bearer(await login(client, accounts["officer"]))
    resp = await client.post(
        "/lab/scenarios", json={"description": "Monsoon evening, IPL at Chepauk"}, headers=officer
    )
    assert resp.status_code == 202, resp.text
    created = resp.json()
    assert created["status"] == "pending" and created["route_id"] == "T1"
    assert queue.jobs == [("generate_scenario", (created["id"],))]

    assert await run_generation(uuid.UUID(created["id"]), FakeModel()) == ScenarioStatus.READY
    body = (await client.get(f"/lab/scenarios/{created['id']}", headers=officer)).json()
    assert body["status"] == "ready" and body["attempts"] == 1
    assert body["plan"]["name"] == "Rainy match night"
    preview = body["preview"]
    assert preview["minutes"] == 60
    assert preview["scenario"]["avg_wait_s"] > preview["baseline"]["avg_wait_s"]  # it matters

    async with get_sessionmaker()() as session:
        usage = (await session.scalars(select(LlmUsage))).all()
    assert [(u.agent, u.provider, u.success, u.input_tokens) for u in usage] == [
        ("scenario_director", "ollama", True, 900)
    ]

    listed = (await client.get("/lab/scenarios", headers=officer)).json()["scenarios"]
    assert [s["id"] for s in listed] == [created["id"]]


async def test_only_officers_use_the_lab(
    lab: tuple[AsyncClient, FakeQueue], accounts: dict[str, Account]
) -> None:
    client, _ = lab
    for role in ("driver", "passenger"):
        headers = bearer(await login(client, accounts[role]))
        assert (await client.get("/lab/scenarios", headers=headers)).status_code == 403
        resp = await client.post(
            "/lab/scenarios", json={"description": "heavy rain"}, headers=headers
        )
        assert resp.status_code == 403


async def test_implausible_output_fails_after_two_regenerations(
    lab: tuple[AsyncClient, FakeQueue], accounts: dict[str, Account]
) -> None:
    client, _ = lab
    officer = bearer(await login(client, accounts["officer"]))
    created = (
        await client.post("/lab/scenarios", json={"description": "chaos"}, headers=officer)
    ).json()
    bad = json.dumps(PLAN | {"surges": [PLAN["surges"][0] | {"stop_ids": ["CHEPAUK"]}]})
    status = await run_generation(uuid.UUID(created["id"]), FakeModel([bad] * 3))
    assert status == ScenarioStatus.FAILED
    record = await _record(created["id"])
    assert record.attempts == 3 and record.plan is None
    assert record.problems and "CHEPAUK" in record.problems[0]
    async with get_sessionmaker()() as session:
        failed = await session.scalar(
            select(func.count()).select_from(LlmUsage).where(LlmUsage.success.is_(False))
        )
    assert failed == 3


async def test_llm_down_fails_cleanly(
    lab: tuple[AsyncClient, FakeQueue], accounts: dict[str, Account]
) -> None:
    client, _ = lab
    officer = bearer(await login(client, accounts["officer"]))
    created = (
        await client.post("/lab/scenarios", json={"description": "heavy rain"}, headers=officer)
    ).json()
    model = FakeModel(error=LlmError("Ollama unreachable", "unavailable"))
    assert await run_generation(uuid.UUID(created["id"]), model) == ScenarioStatus.FAILED
    record = await _record(created["id"])
    assert record.error is not None and "local LLM unavailable (unavailable)" in record.error
    # A second run of the same job does nothing (it is no longer pending).
    assert await run_generation(uuid.UUID(created["id"]), FakeModel()) == ScenarioStatus.FAILED


async def test_generation_is_limited_and_queue_outage_is_reported(
    lab: tuple[AsyncClient, FakeQueue], accounts: dict[str, Account]
) -> None:
    client, queue = lab
    officer = bearer(await login(client, accounts["officer"]))
    for _ in range(3):
        resp = await client.post(
            "/lab/scenarios", json={"description": "heavy rain"}, headers=officer
        )
        assert resp.status_code == 202
    resp = await client.post("/lab/scenarios", json={"description": "heavy rain"}, headers=officer)
    assert resp.status_code == 429  # three already waiting for the local LLM

    async with get_sessionmaker()() as session:
        for record in (await session.scalars(select(ScenarioRecord))).all():
            record.status = ScenarioStatus.FAILED
        await session.commit()
    queue.fail = True
    resp = await client.post("/lab/scenarios", json={"description": "heavy rain"}, headers=officer)
    assert resp.status_code == 503
    assert "id" not in resp.json()  # a 503, not a scenario
    async with get_sessionmaker()() as session:
        errors = (await session.scalars(select(ScenarioRecord.error))).all()
    assert "job queue unavailable" in errors


@pytest.mark.parametrize(
    "body", [{"description": "x"}, {"description": "heavy rain", "seed": 1}, {}]
)
async def test_bad_requests_are_rejected(
    lab: tuple[AsyncClient, FakeQueue], accounts: dict[str, Account], body: dict[str, Any]
) -> None:
    client, _ = lab
    officer = bearer(await login(client, accounts["officer"]))
    assert (await client.post("/lab/scenarios", json=body, headers=officer)).status_code == 422


async def test_golden_scenarios_are_synced_once(t1: WorldRoute, tmp_path: Path) -> None:
    golden_dir = tmp_path / "golden"
    golden_dir.mkdir()
    golden = {
        "key": "t1_rain",
        "description": "Rain on the test route.",
        "route_id": "T1",
        "seed": 3,
        "plan": PLAN,
        "expected": [{"metric": "avg_wait_s", "relation": "higher"}],
    }
    (golden_dir / "t1_rain.json").write_text(json.dumps(golden), encoding="utf-8")
    async with get_sessionmaker()() as session:
        assert await sync_golden(session, "T1", golden_dir) == 1
        assert await sync_golden(session, "T1", golden_dir) == 1  # idempotent
        rows = (await session.scalars(select(ScenarioRecord))).all()
    assert len(rows) == 1
    assert rows[0].golden_key == "t1_rain" and rows[0].status == ScenarioStatus.READY
    assert rows[0].preview is not None


# ------------------------------------------------------------------ live apply


def _runner(route: WorldRoute, redis: Redis, clock: FakeClock) -> WorldRunner:
    return WorldRunner(
        route,
        get_sessionmaker(),
        redis,
        seed=1,
        speed=10,
        stale_after_s=60,
        bunching_ratio=0.5,
        closing_ratio=0.75,
        queue_every_s=10,
        retention=timedelta(hours=6),
        clock=clock,
    )


async def test_applying_a_scenario_restarts_the_live_world(
    lab: tuple[AsyncClient, FakeQueue], t1: WorldRoute, redis: Redis, accounts: dict[str, Account]
) -> None:
    client, _ = lab
    officer = bearer(await login(client, accounts["officer"]))
    created = (
        await client.post("/lab/scenarios", json={"description": "heavy rain"}, headers=officer)
    ).json()

    # Not ready yet: can't be applied.
    assert (
        await client.post(f"/lab/scenarios/{created['id']}/apply", headers=officer)
    ).status_code == 409
    await run_generation(uuid.UUID(created["id"]), FakeModel())

    clock = FakeClock()
    runner = _runner(t1, redis, clock)
    await runner.setup()
    clock.advance(2)
    normal = await runner.tick(2)
    assert normal.scenario is None and normal.sim_time.startswith("08:")
    async with get_sessionmaker()() as session:  # an event open in the old world
        leader, follower = list(runner.vehicle_ids.values())[:2]
        session.add(
            BunchingEvent(
                route_id="T1",
                leader_vehicle_id=leader,
                follower_vehicle_id=follower,
                headway_seconds=30,
                scheduled_headway_seconds=300,
                severity="bunched",
            )
        )
        await session.commit()
    await runner.setup()  # picks the open event up

    resp = await client.post(f"/lab/scenarios/{created['id']}/apply", headers=officer)
    assert resp.status_code == 202 and resp.json()["name"] == "Rainy match night"
    clock.advance(2)
    frame = await runner.tick(2)
    assert frame.scenario == "Rainy match night"
    assert frame.sim_time == "18:00:20" and frame.sim_elapsed_s == 20  # restarted at 18:00
    assert "T1-02" not in {v.fleet_number for v in frame.vehicles}  # its GPS is out
    assert frame.events == []
    async with get_sessionmaker()() as session:
        statuses = (await session.scalars(select(BunchingEvent.status))).all()
    assert statuses == [BunchingEventStatus.DISMISSED]

    clock.advance(2)
    assert (await runner.tick(2)).sim_elapsed_s == 40  # no second reset for the same token

    current = (await client.get("/lab/world/scenario", headers=officer)).json()
    assert current == {"scenario_id": created["id"], "name": "Rainy match night", "faults": []}
    assert (await client.delete("/lab/world/scenario", headers=officer)).status_code == 200
    clock.advance(2)
    back = await runner.tick(2)
    assert back.scenario is None and back.sim_time == "08:00:20"
    assert json.loads(await redis.get(SCENARIO_KEY) or "{}")["plan"] is None
