"""What the API does to move a paused run along (B12): queue resumes, tell the world.

The API never runs the graph itself; it records the human's decision and queues a
`resume_workflow` job with a deterministic job id, so a double-click queues one job. The
database claim in `resume_workflow` makes a duplicate delivery harmless anyway.
"""

import json
import uuid
from collections.abc import Awaitable
from typing import Any, cast

from arq import ArqRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, DriverInstruction, Recommendation, Vehicle
from app.live.channels import LAST_FRAME_KEY, WORLD_ACTIONS_KEY
from app.live.schemas import LiveFrame


async def queue_resume(
    queue: ArqRedis, run_id: uuid.UUID, kind: str, value: dict[str, Any], dedupe: str
) -> None:
    await queue.enqueue_job(
        "resume_workflow", str(run_id), kind, value, _job_id=f"resume:{run_id}:{dedupe}"
    )


async def world_time_s(redis: Redis) -> float | None:
    """The world's clock (seconds since midnight) from the runner's latest frame."""
    try:
        raw = await redis.get(LAST_FRAME_KEY)
    except RedisError:
        return None
    if raw is None:
        return None
    h, m, s = (int(x) for x in LiveFrame.model_validate_json(raw).sim_time.split(":"))
    return float(h * 3600 + m * 60 + s)


async def apply_in_world(
    redis: Redis, session: AsyncSession, instruction: DriverInstruction
) -> None:
    """The driver acknowledged: the world simulator carries the action out on its next tick."""
    vehicle = await session.get(Vehicle, instruction.vehicle_id)
    if vehicle is None:
        return
    await cast(
        Awaitable[int],
        redis.rpush(
            WORLD_ACTIONS_KEY,
            json.dumps(
                {
                    "instruction_id": instruction.instruction_id,
                    "fleet_number": vehicle.fleet_number,
                    "action_type": instruction.action_type.value,
                    "params": instruction.params,
                }
            ),
        ),
    )


async def run_of_instruction(
    session: AsyncSession, instruction: DriverInstruction
) -> uuid.UUID | None:
    run_id: uuid.UUID | None = await session.scalar(
        select(Recommendation.run_id)
        .join(Approval, Approval.recommendation_id == Recommendation.id)
        .where(Approval.id == instruction.approval_id)
    )
    return run_id
