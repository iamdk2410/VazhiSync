"""A/B evaluation (B19): the same scenario with no control vs with the advisor.

The world is a golden scenario's simulation (reality). Arms:
- control: nobody intervenes;
- advisor: every minute, a bunched pair triggers the advisor's own decision chain
  (`propose` -> rulebook -> what-if simulation over seeds -> `rank`); the recommendation is
  auto-approved (evaluation mode) and applied to the world;
- advisor_ew: the same, plus early-warning triggers (`predict_bunching`, B21).

The advisor sees only what the MCP tools would report (app/eval/observe.py, rule 1): it does
not know the scenario's weather, surges or breakdowns, exactly as in production. Each applied
action is also measured against ground truth: the world is copied just before it, and the
copy is run with and without the action (same random numbers) to get the *actual* benefit,
compared with what the advisor predicted.
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.agents.action_generator import propose
from app.agents.control_advisor import rank
from app.agents.early_warning import predict_bunching
from app.agents.impact_simulator import observations_from, run_seeds, summarize, to_action
from app.agents.rulebook import RecentInstruction, RuleContext, violations
from app.eval import observe
from app.graph.state import Snapshot
from scenario_gen.checks import ScenarioContext, dropouts, to_sim_config
from scenario_gen.golden import GoldenScenario
from simulator import ActionError, DoNothing, RouteModel, SimConfig, Simulation

Arm = Literal["control", "advisor", "advisor_ew"]
ARMS: tuple[Arm, ...] = ("control", "advisor", "advisor_ew")
DECIDE_EVERY_S = 60
WHATIF_SEEDS = (11, 22, 33)
HORIZON_MIN = 15.0
PAIR_COOLDOWN_S = 10 * 60  # don't re-plan the same pair straight away (R5 in spirit)
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)  # wall clock for cooldowns = world clock


@dataclass
class Intervention:
    at_min: float
    trigger: Literal["bunched", "predicted"]
    pair: str
    action: str
    predicted_benefit: float  # passenger-minutes, what the advisor's simulation said
    actual_benefit: float  # passenger-minutes, the world with vs without it (ground truth)


@dataclass
class ArmResult:
    scenario: str
    seed: int
    arm: Arm
    minutes: int
    passenger_wait_minutes: float
    avg_wait_s: float | None
    headway_cv: float | None
    bunching_count: int
    left_behind: int
    decisions: int = 0  # times the advisor ran
    do_nothing: int = 0  # ...and decided to do nothing
    interventions: list[Intervention] = field(default_factory=list)
    unsafe: int = 0  # applied actions that break the rulebook
    invalid: int = 0  # recommended actions the world couldn't carry out
    cpu_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "interventions"}
        out["interventions"] = [i.__dict__ for i in self.interventions]
        return out


def _rule_context(
    route: RouteModel, world_time_s: float, recent: list[RecentInstruction]
) -> RuleContext:
    return RuleContext(
        route_id=route.route_id,
        world_time_s=world_time_s,
        now=EPOCH + timedelta(seconds=world_time_s),
        stop_names={s.stop_id: s.name for d in route.directions for s in d.stops},
        last_departure_s={d.direction_id: 24 * 3600 for d in route.directions},
        headway_s={d.direction_id: float(d.scheduled_headway_s) for d in route.directions},
        recent=recent,
    )


def _wait(sim: Simulation, minutes: float) -> float:
    clone = sim.copy()
    clone.reset_metrics()
    clone.step(minutes * 60)
    return clone.metrics().passenger_wait_minutes


def decide(
    world: Simulation,
    gps: observe.Gps,
    route: RouteModel,
    dirs: dict[int, Any],
    pair_id: tuple[str, str],
    recent: list[RecentInstruction],
    whatif: SimConfig,
    seeds: tuple[int, ...],
    horizon_min: float,
) -> tuple[Any, list[Any]] | None:
    """The advisor's decision for one pair, from observations only: (recommendation,
    candidates), or None if the pair is gone."""
    world_time_s = world.snapshot().time_s
    pos = gps.positions(route.route_id, world_time_s)
    hw = gps.headways(route.route_id, dirs, whatif)
    est = observe.estimates(world, route)
    pair = next(
        (p for p in hw.pairs if (p.leader_fleet_number, p.follower_fleet_number) == pair_id),
        None,
    )
    if pair is None or pair.status == "stale":  # rule 4: never act on stale GPS
        return None
    snapshot = Snapshot(
        world_time_s=world_time_s, positions=pos, headways=hw, estimates=est, pair=pair
    )
    ctx = _rule_context(route, world_time_s, recent)
    candidates = [
        c for c in propose(snapshot, route, est) if not violations(c, ctx, pair.direction_id)
    ]
    observations = observations_from(pos, est, world_time_s, route)
    direction_of = {v.vehicle_id: v.direction for v in observations.vehicles}
    outcomes = []
    for c in candidates:
        try:
            runs = run_seeds(
                route, observations, whatif, to_action(c, route, direction_of), seeds, horizon_min
            )
        except (ActionError, KeyError, ValueError):
            continue
        rows = [
            {
                "passenger_wait_minutes": m.passenger_wait_minutes,
                "bunching_count": m.bunching_count,
                "left_behind": m.left_behind,
                "overcarried": m.overcarried,
                "displaced": m.displaced,
                "headway_cv": m.headway_cv,
            }
            for m in runs
        ]
        outcomes.append(summarize(c.key, list(seeds), rows, 0))
    return rank(outcomes, candidates, horizon_min), candidates


def play(
    golden: GoldenScenario,
    route: RouteModel,
    arm: Arm,
    seed: int,
    *,
    max_minutes: int = 60,
    seeds: tuple[int, ...] = WHATIF_SEEDS,
    horizon_min: float = HORIZON_MIN,
) -> ArmResult:
    t0 = time.process_time()
    ctx = ScenarioContext.for_route(route)
    config = to_sim_config(golden.plan, ctx, SimConfig())
    world = Simulation.new(route, config, seed)
    world.reset_metrics()
    dirs = observe.directions_of(route)
    # The advisor's what-if model: the same ratios, but it doesn't know the scenario's events.
    whatif = SimConfig(bunching_ratio=config.bunching_ratio, closing_ratio=config.closing_ratio)
    minutes = min(golden.plan.duration_min, max_minutes)
    gps = observe.Gps([(d.bus_id, d.start_s, d.duration_s) for d in dropouts(golden.plan, ctx)])
    recent: list[RecentInstruction] = []
    handled: dict[tuple[str, str], float] = {}
    result_extra: dict[str, Any] = {"decisions": 0, "do_nothing": 0, "unsafe": 0, "invalid": 0}
    interventions: list[Intervention] = []

    elapsed = 0
    while elapsed < minutes * 60:
        gps.refresh(world, elapsed)
        if arm != "control" and elapsed > 0:
            t = world.snapshot().time_s
            hw = gps.headways(route.route_id, dirs, whatif)
            triggers: list[tuple[tuple[str, str], Literal["bunched", "predicted"]]] = [
                ((p.leader_fleet_number, p.follower_fleet_number), "bunched")
                for p in hw.pairs
                if p.status == "bunched"
            ]
            if arm == "advisor_ew":
                obs = observations_from(
                    gps.positions(route.route_id, t),
                    observe.estimates(world, route),
                    t,
                    route,
                )
                triggers += [
                    ((p.leader, p.follower), "predicted")
                    for p in predict_bunching(
                        route, dirs, obs, whatif, hw.pairs, seeds=(1, 2, 3),
                        horizon_s=horizon_min * 60, step_s=30, min_agree=2,
                    )
                ]  # fmt: skip
            for pair_id, trigger in triggers:
                if t - handled.get(pair_id, -1e9) < PAIR_COOLDOWN_S:
                    continue
                handled[pair_id] = t
                decision = decide(
                    world, gps, route, dirs, pair_id, recent, whatif, seeds, horizon_min
                )
                if decision is None:
                    continue
                rec, candidates = decision
                result_extra["decisions"] += 1
                chosen = next((c for c in candidates if c.key == rec.candidate_key), None)
                if chosen is None or chosen.action_type == "do_nothing":
                    result_extra["do_nothing"] += 1
                    continue
                ctx_now = _rule_context(route, t, recent)
                if violations(chosen, ctx_now, None):  # auto-approval never skips the rulebook
                    result_extra["unsafe"] += 1
                direction_of = {b.bus_id: b.direction for b in world.snapshot().buses}
                action = to_action(chosen, route, direction_of)
                before = world.copy()
                try:
                    world.apply(action)
                except ActionError:
                    result_extra["invalid"] += 1
                    continue
                actual = _wait(before, horizon_min) - _wait(world, horizon_min)
                recent.append(
                    RecentInstruction(
                        str(chosen.vehicle_id), chosen.action_type,
                        EPOCH + timedelta(seconds=t), True,
                    )
                )  # fmt: skip
                interventions.append(
                    Intervention(
                        at_min=round(elapsed / 60, 1),
                        trigger=trigger,
                        pair=f"{pair_id[0]}>{pair_id[1]}",
                        action=chosen.key,
                        predicted_benefit=rec.benefit_passenger_minutes,
                        actual_benefit=round(actual, 1),
                    )
                )
        world.step(DECIDE_EVERY_S)
        elapsed += DECIDE_EVERY_S
    m = world.metrics()
    return ArmResult(
        scenario=golden.key,
        seed=seed,
        arm=arm,
        minutes=minutes,
        passenger_wait_minutes=round(m.passenger_wait_minutes, 1),
        avg_wait_s=None if m.avg_wait_s is None else round(m.avg_wait_s, 1),
        headway_cv=None if m.headway_cv is None else round(m.headway_cv, 3),
        bunching_count=m.bunching_count,
        left_behind=m.left_behind,
        interventions=interventions,
        cpu_s=round(time.process_time() - t0, 1),
        **result_extra,
    )


__all__ = ["ARMS", "Arm", "ArmResult", "DoNothing", "play"]
