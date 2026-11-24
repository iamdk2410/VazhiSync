"""Hard operating rules (docs/rulebook.md) as code constraints on candidates (rule 13).

A candidate that breaks any rule is dropped before it is simulated, with the rule ids as
reasons; no model can override this. R7 (no fix that creates a new bunch) needs simulation
results, so the Control Advisor applies it when ranking.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.graph.state import Candidate

MAX_HOLD_S = 180  # R1
MAX_SKIPPED_STOPS = 2  # R2
# R3: stops that are never skipped: hospitals and the Central railway interchange (32B), plus
# any stop whose name says "Hospital" on any route.
NO_SKIP_STOPS: dict[str, frozenset[str]] = {
    "32B": frozenset({"MTC-6809", "MTC-6808", "MTC-83", "MTC-82"}),
}
NO_SKIP_NAME_WORDS = ("hospital",)
LAST_TRIPS_NO_SHORT_TURN = 2  # R4: no short-turns on the day's last two trips
COOLDOWN = timedelta(minutes=10)  # R5: no new instruction to the same bus within this


@dataclass(frozen=True)
class RecentInstruction:
    vehicle_id: str
    action_type: str
    sent_at: datetime
    active: bool  # sent or acknowledged, not yet done/expired/cancelled


@dataclass
class RuleContext:
    route_id: str
    world_time_s: float  # the world's clock (seconds since midnight)
    now: datetime  # wall clock, for instruction cooldowns
    stop_names: dict[str, str]  # stop_id -> name
    last_departure_s: dict[int, int]  # direction -> last scheduled departure (seconds)
    headway_s: dict[int, float]  # direction -> scheduled headway
    recent: list[RecentInstruction] = field(default_factory=list)


def _no_skip(ctx: RuleContext, stop_id: str) -> bool:
    if stop_id in NO_SKIP_STOPS.get(ctx.route_id, frozenset()):
        return True
    name = ctx.stop_names.get(stop_id, "").lower()
    return any(word in name for word in NO_SKIP_NAME_WORDS)


def violations(c: Candidate, ctx: RuleContext, direction_id: int | None = None) -> list[str]:
    """Rule ids (with a short reason) that `c` breaks; empty = allowed."""
    if c.action_type == "do_nothing":
        return []
    out: list[str] = []
    if c.action_type == "hold" and int(c.params.get("seconds", 0)) > MAX_HOLD_S:
        out.append(f"R1: holds are at most {MAX_HOLD_S} s")
    if c.action_type == "skip_stop":
        stops = list(c.params.get("stop_ids", []))
        if len(stops) > MAX_SKIPPED_STOPS:
            out.append(f"R2: at most {MAX_SKIPPED_STOPS} stops may be skipped")
        protected = [s for s in stops if _no_skip(ctx, s)]
        if protected:
            out.append(f"R3: never skip {', '.join(ctx.stop_names.get(s, s) for s in protected)}")
    if c.action_type == "short_turn" and direction_id is not None:
        last = ctx.last_departure_s.get(direction_id)
        headway = ctx.headway_s.get(direction_id, 0.0)
        if last is not None and ctx.world_time_s >= last - LAST_TRIPS_NO_SHORT_TURN * headway:
            out.append("R4: no short-turns on the day's last two trips")
    for recent in ctx.recent:
        if c.vehicle_id is None or recent.vehicle_id != str(c.vehicle_id):
            continue
        if ctx.now - recent.sent_at < COOLDOWN:
            out.append(f"R5: {c.fleet_number} was instructed less than {COOLDOWN} ago")
        if c.action_type == "hold" and recent.active and recent.action_type == "hold":
            out.append(f"R6: {c.fleet_number} is already holding")
    return sorted(set(out))
