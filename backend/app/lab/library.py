"""Scenario library helpers shared by the API, the worker and the world runner."""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ScenarioSource, ScenarioStatus
from app.db.models import ScenarioRecord
from app.runner.route_loader import load_world_route
from scenario_gen.checks import ScenarioContext
from scenario_gen.golden import GOLDEN_DIR, load_golden
from scenario_gen.preview import preview

log = structlog.get_logger()


async def scenario_context(session: AsyncSession, route_id: str) -> ScenarioContext:
    """The imported route as the simulator sees it (raises RouteNotImported)."""
    return ScenarioContext.for_route((await load_world_route(session, route_id)).model)


async def sync_golden(session: AsyncSession, route_id: str, directory: Path = GOLDEN_DIR) -> int:
    """Insert or refresh the committed golden scenarios for `route_id` (idempotent)."""
    golden = [g for g in load_golden(directory) if g.route_id == route_id]
    if not golden:
        return 0
    ctx = await scenario_context(session, route_id)
    now = datetime.now(UTC)
    for g in golden:
        values = {
            "route_id": g.route_id,
            "source": ScenarioSource.GOLDEN,
            "description": g.description,
            "status": ScenarioStatus.READY,
            "seed": g.seed,
            "plan": g.plan.model_dump(mode="json"),
            "preview": dataclasses.asdict(preview(g.scenario, ctx)),
            "attempts": 0,
            "completed_at": now,
        }
        await session.execute(
            insert(ScenarioRecord)
            .values(golden_key=g.key, **values)
            .on_conflict_do_update(index_elements=[ScenarioRecord.golden_key], set_=values)
        )
    await session.commit()
    log.info("golden_scenarios_synced", route_id=route_id, count=len(golden))
    return len(golden)
