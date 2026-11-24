"""Passenger Impact Simulator: each candidate simulated from the observations (rule 1).

The what-if model is built from what the MCP tools reported (positions, loads, staleness,
waiting counts, world clock) - never from the world simulator. The graph fans out with `Send`,
one branch per candidate; each branch submits exactly ONE simulation job to the sim-worker
queue (B11) and awaits it with a timeout (rule 8) - no nested parallelism. Every job runs the
same seeds (common random numbers), so per-seed differences between candidates are fair.
A job that doesn't finish in time is aborted and reported; the advisor ranks the rest.
"""

import asyncio
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from arq.jobs import Job
from redis.exceptions import RedisError

from app.db.enums import JobStatus
from app.db.models import SimulationJob
from app.graph.deps import WorkflowDeps
from app.graph.state import Candidate, SimOutcome, Snapshot, WorkflowState
from simulator import (
    Action,
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

log = structlog.get_logger()

POLL_S = 0.1  # how often a branch checks its job's result
_background: set[asyncio.Task[None]] = set()  # abort tasks still running (kept referenced)


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
    """CPU-bound; runs on the sim-worker (in a thread)."""
    return [
        fast_forward(from_observations(route, observations, config, seed), action, minutes)
        for seed in seeds
    ]


# ------------------------------------------------------------------ job payload (JSON-safe)


@dataclass(frozen=True)
class SimJobSpec:
    job_id: uuid.UUID
    run_id: str
    route_id: str
    candidate_key: str
    observations: Observations
    config: SimConfig
    action: Action
    seeds: tuple[int, ...]
    minutes: float


def job_payload(
    job_id: uuid.UUID,
    deps: WorkflowDeps,
    candidate: Candidate,
    observations: Observations,
) -> dict[str, Any]:
    return {
        "job_id": str(job_id),
        "run_id": deps.run_id,
        "route_id": deps.route.model.route_id,
        "candidate": candidate.model_dump(mode="json"),
        "observations": {
            "time_s": observations.time_s,
            "vehicles": [
                [v.vehicle_id, v.direction, v.distance_m, v.load, v.stale]
                for v in observations.vehicles
            ],
            "waiting": [[d, i, n] for (d, i), n in observations.waiting.items()],
        },
        "bunching_ratio": deps.settings.bunching_ratio,
        "closing_ratio": deps.settings.closing_ratio,
        "seeds": list(deps.seeds),
        "minutes": deps.horizon_min,
    }


def payload_to_job(payload: dict[str, Any], route: RouteModel) -> SimJobSpec:
    obs = payload["observations"]
    observations = Observations(
        time_s=float(obs["time_s"]),
        vehicles=tuple(
            VehicleObservation(str(v), int(d), float(dist), load, bool(stale))
            for v, d, dist, load, stale in obs["vehicles"]
        ),
        waiting={(int(d), int(i)): int(n) for d, i, n in obs["waiting"]},
    )
    candidate = Candidate.model_validate(payload["candidate"])
    direction_of = {v.vehicle_id: v.direction for v in observations.vehicles}
    return SimJobSpec(
        job_id=uuid.UUID(payload["job_id"]),
        run_id=str(payload["run_id"]),
        route_id=str(payload["route_id"]),
        candidate_key=candidate.key,
        observations=observations,
        config=SimConfig(
            bunching_ratio=float(payload["bunching_ratio"]),
            closing_ratio=float(payload["closing_ratio"]),
        ),
        action=to_action(candidate, route, direction_of),
        seeds=tuple(int(s) for s in payload["seeds"]),
        minutes=float(payload["minutes"]),
    )


# ------------------------------------------------------------------ results


def summarize(
    candidate_key: str, seeds: list[int], runs: list[dict[str, Any]], ms: int
) -> SimOutcome:
    def mean(key: str) -> float:
        return round(statistics.fmean(float(r[key]) for r in runs), 2)

    cvs = [float(r["headway_cv"]) for r in runs if r.get("headway_cv") is not None]
    return SimOutcome(
        candidate_key=candidate_key,
        status="completed",
        seeds=seeds,
        passenger_wait_minutes=mean("passenger_wait_minutes"),
        per_seed_wait=[round(float(r["passenger_wait_minutes"]), 1) for r in runs],
        bunching_count=mean("bunching_count"),
        left_behind=mean("left_behind"),
        overcarried=mean("overcarried"),
        displaced=mean("displaced"),
        headway_cv=round(statistics.fmean(cvs), 3) if cvs else None,
        duration_ms=ms,
    )


# ------------------------------------------------------------------ graph nodes


async def impact_simulator(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """Build the what-if inputs once; the graph then fans out one `simulate` branch each."""
    deps.observations = build_observations(state["snapshot"], deps.route.model)
    return {"route": "fan_out"}


async def _set_status(deps: WorkflowDeps, job_id: uuid.UUID, status: JobStatus, error: str) -> None:
    async with deps.sessionmaker() as session:
        row = await session.get(SimulationJob, job_id)
        if row is not None and row.status in (JobStatus.PENDING, JobStatus.RUNNING):
            row.status, row.error = status, error
            await session.commit()


async def simulate(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """One branch = one candidate = exactly one simulation job, awaited with a timeout."""
    candidate = state["candidate"]
    if deps.observations is None or deps.job_queue is None:
        raise RuntimeError("impact_simulator must run first, with a job queue")
    seeds = list(deps.seeds)
    job_id = uuid.uuid4()
    async with deps.sessionmaker() as session:
        session.add(
            SimulationJob(id=job_id, run_id=deps.run_id, candidate_id=candidate.db_id, seeds=seeds)
        )
        await session.commit()

    t0 = time.monotonic()
    timeout = min(deps.settings.sim_timeout_seconds, max(deps.budget.remaining_s, 0.1))
    job: Job | None = None
    try:
        async with deps.sim_semaphore:
            job = await deps.job_queue.enqueue_job(
                "simulate_candidate",
                job_payload(job_id, deps, candidate, deps.observations),
                _job_id=f"sim:{job_id}",
                _queue_name=deps.sim_queue,
            )
            if job is None:
                raise RuntimeError("job was not queued")
            # A hard wall-clock limit: arq's own timeout is only checked between polls.
            result: dict[str, Any] = await asyncio.wait_for(
                job.result(poll_delay=POLL_S), timeout=timeout
            )
    except TimeoutError:
        error = f"exceeded {timeout:g} s"
        await _set_status(deps, job_id, JobStatus.TIMED_OUT, error)
        if job is not None:
            task = asyncio.create_task(_abort(job))  # free the worker slot; don't wait for it
            _background.add(task)
            task.add_done_callback(_background.discard)
        return {"sim_outcomes": [_failed(candidate.key, seeds, "timed_out", error, t0)]}
    except (RedisError, OSError, RuntimeError) as exc:
        error = f"simulation queue unavailable: {exc!r}"
        await _set_status(deps, job_id, JobStatus.FAILED, error)
        return {"sim_outcomes": [_failed(candidate.key, seeds, "failed", error, t0)]}

    if result.get("status") != "completed":
        return {
            "sim_outcomes": [_failed(candidate.key, seeds, "failed", str(result.get("error")), t0)]
        }
    ms = round((time.monotonic() - t0) * 1000)
    return {"sim_outcomes": [summarize(candidate.key, seeds, result["runs"], ms)]}


def _failed(
    key: str, seeds: list[int], status: Literal["failed", "timed_out"], error: str, t0: float
) -> SimOutcome:
    return SimOutcome(
        candidate_key=key,
        status=status,
        seeds=seeds,
        error=error,
        duration_ms=round((time.monotonic() - t0) * 1000),
    )


async def _abort(job: Job) -> None:
    try:
        await job.abort(timeout=5, poll_delay=0.5)
    except (RedisError, OSError, TimeoutError) as exc:
        log.info("sim_job_abort_failed", job_id=job.job_id, error=repr(exc))
