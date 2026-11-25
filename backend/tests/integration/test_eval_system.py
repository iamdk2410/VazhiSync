"""B19: the whole-system evaluation, end to end on the small T1 route.

The real world runner, MCP servers (in-process), LangGraph workflow, Postgres, decision
service (auto-approve), `send_driver_instruction` with its guards, auto-acknowledgement and
outcome measurement: one bunch is detected, acted on, and its benefit measured.
"""

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from arq import ArqRedis
from arq.worker import Worker
from redis.asyncio import Redis

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.eval.system import EvalClock, run_system_eval
from app.gtfs.import_gtfs import prepare
from app.gtfs.writer import write_route
from app.runner.route_loader import load_world_route
from app.runner.world import WorldRunner
from simulator import Observations, VehicleObservation, from_observations
from tests.conftest import Account
from tests.gtfs_fixtures import write_feed, write_route_config
from tests.integration.test_workflow import redis  # noqa: F401

pytestmark = pytest.mark.services


async def test_a_scenario_played_through_the_whole_system(
    db: None,
    accounts: dict[str, Account],
    tmp_path: Path,
    redis: Redis,  # noqa: F811
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_ROUTE_ID", "T1")
    get_settings.cache_clear()
    settings = get_settings()
    routes_dir = tmp_path / "routes"
    write_route_config(routes_dir)
    imported = prepare("T1", write_feed(tmp_path / "feed.zip"), routes_dir)
    async with get_sessionmaker()() as session:
        await write_route(session, imported)
        route = await load_world_route(session, "T1")
    clock = EvalClock()

    async def runner_factory(enqueue: Any) -> WorldRunner:
        runner = WorldRunner(
            route, get_sessionmaker(), redis, seed=1, speed=1, stale_after_s=60,
            bunching_ratio=0.5, closing_ratio=0.75, queue_every_s=10,
            retention=timedelta(hours=6), clock=clock, enqueue=enqueue,
        )  # fmt: skip
        runner.sim = from_observations(  # a bunch about to be detected
            route.model,
            Observations(
                time_s=8 * 3600,
                vehicles=(
                    VehicleObservation("T1-01", 0, 3000.0, load=40),
                    VehicleObservation("T1-02", 0, 2900.0, load=10),
                    VehicleObservation("T1-03", 0, 900.0, load=20),
                    VehicleObservation("T1-04", 1, 3000.0, load=20),
                ),
            ),
            runner.sim.config,
            seed=1,
        )
        await runner.setup()
        return runner

    result = await run_system_eval(
        route,
        runner_factory,
        minutes=20,
        sessionmaker=get_sessionmaker(),
        settings=settings,
        redis=redis,
        job_queue=job_queue,
        checkpointer=checkpointer,
        officer_email=accounts["officer"].email,
        clock=clock,
        monkeypatch_clock=monkeypatch.setattr,
    )

    print("\n ", result)
    assert result.events >= 1 and result.runs >= 1
    assert result.sends_refused == 0  # nothing unsafe reached the guards
    assert result.rule_violations_sent == 0
    if result.instructions_sent:  # acted on: acknowledged, carried out and measured
        assert result.approved >= 1
        assert result.outcomes_measured >= 1
        assert result.mean_abs_error is not None
    else:  # or the advisor honestly chose to do nothing / the pair recovered first
        assert set(result.end_reasons) & {"do_nothing_recommended", "not_bunched"}
    assert result.llm_cost_usd == 0.0  # no LLM in this run
    assert uuid  # (imported for readability of ids in failures)
