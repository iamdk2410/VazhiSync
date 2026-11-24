"""Boarding estimates from counted queues (stop_queues), on the world's clock.

For each stop: people waiting now, an arrival rate estimated from recent counts, and the
boardings expected over a horizon. Between two counts a queue only grows by new arrivals
(it shrinks when a bus serves the stop), so the rate is the sum of increases over the world
time covered. It is an estimate from observations, never the simulator's demand model (rule 1).
"""

import itertools
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StopQueue
from mcp_servers.observations import age_seconds, route_model
from mcp_servers.schemas import BoardingEstimates, StopEstimate

LOOKBACK = timedelta(minutes=15)  # real time: bounds the query
RATE_WINDOW_S = 900.0  # world seconds of history used for the rate
MIN_RATE_SPAN_S = 60.0  # less history than this: no rate yet


def arrival_rate(samples: list[tuple[float, int]]) -> float | None:
    """Arrivals per minute from (world_time_s, waiting) samples, newest first.

    Walks back while the world clock goes backwards in time; a jump forward (the world was
    restarted, e.g. a scenario was loaded) ends the usable history.
    """
    arrivals, span = 0, 0.0
    for (t_new, n_new), (t_old, n_old) in itertools.pairwise(samples):
        dt = t_new - t_old
        if dt <= 0 or span >= RATE_WINDOW_S:
            break
        arrivals += max(n_new - n_old, 0)
        span += dt
    if span < MIN_RATE_SPAN_S:
        return None
    return round(arrivals / span * 60, 2)


async def boarding_estimates(
    session: AsyncSession,
    route_id: str,
    direction_id: int | None,
    horizon_minutes: int,
    now: datetime,
    stale_after_s: int,
) -> BoardingEstimates:
    route = await route_model(session, route_id)
    rows = (
        await session.execute(
            select(
                StopQueue.direction_id,
                StopQueue.stop_id,
                StopQueue.recorded_at,
                StopQueue.sim_time_s,
                StopQueue.waiting_count,
            )
            .where(StopQueue.route_id == route_id, StopQueue.recorded_at >= now - LOOKBACK)
            .order_by(StopQueue.recorded_at.desc())
        )
    ).all()
    history: dict[tuple[int, str], list[tuple[datetime, float | None, int]]] = defaultdict(list)
    for d, stop_id, recorded_at, sim_time_s, waiting in rows:
        if d is not None:
            history[(d, stop_id)].append((recorded_at, sim_time_s, waiting))

    stops: list[StopEstimate] = []
    for d, direction in enumerate(route.model.directions):
        if direction_id is not None and d != direction_id:
            continue
        for i, stop in enumerate(direction.stops[:-1]):  # nobody boards at the last stop
            samples = history.get((d, stop.stop_id), [])
            if not samples:
                stops.append(
                    StopEstimate(
                        direction_id=d, stop_id=stop.stop_id, name=stop.name, sequence=i + 1,
                        waiting_now=None, arrivals_per_min=None, expected_boardings=None,
                        sample_age_seconds=None, data_quality="no_data",
                    )
                )  # fmt: skip
                continue
            latest_at, _, waiting = samples[0]
            age = age_seconds(latest_at, now)
            rate = arrival_rate([(t, n) for _, t, n in samples if t is not None])
            stops.append(
                StopEstimate(
                    direction_id=d,
                    stop_id=stop.stop_id,
                    name=stop.name,
                    sequence=i + 1,
                    waiting_now=waiting,
                    arrivals_per_min=rate,
                    expected_boardings=round(waiting + (rate or 0.0) * horizon_minutes, 1),
                    sample_age_seconds=age,
                    data_quality="stale" if age > stale_after_s else "ok",
                )
            )
    return BoardingEstimates(
        route_id=route_id, server_time=now, horizon_minutes=horizon_minutes, stops=stops
    )
