"""Control Advisor: ranks the simulated options and explains the winner.

The ranking comes from the simulator's numbers only (rule 2). Every candidate ran on the same
seeds as "do nothing", so each seed gives a paired benefit (passenger-minutes of waiting saved);
from those: the mean with a 95 % confidence range, and a bad case (10th percentile) for the
"safest option". R7 rejects a fix that creates more bunching than doing nothing. "Do nothing"
wins when no mean benefit is positive. If the top two confidence ranges overlap it is a
near-tie, escalated to the officer. Explanations are a template for now (B9: the LLM writes
them, from these same numbers).
"""

import math
import statistics
from typing import Any

from app.db.models import Recommendation
from app.graph.deps import WorkflowDeps
from app.graph.state import (
    Candidate,
    RankedCandidate,
    RecommendationResult,
    SimOutcome,
    WorkflowState,
)

# Two-sided 95 % Student-t critical values by degrees of freedom (n - 1).
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}
BAD_CASE_QUANTILE = 0.10


def describe(c: Candidate) -> str:
    if c.action_type == "hold":
        return f"Hold {c.fleet_number} for {c.params['seconds']} s"
    if c.action_type == "skip_stop":
        return f"{c.fleet_number} skips {len(c.params['stop_ids'])} stop(s)"
    if c.action_type == "short_turn":
        return f"Short-turn {c.fleet_number}"
    return "Do nothing"


def quantile(values: list[float], q: float) -> float:
    """Linear-interpolation quantile (like numpy's default)."""
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def paired_benefits(baseline: SimOutcome, outcome: SimOutcome) -> list[float]:
    """Per-seed benefit (same seed for both: common random numbers), else the mean only."""
    same_seeds = baseline.seeds == outcome.seeds
    if same_seeds and len(baseline.per_seed_wait) == len(outcome.per_seed_wait) > 0:
        return [b - c for b, c in zip(baseline.per_seed_wait, outcome.per_seed_wait, strict=True)]
    return [(baseline.passenger_wait_minutes or 0.0) - (outcome.passenger_wait_minutes or 0.0)]


def interval(values: list[float]) -> tuple[float, float, float]:
    """(mean, low, high): a 95 % confidence range of the mean."""
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, mean, mean
    half = T95.get(len(values) - 1, 1.96) * statistics.stdev(values) / math.sqrt(len(values))
    return mean, mean - half, mean + half


def _escalate_no_baseline() -> RecommendationResult:
    return RecommendationResult(
        candidate_key="do_nothing",
        benefit_passenger_minutes=0.0,
        ranking=[],
        explanation="The do-nothing baseline could not be simulated, so the options can't be "
        "compared. Escalated to the officer.",
        is_near_tie=False,
        end_reason="escalated",
    )


def rank(
    outcomes: list[SimOutcome], candidates: list[Candidate], horizon_min: float
) -> RecommendationResult:
    by_key = {c.key: c for c in candidates}
    baseline = next(
        (o for o in outcomes if o.candidate_key == "do_nothing" and o.status == "completed"), None
    )
    if baseline is None or baseline.passenger_wait_minutes is None:
        return _escalate_no_baseline()
    failed = sorted(o.candidate_key for o in outcomes if o.status != "completed")
    base_wait = baseline.passenger_wait_minutes
    base_bunch = baseline.bunching_count or 0.0
    ranking: list[RankedCandidate] = []
    for o in outcomes:
        if o.status != "completed" or o.candidate_key == "do_nothing":
            continue
        benefits = paired_benefits(baseline, o)
        mean, low, high = interval(benefits)
        bunch = o.bunching_count or 0.0
        ranking.append(
            RankedCandidate(
                candidate_key=o.candidate_key,
                benefit_passenger_minutes=round(mean, 1),
                benefit_low=round(low, 1),
                benefit_high=round(high, 1),
                bad_case_benefit=round(quantile(benefits, BAD_CASE_QUANTILE), 1),
                passenger_wait_minutes=o.passenger_wait_minutes or 0.0,
                bunching_count=bunch,
                rejected_reason=(
                    "R7: creates more bunching than doing nothing" if bunch > base_bunch else None
                ),
            )
        )
    ranking.sort(key=lambda r: (r.rejected_reason is not None, -r.benefit_passenger_minutes))
    ranking.append(
        RankedCandidate(
            candidate_key="do_nothing",
            benefit_passenger_minutes=0.0,
            benefit_low=0.0,
            benefit_high=0.0,
            bad_case_benefit=0.0,
            passenger_wait_minutes=base_wait,
            bunching_count=base_bunch,
        )
    )
    viable = [r for r in ranking if r.rejected_reason is None and r.benefit_passenger_minutes > 0]
    note = f" Not simulated in time: {', '.join(failed)}." if failed else ""
    if not viable:
        return RecommendationResult(
            candidate_key="do_nothing",
            benefit_passenger_minutes=0.0,
            ranking=ranking,
            explanation=f"No action beats letting the buses run over the next {horizon_min:.0f} "
            f"minutes (baseline {base_wait:.0f} passenger-minutes of waiting).{note}",
            is_near_tie=False,
            end_reason="do_nothing_recommended",
        )
    best = viable[0]
    runner_up = viable[1] if len(viable) > 1 else None
    near_tie = runner_up is not None and runner_up.benefit_high >= best.benefit_low
    safest = max(viable, key=lambda r: r.bad_case_benefit)
    c = by_key[best.candidate_key]
    spread = (best.benefit_high - best.benefit_low) / 2
    explanation = (
        f"{describe(c)}: saves about {best.benefit_passenger_minutes:.0f} ± {spread:.0f} "
        f"passenger-minutes of waiting over the next {horizon_min:.0f} minutes versus doing "
        f"nothing (worst case {best.bad_case_benefit:.0f}), with {best.bunching_count:.1f} "
        f"bunching episodes vs {base_bunch:.1f}. {c.rationale}"
    )
    safest_key = safest.candidate_key if safest.candidate_key != best.candidate_key else None
    if safest_key is not None:
        explanation += (
            f" Safest option (best worst case, {safest.bad_case_benefit:.0f}): "
            f"'{describe(by_key[safest_key])}'."
        )
    if near_tie and runner_up is not None:
        explanation += (
            f" Near-tie: '{describe(by_key[runner_up.candidate_key])}' saves "
            f"{runner_up.benefit_passenger_minutes:.0f} and the ranges overlap - the officer "
            "should choose."
        )
    return RecommendationResult(
        candidate_key=best.candidate_key,
        benefit_passenger_minutes=best.benefit_passenger_minutes,
        ranking=ranking,
        explanation=explanation + note,
        is_near_tie=near_tie,
        end_reason="escalated" if near_tie else "recommended",
        safest_candidate_key=safest_key,
    )


async def control_advisor(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    candidates = state.get("candidates", [])
    result = rank(state.get("sim_outcomes", []), candidates, deps.horizon_min)
    chosen = next((c for c in candidates if c.key == result.candidate_key), None)
    if chosen is not None and chosen.db_id is not None:
        async with deps.sessionmaker() as session:
            session.add(
                Recommendation(
                    run_id=deps.run_id,
                    candidate_id=chosen.db_id,
                    ranking=[r.model_dump() for r in result.ranking],
                    benefit_passenger_minutes=result.benefit_passenger_minutes,
                    explanation=result.explanation,
                    is_near_tie=result.is_near_tie,
                )
            )
            await session.commit()
    routes = {"recommended": "recommend", "escalated": "escalate"}
    return {
        "recommendation": result,
        "end_reason": result.end_reason,
        "route": routes.get(result.end_reason, "do_nothing"),
    }
