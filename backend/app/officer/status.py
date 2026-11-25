"""Clear status messages for the officer (B17): what is degraded, and why.

Built from facts that are already stored, so any API process can answer without sharing the
worker's memory: the tools each run had to do without (`workflow_runs.degraded`), runs that
failed because a tool was down, recent LLM fallbacks (`llm_usage`) and the active fault plan.
"""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LlmUsage, WorkflowRun
from app.harness.faults import FaultPlan
from app.live.channels import FAULTS_KEY

RECENT = timedelta(minutes=10)
LLM_RECENT = timedelta(minutes=5)
RULES_LEVEL = 4  # app.llm.gateway.RULES

DEGRADED_MESSAGES = {
    "get_boarding_estimates": (
        "Running in degraded mode: demand data unavailable (skip-stop options and "
        "demand-aware estimates are left out)."
    ),
}
DOWN_MESSAGES = {
    "avl": (
        "Bus positions unavailable (avl-mcp down): bunching events can't be checked or acted on."
    ),
    "demand": "Demand data unavailable (demand-mcp down): running in degraded mode.",
}
TOOL_SERVER = {
    "get_vehicle_positions": "avl",
    "get_headways": "avl",
    "get_boarding_estimates": "demand",
    "send_driver_instruction": "demand",
}


def run_messages(degraded: list[str] | None) -> list[str]:
    """Messages for one run (the recommendation card, the run list, the run view)."""
    return [
        DEGRADED_MESSAGES.get(tool, f"Running in degraded mode: {tool} unavailable.")
        for tool in sorted(set(degraded or []))
    ]


class SystemStatus(BaseModel):
    healthy: bool
    messages: list[str]
    faults: list[str]  # injected failures (B17), described
    degraded_tools: list[str]  # tools recent runs did without
    down_servers: list[str]  # servers recent runs failed on
    llm: str  # "ok" | "partly_rules" | "rules_only" | "idle"


async def _faults(redis: Redis) -> FaultPlan:
    try:
        raw = await redis.get(FAULTS_KEY)
    except RedisError:
        return FaultPlan()
    return FaultPlan.model_validate_json(raw) if raw else FaultPlan()


async def system_status(session: AsyncSession, redis: Redis) -> SystemStatus:
    now = datetime.now(UTC)
    since = now - RECENT
    degraded: set[str] = set()
    for tools in await session.scalars(
        select(WorkflowRun.degraded).where(WorkflowRun.started_at > since)
    ):
        degraded |= set(tools or [])
    failed = await session.scalars(
        select(WorkflowRun.end_reason).where(
            WorkflowRun.started_at > since, WorkflowRun.end_reason.like("tools:unavailable:%")
        )
    )
    down = sorted({TOOL_SERVER.get(r.rsplit(":", 1)[-1], "?") for r in failed if r} - {"?"})
    rows = (
        await session.execute(
            select(LlmUsage.fallback_level, func.count())
            .where(LlmUsage.created_at > now - LLM_RECENT, LlmUsage.success.is_(True))
            .group_by(LlmUsage.fallback_level)
        )
    ).all()
    counts: dict[bool, int] = {}
    for level, n in rows:
        counts[level == RULES_LEVEL] = counts.get(level == RULES_LEVEL, 0) + n
    rules, models = counts.get(True, 0), counts.get(False, 0)
    llm = (
        "idle"
        if not (rules or models)
        else "rules_only"
        if not models
        else ("partly_rules" if rules else "ok")
    )
    faults = await _faults(redis)

    messages: list[str] = []
    if faults.active:
        messages.append("Failure injection is on: " + "; ".join(faults.describe()) + ".")
    messages += [DOWN_MESSAGES[s] for s in down if s in DOWN_MESSAGES]
    if "demand" in down:  # already said: the server is down
        degraded.discard("get_boarding_estimates")
    messages += run_messages(sorted(degraded))
    if llm == "rules_only":
        messages.append(
            "LLM providers unavailable or rate-limited: candidate ideas and explanations come "
            "from the rules only (the simulator still decides)."
        )
    elif llm == "partly_rules":
        messages.append("Some LLM calls fell back to the rules in the last few minutes.")
    return SystemStatus(
        healthy=not messages,
        messages=messages,
        faults=faults.describe(),
        degraded_tools=sorted(degraded),
        down_servers=down,
        llm=llm,
    )
