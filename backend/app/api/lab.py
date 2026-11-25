"""Simulation Lab (officer only): generate scenarios, browse the library, load one into the
live world.

Generation runs on the ARQ worker (the local LLM can take a minute), so `POST /lab/scenarios`
answers 202 at once and the client polls `GET /lab/scenarios/{id}`.
"""

import secrets
import uuid
from typing import Annotated

import structlog
from arq import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_job_queue, get_redis
from app.api.schemas import (
    ApplyScenarioRequest,
    ScenarioCreate,
    ScenarioList,
    ScenarioOut,
    WorldScenarioOut,
)
from app.auth.deps import require_role
from app.config import get_settings
from app.db.enums import Role, ScenarioSource, ScenarioStatus
from app.db.models import ScenarioRecord, User
from app.db.session import get_session
from app.harness.faults import FaultPlan
from app.live.channels import FAULTS_KEY, SCENARIO_KEY
from app.live.schemas import WorldScenario
from scenario_gen.schema import ScenarioPlan

log = structlog.get_logger()

router = APIRouter(prefix="/lab", tags=["lab"])

Officer = Annotated[User, Depends(require_role(Role.OFFICER))]
Session = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]

MAX_ACTIVE_GENERATIONS = 3  # the local LLM handles one request at a time
LIST_LIMIT = 50


def _unavailable(what: str) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{what} unavailable")


@router.post("/scenarios", response_model=ScenarioOut, status_code=status.HTTP_202_ACCEPTED)
async def create_scenario(
    body: ScenarioCreate,
    officer: Officer,
    session: Session,
    queue: Annotated[ArqRedis, Depends(get_job_queue)],
) -> ScenarioOut:
    """Queue a scenario generation from a plain-English description."""
    active = await session.scalar(
        select(func.count())
        .select_from(ScenarioRecord)
        .where(ScenarioRecord.status.in_([ScenarioStatus.PENDING, ScenarioStatus.GENERATING]))
    )
    if (active or 0) >= MAX_ACTIVE_GENERATIONS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Scenario generations already in progress; try again shortly.",
        )
    record = ScenarioRecord(
        route_id=get_settings().world_route_id,
        source=ScenarioSource.GENERATED,
        description=body.description,
        status=ScenarioStatus.PENDING,
        seed=secrets.randbelow(2**31),
        created_by=officer.id,
    )
    session.add(record)
    await session.commit()
    try:
        await queue.enqueue_job(
            "generate_scenario", str(record.id), _job_id=f"scenario:{record.id}"
        )
    except (RedisError, OSError) as exc:
        record.status = ScenarioStatus.FAILED
        record.error = "job queue unavailable"
        await session.commit()
        log.warning("scenario_enqueue_failed", scenario_id=str(record.id), error=repr(exc))
        raise _unavailable("Job queue") from None
    log.info("scenario_requested", scenario_id=str(record.id), officer_id=str(officer.id))
    return ScenarioOut.model_validate(record)


@router.get("/scenarios", response_model=ScenarioList)
async def list_scenarios(_: Officer, session: Session) -> ScenarioList:
    """Golden scenarios first, then the newest generated ones."""
    rows = await session.scalars(
        select(ScenarioRecord)
        .order_by(
            (ScenarioRecord.source == ScenarioSource.GOLDEN).desc(),
            ScenarioRecord.created_at.desc(),
        )
        .limit(LIST_LIMIT)
    )
    return ScenarioList(scenarios=[ScenarioOut.model_validate(r) for r in rows])


