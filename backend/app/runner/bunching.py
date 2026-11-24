"""Decide when a bunching event opens and resolves (pure; the runner persists the decisions).

- One open event per (leader, follower) pair: repeated "bunched" readings don't re-raise.
- Hysteresis: an event opens below BUNCHING_RATIO and resolves only once the headway is back
  above CLOSING_RATIO, so a pair hovering at the threshold can't flicker open/closed.
- A stale reading never opens or resolves anything (rule 4: no decisions on stale data).
- If the pair stops being adjacent (overtaking, or one bus reached the terminal), the event
  resolves: the situation it described no longer exists.
"""

import uuid
from dataclasses import dataclass, field

from app.live.headways import Headway

Pair = tuple[str, str]  # (leader vehicle id, follower vehicle id)


@dataclass(frozen=True)
class Decisions:
    open: list[Headway]
    resolve: list[uuid.UUID]


@dataclass
class BunchingTracker:
    closing_ratio: float
    open_events: dict[Pair, uuid.UUID] = field(default_factory=dict)

    def update(self, headways: list[Headway]) -> Decisions:
        current = {(h.leader_id, h.follower_id): h for h in headways}
        to_open = [
            h
            for pair, h in current.items()
            if h.status == "bunched" and pair not in self.open_events
        ]
        to_resolve: list[uuid.UUID] = []
        for pair, event_id in list(self.open_events.items()):
            h = current.get(pair)
            if h is None or (h.status != "stale" and h.ratio >= self.closing_ratio):
                to_resolve.append(event_id)
                del self.open_events[pair]
        return Decisions(open=to_open, resolve=to_resolve)

    def opened(self, pair: Pair, event_id: uuid.UUID) -> None:
        self.open_events[pair] = event_id
