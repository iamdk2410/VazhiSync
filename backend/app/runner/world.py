"""The live world: the ground-truth simulator, seen through a GPS layer, streamed to the app.

Each tick:
1. advance the world simulator by (real seconds x speed), in whole simulated seconds;
2. report what GPS would: a fix per bus, except buses in a GPS dropout (their last fix just
   gets older); write fixes to `vehicle_positions` and, every few seconds, queues to
   `stop_queues`;
3. detect bunching from the *reported* fixes only (rule 1) and open/resolve events;
4. publish one LiveFrame to Redis (the API filters it per role).

Staleness is computed here, server-side, from the fix age in real time (rule 4).
"""

import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog
from geoalchemy2 import WKTElement
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import GpsDropout, Settings
from app.db.enums import BunchingEventStatus, BunchingSeverity
from app.db.models import BunchingEvent, StopQueue, Vehicle, VehiclePosition
from app.live.channels import (
    BUNCHING_CHANNEL,
    LAST_FRAME_KEY,
    LAST_FRAME_TTL_SECONDS,
    LIVE_CHANNEL,
    SCENARIO_KEY,
    SPEED_KEY,
)
from app.live.headways import Fix, Headway, compute_headways
from app.live.schemas import (
    BunchingRaised,
    LiveEvent,
    LiveFrame,
    LiveHeadway,
    LiveQueue,
    LiveVehicle,
    WorldScenario,
    crowding,
)
from app.runner.bunching import BunchingTracker
from app.runner.route_loader import WorldRoute
from scenario_gen.checks import ScenarioContext, to_sim_config
from scenario_gen.checks import dropouts as scenario_dropouts
from simulator import SimConfig, Simulation

log = structlog.get_logger()

Speed = Literal[1, 5, 10]
SPEEDS: tuple[Speed, ...] = (1, 5, 10)
PRUNE_EVERY = timedelta(minutes=1)
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_speed(raw: bytes | str | None) -> Speed | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    for speed in SPEEDS:
        if value == speed:
            return speed
    return None


def clock_time(sim_time_s: float) -> str:
    s = int(sim_time_s) % 86_400
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


@dataclass(frozen=True)
class GpsFix:
    recorded_at: datetime
    direction_id: int
    distance_m: float
    lon: float
    lat: float
    heading_deg: float
    speed_mps: float
    load: int
    capacity: int