async def _get(session: AsyncSession, scenario_id: uuid.UUID) -> ScenarioRecord:
    record = await session.get(ScenarioRecord, scenario_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Scenario not found")
    return record


@router.get("/scenarios/{scenario_id}", response_model=ScenarioOut)
async def get_scenario(scenario_id: uuid.UUID, _: Officer, session: Session) -> ScenarioOut:
    return ScenarioOut.model_validate(await _get(session, scenario_id))


async def _set_world(redis: Redis, wanted: WorldScenario) -> None:
    try:
        await redis.set(SCENARIO_KEY, wanted.model_dump_json())
    except RedisError as exc:
        log.warning("world_scenario_unavailable", error=repr(exc))
        raise _unavailable("Live world") from None


async def _set_faults(redis: Redis, faults: FaultPlan | None) -> None:
    try:
        if faults is None or not faults.active:
            await redis.delete(FAULTS_KEY)
        else:
            await redis.set(FAULTS_KEY, faults.model_dump_json())
    except RedisError as exc:
        log.warning("faults_unavailable", error=repr(exc))
        raise _unavailable("Fault injection") from None


@router.post(
    "/scenarios/{scenario_id}/apply",
    response_model=WorldScenarioOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_scenario(
    scenario_id: uuid.UUID,
    officer: Officer,
    session: Session,
    redis: RedisDep,
    body: ApplyScenarioRequest | None = None,
) -> WorldScenarioOut:
    """Restart the live world with this scenario (the runner picks it up on its next tick),
    optionally with injected failures (B17); applying without faults clears any."""
    record = await _get(session, scenario_id)
    if record.status != ScenarioStatus.READY or record.plan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Scenario is {record.status}")
    plan = ScenarioPlan.model_validate(record.plan)  # rule 6: validated again at the boundary
    await _set_world(
        redis,
        WorldScenario(
            token=uuid.uuid4().hex,
            scenario_id=record.id,
            name=plan.name,
            seed=record.seed,
            plan=plan,
        ),
    )
    faults = body.faults if body else None
    await _set_faults(redis, faults)
    log.info(
        "world_scenario_applied",
        scenario_id=str(record.id),
        officer_id=str(officer.id),
        faults=faults.describe() if faults else [],
    )
    return WorldScenarioOut(
        scenario_id=record.id, name=plan.name, faults=faults.describe() if faults else []
    )


@router.delete("/world/scenario", response_model=WorldScenarioOut)
async def clear_world_scenario(officer: Officer, redis: RedisDep) -> WorldScenarioOut:
    """Back to a normal day (the world restarts)."""
    await _set_world(redis, WorldScenario(token=uuid.uuid4().hex))
    await _set_faults(redis, None)
    log.info("world_scenario_cleared", officer_id=str(officer.id))
    return WorldScenarioOut(scenario_id=None, name=None)


@router.get("/world/faults", response_model=FaultPlan)
async def current_faults(_: Officer, redis: RedisDep) -> FaultPlan:
    try:
        raw = await redis.get(FAULTS_KEY)
    except RedisError:
        raise _unavailable("Fault injection") from None
    return FaultPlan.model_validate_json(raw) if raw else FaultPlan()


@router.put("/world/faults", response_model=FaultPlan)
async def set_faults(body: FaultPlan, officer: Officer, redis: RedisDep) -> FaultPlan:
    """Toggle injected failures on the running world without restarting it (B17). An empty
    plan switches them off. Takes effect within a couple of seconds."""
    await _set_faults(redis, body)
    log.info("faults_set", officer_id=str(officer.id), faults=body.describe())
    return body


@router.get("/world/scenario", response_model=WorldScenarioOut)
async def current_world_scenario(_: Officer, redis: RedisDep) -> WorldScenarioOut:
    try:
        raw = await redis.get(SCENARIO_KEY)
    except RedisError:
        raise _unavailable("Live world") from None
    try:
        raw_faults = await redis.get(FAULTS_KEY)
    except RedisError:
        raise _unavailable("Live world") from None
    faults = FaultPlan.model_validate_json(raw_faults).describe() if raw_faults else []
    if raw is None:
        return WorldScenarioOut(scenario_id=None, name=None, faults=faults)
    wanted = WorldScenario.model_validate_json(raw)
    return WorldScenarioOut(scenario_id=wanted.scenario_id, name=wanted.name, faults=faults)
