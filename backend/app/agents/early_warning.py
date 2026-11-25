"""Early warning (B21): act before the bunch forms.

Every few seconds the sweep reads what the advisor may see (avl-mcp positions and headways,
demand-mcp estimates; through the harness, rule 12), builds the what-if model with
`from_observations` (rule 1) and lets it run "do nothing" for the horizon over a few seeds,
checking the pair headways with the live detector's own `compute_headways`. A pair that is
fine now but bunches in at least `min_agree` seeds raises a *predicted* bunching event, which
starts the same workflow: the same one-run-per-pair claim, rulebook and cooldowns apply, and
the officer still approves. A fix applied early (a short hold) is smaller than the one needed
once the buses have bunched.
"""

import asyncio
import math
import statistics
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.headway_monitor import world_time
from app.agents.impact_simulator import observations_from
from app.config import Settings
from app.db.enums import BunchingEventKind, BunchingEventStatus, BunchingSeverity
from app.db.models import BunchingEvent, Recommendation, WorkflowRun
from app.graph.workflow import ACTIVE
from app.harness.budget import RunBudget
from app.harness.harness import ToolHarness
from app.live.headways import DirectionInfo, Fix, compute_headways
from app.runner.route_loader import WorldRoute
from mcp_servers.schemas import BoardingEstimates, HeadwayPair, Headways, VehiclePositions
from simulator import ObservationError, Observations, RouteModel, SimConfig, from_observations

log = structlog.get_logger()

AGENT = "early_warning"
SWEEP_TIME_LIMIT_S = 25.0
Pair = tuple[str, str]  # (leader fleet number, follower fleet number)
Enqueue = Callable[[uuid.UUID], Awaitable[None]]


@dataclass(frozen=True)
class Prediction:
    direction_id: int
    leader: str  # fleet numbers (the what-if model's bus ids)
    follower: str
    in_s: float  # median lead time over the seeds that bunched the pair
    seeds_agreeing: int
    seeds: int


def first_bunching(
    route: RouteModel,
    directions: dict[int, DirectionInfo],
    observations: Observations,
    config: SimConfig,
    watch: set[Pair],
    seed: int,
    horizon_s: float,
    step_s: float,
) -> dict[Pair, float]:
    """For one seed: when each watched pair first reads 'bunched' while doing nothing."""
    sim = from_observations(route, observations, config, seed)
    found: dict[Pair, float] = {}
    t = 0.0
    while t < horizon_s and len(found) < len(watch):
        dt = min(step_s, horizon_s - t)
        sim.step(dt)
        t += dt
        fixes = [Fix(b.bus_id, b.direction, b.distance_m, 0.0) for b in sim.snapshot().buses]
        for h in compute_headways(
            fixes, directions, math.inf, config.bunching_ratio, config.closing_ratio
        ):
            pair = (h.leader_id, h.follower_id)
            if h.status == "bunched" and pair in watch and pair not in found:
                found[pair] = t
    return found


def predict_bunching(
    route: RouteModel,
    directions: dict[int, DirectionInfo],
    observations: Observations,
    config: SimConfig,
    current: list[HeadwayPair],
    *,
    seeds: tuple[int, ...],
    horizon_s: float,
    step_s: float,
    min_agree: int,
) -> list[Prediction]:
    """Pairs that are fine now (ok / closing, fresh) but bunch within the horizon in at least
    `min_agree` seeds. CPU-bound and pure: run it in a thread."""
    watch = {
        (p.leader_fleet_number, p.follower_fleet_number): p
        for p in current
        if p.status in ("ok", "closing") and p.data_quality == "ok"
    }
    if not watch:
        return []
    per_seed = [
        first_bunching(route, directions, observations, config, set(watch), seed, horizon_s, step_s)
        for seed in seeds
    ]
    out: list[Prediction] = []
    for pair, now in watch.items():
        times = [found[pair] for found in per_seed if pair in found]
        if len(times) >= min_agree:
            out.append(
                Prediction(
                    direction_id=now.direction_id,
                    leader=pair[0],
                    follower=pair[1],
                    in_s=round(statistics.median(times), 1),
                    seeds_agreeing=len(times),
                    seeds=len(seeds),
                )
            )
    return sorted(out, key=lambda p: p.in_s)


