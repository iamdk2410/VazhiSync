"""One Redis subscription per API process, fanned out to every connected WebSocket.

Each connection has a tiny queue; a slow client drops old frames rather than slowing the
others (a live map only needs the newest frame).
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.db.enums import Role
from app.live.channels import LAST_FRAME_KEY, LIVE_CHANNEL
from app.live.schemas import LiveFrame

log = structlog.get_logger()

QUEUE_SIZE = 2
RECONNECT_MAX_SECONDS = 30


@dataclass(eq=False)
class Subscriber:
    role: Role
    vehicle_id: uuid.UUID | None
    queue: asyncio.Queue[LiveFrame] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))

    def offer(self, frame: LiveFrame) -> None:
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()  # drop the oldest
        self.queue.put_nowait(frame)


class LiveHub:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._subscribers: set[Subscriber] = set()
        self._task: asyncio.Task[None] | None = None
        self.last: LiveFrame | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def subscribe(self, role: Role, vehicle_id: uuid.UUID | None) -> Subscriber:
        sub = Subscriber(role, vehicle_id)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subscribers.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def dispatch(self, raw: bytes | str) -> None:
        """Validate a frame from Redis and hand it to every subscriber (rule 6)."""
        try:
            frame = LiveFrame.model_validate_json(raw)
        except ValidationError as exc:
            log.warning("live_frame_invalid", error=str(exc)[:500])
            return
        self.last = frame
        for sub in list(self._subscribers):
            sub.offer(frame)

    async def latest(self) -> LiveFrame | None:
        """The newest frame: in memory, else the copy the runner leaves in Redis."""
        if self.last is None:
            try:
                raw = await self._redis.get(LAST_FRAME_KEY)
            except RedisError as exc:
                log.warning("live_last_frame_unavailable", error=repr(exc))
                return None
            if raw is not None:
                self.dispatch(raw)
        return self.last

    async def _listen(self) -> None:
        failures = 0
        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.subscribe(LIVE_CHANNEL)
                    failures = 0
                    async for message in pubsub.listen():
                        if message.get("type") == "message":
                            self.dispatch(message["data"])
            except RedisError as exc:
                failures += 1
                backoff = min(2**failures, RECONNECT_MAX_SECONDS)
                log.warning("live_subscription_lost", error=repr(exc), retry_in_s=backoff)
                await asyncio.sleep(backoff)
