"""Scenario vs a normal day, measured by the simulator (rule 2: numbers come from the
simulator, never the LLM). Same route, start time and seed; only the scenario differs."""

import dataclasses
from dataclasses import dataclass

from scenario_gen.checks import ScenarioContext, to_sim_config
from scenario_gen.schema import Scenario
from simulator import Metrics, SimConfig, Simulation

PREVIEW_MINUTES_MAX = 60


@dataclass(frozen=True)
class PreviewMetrics:
    avg_wait_s: float | None  # None when nobody boarded
    passenger_wait_minutes: float
    boarded: int
    left_behind: int
    bunching_count: int
    first_bunching_min: float | None
    headway_cv: float | None

    @classmethod
    def from_metrics(cls, m: Metrics) -> "PreviewMetrics":
        return cls(
            avg_wait_s=None if m.avg_wait_s is None else round(m.avg_wait_s, 1),
            passenger_wait_minutes=round(m.passenger_wait_minutes, 1),
            boarded=m.boarded,
            left_behind=m.left_behind,
            bunching_count=m.bunching_count,
            first_bunching_min=(
                None if m.first_bunching_s is None else round(m.first_bunching_s / 60, 1)
            ),
            headway_cv=None if m.headway_cv is None else round(m.headway_cv, 3),
        )


@dataclass(frozen=True)
class Preview:
    minutes: int
    baseline: PreviewMetrics
    scenario: PreviewMetrics


def run(config: SimConfig, ctx: ScenarioContext, seed: int, minutes: int) -> PreviewMetrics:
    sim = Simulation.new(ctx.route, config, seed)
    sim.step(minutes * 60)
    return PreviewMetrics.from_metrics(sim.metrics())


def preview(scenario: Scenario, ctx: ScenarioContext, base: SimConfig | None = None) -> Preview:
    base = base or SimConfig()
    minutes = min(scenario.plan.duration_min, PREVIEW_MINUTES_MAX)
    normal = dataclasses.replace(base, start_time_s=scenario.plan.start_time_s)
    return Preview(
        minutes=minutes,
        baseline=run(normal, ctx, scenario.seed, minutes),
        scenario=run(to_sim_config(scenario.plan, ctx, base), ctx, scenario.seed, minutes),
    )
