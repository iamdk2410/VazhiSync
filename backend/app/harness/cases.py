"""Case retrieval (B22, decision 6): learn from past interventions without text embeddings.

A case is the situation an intervention was decided in, as a normalised numeric vector built
only from MCP observations (rule 1), plus what was done and what it achieved. Retrieval is a
pgvector L2 nearest-neighbour query; the context builder hands the closest cases to the Action
Generator, and the officer sees them on the recommendation card. If similar cases of the
recommended action saved clearly less than predicted, the confidence is lowered: computed
here in code, never by an LLM (rule 2).

Features (each scaled to about 0..1):
  gap_ratio         pair headway / scheduled (clipped at 1.5)
  route_position    follower's distance along its direction
  direction         0 or 1
  time_sin/time_cos time of day on the world clock (cyclic)
  speed_factor      observed mean speed / timetabled speed (traffic and weather show up here:
                    the advisor never sees the world's weather, only how the buses move)
  leader_load       passengers / capacity
  follower_load     passengers / capacity
  next_stop_demand  expected boardings at the leader's next 3 stops
"""

import math
import statistics
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Case
from app.db.models.cases import CASE_FEATURES
from app.graph.state import Snapshot
from app.runner.route_loader import WorldRoute
from mcp_servers.schemas import VehiclePosition

FEATURE_NAMES = (
    "gap_ratio",
    "route_position",
    "direction",
    "time_sin",
    "time_cos",
    "speed_factor",
    "leader_load",
    "follower_load",
    "next_stop_demand",
)
if len(FEATURE_NAMES) != CASE_FEATURES:  # the column's dimension must match
    raise RuntimeError("FEATURE_NAMES and cases.features disagree")

BUS_CAPACITY = 80  # simulator default (SimConfig.capacity)
DEMAND_SCALE = 60.0  # boardings at three stops that count as "very busy"
UNKNOWN = 0.5  # a missing reading sits in the middle, so it neither attracts nor repels
MAX_DISTANCE = 0.6  # L2 distance beyond which a case is not "similar"
TOP_K = 3
UNDERPERFORMED_RATIO = 0.7  # actual < 70 % of predicted on average -> confidence lowered
MIN_MEASURED = 2  # measured similar cases of the same action needed to judge


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _vehicle(snapshot: Snapshot, vehicle_id: uuid.UUID) -> VehiclePosition | None:
    return next((v for v in snapshot.positions.vehicles if v.vehicle_id == vehicle_id), None)


def _load(v: VehiclePosition | None) -> float:
    return UNKNOWN if v is None or v.load is None else _clip(v.load / BUS_CAPACITY)


def raw_features(snapshot: Snapshot, route: WorldRoute) -> dict[str, float]:
    """The situation in plain numbers (also stored, so a case stays readable)."""
    pair = snapshot.pair
    d = pair.direction_id
    direction = route.model.direction(d)
    info = route.directions[d]
    leader = _vehicle(snapshot, pair.leader_vehicle_id)
    follower = _vehicle(snapshot, pair.follower_vehicle_id)
    day = 2 * math.pi * (snapshot.world_time_s % 86400) / 86400
    speeds = [
        v.speed_mps
        for v in snapshot.positions.vehicles
        if v.speed_mps is not None and not v.is_stale
    ]
    demand: float | None = None
    if snapshot.estimates is not None and leader is not None and leader.distance_m is not None:
        here = direction.stop_index_at(leader.distance_m)
        upcoming = {
            direction.stops[i].stop_id for i in range(here + 1, min(here + 4, direction.n_stops))
        }
        values = [
            s.expected_boardings
            for s in snapshot.estimates.stops
            if s.direction_id == d and s.stop_id in upcoming and s.expected_boardings is not None
        ]
        demand = sum(values) if values else None
    position = (
        (follower.distance_m or 0.0) / direction.length_m
        if follower is not None and follower.distance_m is not None
        else UNKNOWN
    )
    return {
        "gap_ratio": round(pair.ratio, 4),
        "route_position": round(position, 4),
        "direction": float(d),
        "time_of_day_s": round(snapshot.world_time_s % 86400, 1),
        "time_sin": round(math.sin(day), 4),
        "time_cos": round(math.cos(day), 4),
        "speed_factor": round(statistics.fmean(speeds) / info.avg_speed_mps, 4) if speeds else -1,
        "leader_load": float(leader.load) if leader and leader.load is not None else -1,
        "follower_load": float(follower.load) if follower and follower.load is not None else -1,
        "next_stop_demand": round(demand, 2) if demand is not None else -1,
    }


