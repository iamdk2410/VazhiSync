"""Passenger Impact Simulator: each candidate simulated from the observations (rule 1).

The what-if model is built once with `from_observations` from what the MCP tools reported
(positions, loads, staleness, waiting counts, world clock) — never from the world simulator.
The graph then fans out with `Send`, one branch per candidate. Each branch runs the same seeds
(common random numbers), under a per-simulation timeout and a concurrency semaphore (rule 8).
B11 moves the runs onto ARQ workers.
"""

import asyncio
import statistics
import time
from typing import Any

from app.graph.deps import WorkflowDeps
from app.graph.state import Candidate, SimOutcome, Snapshot, WorkflowState
from simulator import (
    Action,
    ActionError,
    DoNothing,
    Hold,
    Metrics,
    Observations,
    RouteModel,
    ShortTurn,
    SimConfig,
    SkipStops,
    VehicleObservation,
    fast_forward,
    from_observations,
)


def build_observations(snapshot: Snapshot, route: RouteModel) -> Observations:
    vehicles = tuple(
        VehicleObservation(
            vehicle_id=v.fleet_number,
            direction=v.direction_id,
            distance_m=v.distance_m,
            load=v.load,
            stale=v.is_stale,
        )
        for v in snapshot.positions.vehicles
        if v.distance_m is not None
    )
    waiting: dict[tuple[int, int], int] = {}
    if snapshot.estimates is not None:
        index = {
            (d.direction_id, s.stop_id): i for d in route.directions for i, s in enumerate(d.stops)
        }
        for e in snapshot.estimates.stops:
            i = index.get((e.direction_id, e.stop_id))
            if i is not None and e.waiting_now is not None and e.data_quality == "ok":
                waiting[(e.direction_id, i)] = e.waiting_now
    return Observations(time_s=snapshot.world_time_s, vehicles=vehicles, waiting=waiting)


def to_action(c: Candidate, route: RouteModel, direction_of: dict[str, int]) -> Action:
    if c.action_type == "do_nothing" or c.fleet_number is None:
        return DoNothing()
    if c.action_type == "hold":
        return Hold(c.fleet_number, int(c.params["seconds"]))
    direction = route.direction(direction_of[c.fleet_number])
    index = {s.stop_id: i for i, s in enumerate(direction.stops)}
    if c.action_type == "skip_stop":
        return SkipStops(c.fleet_number, tuple(index[s] for s in c.params["stop_ids"]))
    return ShortTurn(c.fleet_number, index[c.params["at_stop_id"]])


def run_seeds(
    route: RouteModel,
    observations: Observations,
    config: SimConfig,
    action: Action,
    seeds: tuple[int, ...],
    minutes: float,
) -> list[Metrics]:
    """CPU-bound: runs in a worker thread."""
    return [
        fast_forward(from_observations(route, observations, config, seed), action, minutes)
        for seed in seeds
    ]


def summarize(
    candidate_key: str, seeds: tuple[int, ...], runs: list[Metrics], ms: int
) -> SimOutcome:
    def mean(values: list[float]) -> float:
        return round(statistics.fmean(values), 2)

    cvs = [m.headway_cv for m in runs if m.headway_cv is not None]
    return SimOutcome(
        candidate_key=candidate_key,
        status="completed",
        seeds=list(seeds),
        passenger_wait_minutes=mean([m.passenger_wait_minutes for m in runs]),
        per_seed_wait=[round(m.passenger_wait_minutes, 1) for m in runs],
        bunching_count=mean([float(m.bunching_count) for m in runs]),
        left_behind=mean([float(m.left_behind) for m in runs]),
        overcarried=mean([float(m.overcarried) for m in runs]),
        displaced=mean([float(m.displaced) for m in runs]),
        headway_cv=mean(cvs) if cvs else None,
        duration_ms=ms,
    )


async def impact_simulator(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Prepare the what-if model; the graph then fans out one `simulate` branch per candidate."""
    deps.observations = build_observations(state["snapshot"], deps.route.model)
    return {"route": "fan_out"}


async def simulate(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    candidate = state["candidate"]
    if deps.observations is None:
        raise RuntimeError("impact_simulator must run before simulate")
    direction_of = {v.vehicle_id: v.direction for v in deps.observations.vehicles}
    config = SimConfig(
        bunching_ratio=deps.settings.bunching_ratio, closing_ratio=deps.settings.closing_ratio
    )
    t0 = time.monotonic()
    try:
        action = to_action(candidate, deps.route.model, direction_of)
        async with deps.sim_semaphore:
            runs = await asyncio.wait_for(
                asyncio.to_thread(
                    run_seeds,
                    deps.route.model,
                    deps.observations,
                    config,
                    action,
                    deps.seeds,
                    deps.horizon_min,
                ),
                timeout=deps.settings.sim_timeout_seconds,
            )
    except TimeoutError:
        outcome = SimOutcome(
            candidate_key=candidate.key,
            status="timed_out",
            seeds=list(deps.seeds),
            error=f"exceeded {deps.settings.sim_timeout_seconds:g} s",
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
    except (ActionError, KeyError) as exc:
        outcome = SimOutcome(
            candidate_key=candidate.key,
            status="failed",
            seeds=list(deps.seeds),
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=round((time.monotonic() - t0) * 1000),
        )
    else:
        outcome = summarize(candidate.key, deps.seeds, runs, round((time.monotonic() - t0) * 1000))
    return {"sim_outcomes": [outcome]}