class WorldRunner:
    def __init__(
        self,
        route: WorldRoute,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        seed: int,
        speed: Speed,
        stale_after_s: float,
        bunching_ratio: float,
        closing_ratio: float,
        queue_every_s: float,
        retention: timedelta,
        dropouts: Sequence[GpsDropout] = (),
        clock: Clock = utc_now,
        sim_config: SimConfig | None = None,
        enqueue: Callable[[uuid.UUID], Awaitable[None]] | None = None,
    ) -> None:
        self._enqueue = enqueue  # starts an advisor workflow run for a new bunching event
        self.route = route
        self._sessionmaker = sessionmaker
        self._redis = redis
        config = sim_config or SimConfig(bunching_ratio=bunching_ratio, closing_ratio=closing_ratio)
        self.sim = Simulation.new(route.model, config, seed)
        # The normal day, restored when an officer clears a scenario.
        self._normal = (config, seed, tuple(dropouts))
        self._scenario_token: str | None = None
        self.scenario_name: str | None = None
        self.speed: Speed = speed
        self._stale_after_s = stale_after_s
        self._bunching_ratio = bunching_ratio
        self._closing_ratio = closing_ratio
        self._queue_every = timedelta(seconds=queue_every_s)
        self._retention = retention
        self._dropouts = tuple(dropouts)
        self._clock = clock
        self._pending_s = 0.0
        self._last_queue_write: datetime | None = None
        self._last_prune: datetime | None = None
        self._prev_distance: dict[str, tuple[int, float]] = {}
        self.vehicle_ids: dict[str, uuid.UUID] = {}  # simulator bus id -> vehicles.id
        self.fleet_numbers: dict[uuid.UUID, str] = {}
        self.fixes: dict[str, GpsFix] = {}  # last reported fix per bus id
        self.tracker = BunchingTracker(closing_ratio=closing_ratio)
        self.events: dict[uuid.UUID, LiveEvent] = {}

    @classmethod
    def from_settings(
        cls,
        route: WorldRoute,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        clock: Clock = utc_now,
        enqueue: Callable[[uuid.UUID], Awaitable[None]] | None = None,
    ) -> "WorldRunner":
        return cls(
            route,
            sessionmaker,
            redis,
            seed=settings.world_seed,
            speed=settings.world_speed,
            stale_after_s=settings.stale_gps_seconds,
            bunching_ratio=settings.bunching_ratio,
            closing_ratio=settings.closing_ratio,
            queue_every_s=settings.world_queue_every_seconds,
            retention=timedelta(hours=settings.world_retention_hours),
            dropouts=settings.world_gps_dropouts,
            clock=clock,
            enqueue=enqueue,
        )

    # ------------------------------------------------------------------ setup

    async def setup(self) -> None:
        """Register the simulated buses as vehicles and pick up events still open."""
        route_id = self.route.model.route_id
        bus_ids = [b.bus_id for b in self.sim.state.buses]
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                insert(Vehicle)
                .values(
                    [
                        {
                            "fleet_number": b,
                            "route_id": route_id,
                            "capacity": self.sim.config.capacity,
                        }
                        for b in bus_ids
                    ]
                )
                .on_conflict_do_nothing(index_elements=[Vehicle.fleet_number])
            )
            rows = await session.execute(
                select(Vehicle.fleet_number, Vehicle.id).where(Vehicle.fleet_number.in_(bus_ids))
            )
            self.vehicle_ids = {fleet: vid for fleet, vid in rows.all()}
            self.fleet_numbers = {vid: fleet for fleet, vid in self.vehicle_ids.items()}
            open_rows = await session.scalars(
                select(BunchingEvent).where(
                    BunchingEvent.route_id == route_id,
                    BunchingEvent.status == BunchingEventStatus.OPEN,
                )
            )
            for event in open_rows:
                pair = (str(event.leader_vehicle_id), str(event.follower_vehicle_id))
                self.tracker.opened(pair, event.id)
                self.events[event.id] = _live_event(event)
        log.info(
            "world_ready",
            route_id=route_id,
            buses=len(bus_ids),
            seed=self.sim.seed,
            open_events=len(self.events),
        )

    # ------------------------------------------------------------------ tick

    async def tick(self, real_dt_s: float) -> LiveFrame:
        await self._refresh_scenario()
        await self._refresh_speed()
        self._pending_s += real_dt_s * self.speed
        whole = math.floor(self._pending_s)
        self._pending_s -= whole
        self.sim.step(whole)  # whole simulated seconds: same seed -> same world (rule 11)
        now = self._clock()
        new_fixes = self._report_gps(now, whole)
        headways = compute_headways(
            [
                Fix(str(self.vehicle_ids[bus]), f.direction_id, f.distance_m, self._age(f, now))
                for bus, f in self.fixes.items()
            ],
            self.route.directions,
            self._stale_after_s,
            self._bunching_ratio,
            self._closing_ratio,
        )
        decisions = self.tracker.update(headways)
        async with self._sessionmaker() as session, session.begin():
            session.add_all(self._position_rows(new_fixes))
            if self._last_queue_write is None or now - self._last_queue_write >= self._queue_every:
                session.add_all(self._queue_rows(now))
                self._last_queue_write = now
            raised = [await self._open_event(session, h, now) for h in decisions.open]
            if decisions.resolve:
                await session.execute(
                    update(BunchingEvent)
                    .where(
                        BunchingEvent.id.in_(decisions.resolve),
                        BunchingEvent.status == BunchingEventStatus.OPEN,
                    )
                    .values(status=BunchingEventStatus.RESOLVED, resolved_at=now)
                )
            if self._last_prune is None or now - self._last_prune >= PRUNE_EVERY:
                await self._prune(session, now)
                self._last_prune = now
        for event_id in decisions.resolve:
            self.events.pop(event_id, None)
            log.info("bunching_resolved", event_id=str(event_id))
        frame = self._frame(now, headways)
        await self._publish(frame, raised)
        for event in raised:
            await self._start_workflow(event.event_id)
        return frame

    def _age(self, fix: GpsFix, now: datetime) -> float:
        return max((now - fix.recorded_at).total_seconds(), 0.0)

    def _in_dropout(self, bus_id: str) -> bool:
        t = self.sim.state.elapsed_s
        return any(
            d.bus_id == bus_id and d.start_s <= t < d.start_s + d.duration_s for d in self._dropouts
        )

    def _report_gps(self, now: datetime, dt_s: float) -> dict[str, GpsFix]:
        reported: dict[str, GpsFix] = {}
        for bus in self.sim.snapshot().buses:
            prev = self._prev_distance.get(bus.bus_id)
            moved = bus.distance_m - prev[1] if prev and prev[0] == bus.direction else 0.0
            self._prev_distance[bus.bus_id] = (bus.direction, bus.distance_m)
            if self._in_dropout(bus.bus_id):
                continue
            where = self.route.lines[bus.direction].locate(bus.distance_m)
            reported[bus.bus_id] = GpsFix(
                recorded_at=now,
                direction_id=bus.direction,
                distance_m=bus.distance_m,
                lon=round(where.lon, 6),
                lat=round(where.lat, 6),
                heading_deg=round(where.heading_deg, 1),
                speed_mps=round(max(moved, 0.0) / dt_s, 2) if dt_s > 0 else 0.0,
                load=bus.load,
                capacity=bus.capacity,
            )
        self.fixes.update(reported)
        return reported

    def _position_rows(self, fixes: dict[str, GpsFix]) -> list[VehiclePosition]:
        return [
            VehiclePosition(
                vehicle_id=self.vehicle_ids[bus],
                recorded_at=f.recorded_at,
                geom=WKTElement(f"POINT({f.lon} {f.lat})", srid=4326),
                distance_along_m=f.distance_m,
                sim_time_s=self.sim.state.time_s,
                direction_id=f.direction_id,
                speed_mps=f.speed_mps,
                heading_deg=f.heading_deg,
                load_count=f.load,
            )
            for bus, f in fixes.items()
        ]

    def _queues(self) -> list[LiveQueue]:
        queues = self.sim.state.queues
        return [
            LiveQueue(direction_id=d, stop_id=stop_id, waiting=queues[d][i])
            for d in (0, 1)
            for i, stop_id in enumerate(self.route.stop_ids[d])
        ]

    def _queue_rows(self, now: datetime) -> list[StopQueue]:
        route_id = self.route.model.route_id
        return [
            StopQueue(
                route_id=route_id,
                stop_id=q.stop_id,
                recorded_at=now,
                sim_time_s=self.sim.state.time_s,
                direction_id=q.direction_id,
                waiting_count=q.waiting,
            )
            for q in self._queues()
        ]

    async def _open_event(self, session: AsyncSession, h: Headway, now: datetime) -> BunchingRaised:
        event = BunchingEvent(
            route_id=self.route.model.route_id,
            leader_vehicle_id=uuid.UUID(h.leader_id),
            follower_vehicle_id=uuid.UUID(h.follower_id),
            detected_at=now,
            headway_seconds=h.headway_s,
            scheduled_headway_seconds=h.scheduled_headway_s,
            severity=BunchingSeverity.BUNCHED,
            status=BunchingEventStatus.OPEN,
        )
        session.add(event)
        await session.flush()
        self.tracker.opened((h.leader_id, h.follower_id), event.id)
        self.events[event.id] = _live_event(event)
        log.info(
            "bunching_detected",
            event_id=str(event.id),
            leader=self.fleet_numbers.get(event.leader_vehicle_id),
            follower=self.fleet_numbers.get(event.follower_vehicle_id),
            headway_s=h.headway_s,
            scheduled_s=h.scheduled_headway_s,
        )
        return BunchingRaised(
            event_id=event.id,
            route_id=event.route_id,
            leader_vehicle_id=event.leader_vehicle_id,
            follower_vehicle_id=event.follower_vehicle_id,
            headway_seconds=h.headway_s,
            scheduled_headway_seconds=h.scheduled_headway_s,
            detected_at=now,
        )

    async def _prune(self, session: AsyncSession, now: datetime) -> None:
        cutoff = now - self._retention
        await session.execute(delete(VehiclePosition).where(VehiclePosition.recorded_at < cutoff))
        await session.execute(delete(StopQueue).where(StopQueue.recorded_at < cutoff))

    def _frame(self, now: datetime, headways: list[Headway]) -> LiveFrame:
        vehicles = []
        for bus, f in sorted(self.fixes.items()):
            age = self._age(f, now)
            vid = self.vehicle_ids[bus]
            vehicles.append(
                LiveVehicle(
                    vehicle_id=vid,
                    fleet_number=bus,
                    direction_id=f.direction_id,
                    lon=f.lon,
                    lat=f.lat,
                    heading_deg=f.heading_deg,
                    distance_m=f.distance_m,
                    speed_mps=f.speed_mps,
                    load=f.load,
                    capacity=f.capacity,
                    crowding=crowding(f.load, f.capacity),
                    last_fix_at=f.recorded_at,
                    age_seconds=round(age, 1),
                    is_stale=age > self._stale_after_s,
                )
            )
        return LiveFrame(
            route_id=self.route.model.route_id,
            sim_time=clock_time(self.sim.state.time_s),
            sim_elapsed_s=self.sim.state.elapsed_s,
            speed=self.speed,
            scenario=self.scenario_name,
            generated_at=now,
            vehicles=vehicles,
            queues=self._queues(),
            headways=[
                LiveHeadway(
                    direction_id=h.direction_id,
                    leader_vehicle_id=uuid.UUID(h.leader_id),
                    follower_vehicle_id=uuid.UUID(h.follower_id),
                    gap_m=h.gap_m,
                    headway_seconds=h.headway_s,
                    scheduled_headway_seconds=h.scheduled_headway_s,
                    ratio=h.ratio,
                    status=h.status,
                )
                for h in headways
            ],
            events=sorted(self.events.values(), key=lambda e: e.detected_at),
        )

    # ------------------------------------------------------------------ scenarios

    async def _refresh_scenario(self) -> None:
        """Restart the world when an officer loads (or clears) a scenario."""
        try:
            raw = await self._redis.get(SCENARIO_KEY)
        except RedisError as exc:
            log.warning("scenario_unavailable", error=repr(exc))
            return
        if raw is None:
            return
        try:
            wanted = WorldScenario.model_validate_json(raw)
        except ValidationError as exc:
            log.warning("scenario_invalid", error=str(exc)[:300])
            return
        if wanted.token != self._scenario_token:
            await self.reset(wanted)

    async def reset(self, wanted: WorldScenario) -> None:
        """Start the world again from the scenario's start (or a normal day).

        Open bunching events describe a world that no longer exists: they are dismissed.
        """
        config, seed, dropouts_ = self._normal
        name: str | None = None
        if wanted.plan is not None:
            ctx = ScenarioContext(self.route.model, fleet_size=len(self.sim.state.buses))
            config = to_sim_config(wanted.plan, ctx, self._normal[0])
            seed = wanted.seed if wanted.seed is not None else seed
            dropouts_ = tuple(
                GpsDropout(bus_id=d.bus_id, start_s=d.start_s, duration_s=d.duration_s)
                for d in scenario_dropouts(wanted.plan, ctx)
            )
            name = wanted.name or wanted.plan.name
        self.sim = Simulation.new(self.route.model, config, seed)
        self._dropouts = dropouts_
        self._pending_s = 0.0
        self._prev_distance.clear()
        self.fixes.clear()
        self._last_queue_write = None
        dismissed = list(self.events)
        self.events.clear()
        self.tracker.open_events.clear()
        self._scenario_token = wanted.token
        self.scenario_name = name
        now = self._clock()
        if dismissed:
            async with self._sessionmaker() as session, session.begin():
                await session.execute(
                    update(BunchingEvent)
                    .where(
                        BunchingEvent.id.in_(dismissed),
                        BunchingEvent.status == BunchingEventStatus.OPEN,
                    )
                    .values(status=BunchingEventStatus.DISMISSED, resolved_at=now)
                )
        log.info(
            "world_reset",
            scenario=name or "normal day",
            scenario_id=str(wanted.scenario_id) if wanted.scenario_id else None,
            seed=seed,
            start=clock_time(config.start_time_s),
            dismissed_events=len(dismissed),
        )

    # ------------------------------------------------------------------ redis

    async def _refresh_speed(self) -> None:
        try:
            speed = parse_speed(await self._redis.get(SPEED_KEY))
        except RedisError as exc:
            log.warning("speed_unavailable", error=repr(exc))
            return
        if speed is not None and speed != self.speed:
            log.info("world_speed_changed", speed=speed)
            self.speed = speed

    async def _start_workflow(self, event_id: uuid.UUID) -> None:
        """Best effort: if the queue is down, the worker's minute sweep picks the event up."""
        if self._enqueue is None:
            return
        try:
            await self._enqueue(event_id)
        except (RedisError, OSError) as exc:
            log.warning("workflow_enqueue_failed", event_id=str(event_id), error=repr(exc))

    async def _publish(self, frame: LiveFrame, raised: list[BunchingRaised]) -> None:
        """Best effort: the database already has the data, so a Redis outage only pauses the
        live view (logged), it never stops the world."""
        payload = frame.model_dump_json()
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.set(LAST_FRAME_KEY, payload, ex=LAST_FRAME_TTL_SECONDS)
                pipe.publish(LIVE_CHANNEL, payload)
                for event in raised:
                    pipe.publish(BUNCHING_CHANNEL, event.model_dump_json())
                await pipe.execute()
        except RedisError as exc:
            log.warning("live_publish_failed", error=repr(exc))


def _live_event(event: BunchingEvent) -> LiveEvent:
    return LiveEvent(
        event_id=event.id,
        leader_vehicle_id=event.leader_vehicle_id,
        follower_vehicle_id=event.follower_vehicle_id,
        severity=event.severity,
        detected_at=event.detected_at,
        headway_seconds=event.headway_seconds,
    )