def vectorize(raw: dict[str, float]) -> list[float]:
    """Scale the raw numbers to about 0..1 (missing readings: -1 -> the middle)."""

    def scaled(value: float, scale: float, hi: float = 1.0) -> float:
        return UNKNOWN if value < 0 else _clip(value / scale, 0.0, hi)

    return [
        round(_clip(raw["gap_ratio"] / 1.5), 4),
        round(_clip(raw["route_position"]), 4),
        raw["direction"],
        round((raw["time_sin"] + 1) / 2, 4),
        round((raw["time_cos"] + 1) / 2, 4),
        round(scaled(raw["speed_factor"], 2.0), 4),
        round(scaled(raw["leader_load"], BUS_CAPACITY), 4),
        round(scaled(raw["follower_load"], BUS_CAPACITY), 4),
        round(scaled(raw["next_stop_demand"], DEMAND_SCALE), 4),
    ]


class SimilarCase(BaseModel):
    """What the Action Generator, the officer's card and the run view see of a past case."""

    case_id: uuid.UUID
    distance: float
    action_type: str
    params: dict[str, Any]
    predicted_benefit_passenger_minutes: float
    actual_benefit_passenger_minutes: float | None
    bunching_resolved: bool | None
    decided_at: str  # ISO timestamp
    situation: dict[str, float]  # the raw features: gap ratio, loads, time of day...


async def similar_cases(
    session: AsyncSession,
    route_id: str,
    vector: list[float],
    *,
    k: int = TOP_K,
    max_distance: float = MAX_DISTANCE,
) -> list[SimilarCase]:
    distance = Case.features.l2_distance(vector).label("distance")
    rows = (
        await session.execute(
            select(Case, distance)
            .where(Case.route_id == route_id)
            .order_by(distance, Case.created_at.desc())
            .limit(k)
        )
    ).all()
    return [
        SimilarCase(
            case_id=case.id,
            distance=round(float(dist), 4),
            action_type=case.action_type.value,
            params=case.params,
            predicted_benefit_passenger_minutes=case.predicted_benefit_passenger_minutes,
            actual_benefit_passenger_minutes=case.actual_benefit_passenger_minutes,
            bunching_resolved=case.bunching_resolved,
            decided_at=case.created_at.isoformat(),
            situation=case.feature_values,
        )
        for case, dist in rows
        if float(dist) <= max_distance
    ]


@dataclass(frozen=True)
class Confidence:
    level: Literal["normal", "lowered"]
    note: str | None


def confidence(action_type: str, cases: list[SimilarCase]) -> Confidence:
    """Lowered when similar past cases of this action, measured afterwards, saved clearly
    less than the simulator predicted for them."""
    measured = [
        c
        for c in cases
        if c.action_type == action_type
        and c.actual_benefit_passenger_minutes is not None
        and c.predicted_benefit_passenger_minutes > 0
    ]
    if len(measured) < MIN_MEASURED:
        return Confidence("normal", None)
    predicted = sum(c.predicted_benefit_passenger_minutes for c in measured)
    actual = sum(c.actual_benefit_passenger_minutes or 0.0 for c in measured)
    ratio = actual / predicted
    if ratio >= UNDERPERFORMED_RATIO:
        return Confidence("normal", None)
    return Confidence(
        "lowered",
        f"In {len(measured)} similar past cases this kind of action saved {actual:.0f} "
        f"passenger-minutes against {predicted:.0f} predicted ({ratio:.0%}); treat the "
        "estimate with caution.",
    )