async def _blocked(
    session: AsyncSession, leader: uuid.UUID, follower: uuid.UUID, cooldown: timedelta
) -> str | None:
    """Same dedupe as actual events, plus a per-pair cool-down between predictions."""
    same_pair = (
        BunchingEvent.leader_vehicle_id == leader,
        BunchingEvent.follower_vehicle_id == follower,
    )
    open_event = await session.scalar(
        select(BunchingEvent.id).where(*same_pair, BunchingEvent.status == BunchingEventStatus.OPEN)
    )
    if open_event is not None:
        return "event_open"
    # A run still in progress for the pair (the same test as the workflow's claim). An event
    # merely left 'handling' by a finished run doesn't count.
    # Like the claim, a run still waiting for an officer whose recommendation has expired is
    # stale (the next event supersedes it), so it doesn't hold the pair either.
    latest_expiry = (
        select(func.max(Recommendation.expires_at))
        .where(Recommendation.run_id == WorkflowRun.id)
        .scalar_subquery()
    )
    running = await session.scalar(
        select(WorkflowRun.id)
        .join(BunchingEvent, BunchingEvent.id == WorkflowRun.bunching_event_id)
        .where(
            *same_pair,
            WorkflowRun.status.in_(ACTIVE),
            WorkflowRun.finished_at.is_(None),
            or_(
                WorkflowRun.waiting_for.is_distinct_from("officer"),
                latest_expiry > datetime.now(UTC),
            ),
        )
    )
    if running is not None:
        return "run_active"
    recent = await session.scalar(
        select(BunchingEvent.id).where(
            *same_pair,
            BunchingEvent.kind == BunchingEventKind.PREDICTED,
            BunchingEvent.detected_at > datetime.now(UTC) - cooldown,
        )
    )
    return "recently_predicted" if recent is not None else None


async def early_warning_sweep(
    harness: ToolHarness,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    route: WorldRoute,
    enqueue: Enqueue,
) -> list[uuid.UUID]:
    """One look ahead; returns the predicted events it raised (each starts a workflow)."""
    sweep_id = uuid.uuid4()
    budget = RunBudget(sweep_id, SWEEP_TIME_LIMIT_S, max_tool_calls_per_agent=3, max_steps=1)
    route_id = route.model.route_id
    args = {"route_id": route_id}
    with structlog.contextvars.bound_contextvars(run_id=str(sweep_id)):
        positions = await harness.call(budget, AGENT, "get_vehicle_positions", args)
        headways = await harness.call(budget, AGENT, "get_headways", args)
        estimates = await harness.call_optional(
            budget,
            AGENT,
            "get_boarding_estimates",
            {"route_id": route_id, "horizon_minutes": int(settings.early_warning_horizon_minutes)},
        )
        if not isinstance(positions, VehiclePositions) or not isinstance(headways, Headways):
            return []
        demand = estimates.value if isinstance(estimates.value, BoardingEstimates) else None
        observations = observations_from(positions, demand, world_time(positions), route.model)
        config = SimConfig(
            bunching_ratio=headways.bunching_ratio, closing_ratio=headways.closing_ratio
        )
        try:
            predictions = await asyncio.to_thread(
                predict_bunching,
                route.model,
                route.directions,
                observations,
                config,
                headways.pairs,
                seeds=tuple(range(1, settings.early_warning_seeds + 1)),
                horizon_s=settings.early_warning_horizon_minutes * 60,
                step_s=settings.early_warning_step_seconds,
                min_agree=min(settings.early_warning_min_agree, settings.early_warning_seeds),
            )
        except ObservationError as exc:
            log.warning("early_warning_skipped", reason=str(exc))
            return []
        if not predictions:
            return []
        pairs = {(p.leader_fleet_number, p.follower_fleet_number): p for p in headways.pairs}
        cooldown = timedelta(minutes=settings.early_warning_cooldown_minutes)
        raised: list[uuid.UUID] = []
        async with sessionmaker() as session:
            for prediction in predictions:
                now = pairs[(prediction.leader, prediction.follower)]
                blocked = await _blocked(
                    session, now.leader_vehicle_id, now.follower_vehicle_id, cooldown
                )
                if blocked is not None:
                    log.info(
                        "bunching_prediction_skipped",
                        leader=prediction.leader,
                        follower=prediction.follower,
                        reason=blocked,
                    )
                    continue
                event = BunchingEvent(
                    route_id=route_id,
                    leader_vehicle_id=now.leader_vehicle_id,
                    follower_vehicle_id=now.follower_vehicle_id,
                    headway_seconds=now.headway_seconds,
                    scheduled_headway_seconds=now.scheduled_headway_seconds,
                    severity=BunchingSeverity.CLOSING,
                    status=BunchingEventStatus.OPEN,
                    kind=BunchingEventKind.PREDICTED,
                    predicted_in_seconds=prediction.in_s,
                )
                session.add(event)
                await session.commit()
                raised.append(event.id)
                log.info(
                    "bunching_predicted",
                    event_id=str(event.id),
                    leader=prediction.leader,
                    follower=prediction.follower,
                    in_s=prediction.in_s,
                    seeds=f"{prediction.seeds_agreeing}/{prediction.seeds}",
                    ratio_now=now.ratio,
                )
        for event_id in raised:
            await enqueue(event_id)
        return raised
