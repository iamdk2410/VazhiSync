"""Control Advisor: ranks the simulated options and explains the winner.

The ranking comes from the simulator's numbers only (rule 2). Every candidate ran on the same
seeds as "do nothing", so each seed gives a paired benefit (passenger-minutes of waiting saved);
from those: the mean with a 95 % confidence range, and a bad case (10th percentile) for the
"safest option". R7 rejects a fix that creates more bunching than doing nothing. "Do nothing"
wins when no mean benefit is positive. If the top two confidence ranges overlap it is a
near-tie, escalated to the officer. The explanation is a template; with the LLM gateway (B9)
an LLM rewords it from these same numbers, and an answer that quotes a number the simulator
didn't produce is rejected (repaired once, else the template stays).
"""

import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from app.db.models import Recommendation
from app.graph.deps import WorkflowDeps
from app.graph.state import (
    Candidate,
    RankedCandidate,
    RecommendationResult,
    SimOutcome,
    WorkflowState,
)
from app.harness.context import control_advisor_messages, numbers_not_in

# Two-sided 95 % Student-t critical values by degrees of freedom (n - 1).
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}
BAD_CASE_QUANTILE = 0.10
AGENT = "control_advisor"
MAX_EXPLAINED = 4  # options shown to the LLM (the top of the ranking plus do nothing)


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


RECOMMENDATION_TTL = timedelta(seconds=90)  # B12: re-plan if the officer decides later


class Explanation(BaseModel):
    text: str = Field(min_length=20, max_length=700)


def explanation_context(
    result: RecommendationResult, candidates: list[Candidate], horizon_min: float
) -> list[dict[str, str]]:
    """The simulator's figures for the top options, worded for the officer (B9)."""
    by_key = {c.key: c for c in candidates}
    nothing = next((r for r in result.ranking if r.candidate_key == "do_nothing"), None)
    rows: list[dict[str, Any]] = []
    for r in result.ranking[:MAX_EXPLAINED]:
        c = by_key.get(r.candidate_key)
        row: dict[str, Any] = {
            "option": describe(c) if c else r.candidate_key,
            "passenger_minutes_saved_vs_doing_nothing": round(r.benefit_passenger_minutes),
            "likely_range_of_that_saving": [round(r.benefit_low), round(r.benefit_high)],
            "saving_in_a_bad_case": round(r.bad_case_benefit),
            "bunching_episodes_with_this_option": round(r.bunching_count, 1),
        }
        if nothing is not None:
            row["bunching_episodes_if_nothing_is_done"] = round(nothing.bunching_count, 1)
        if r.candidate_key == "do_nothing":
            row = {
                "option": "Do nothing",
                "total_passenger_minutes_of_waiting": round(r.passenger_wait_minutes),
            }
        if r.rejected_reason:
            row["rejected"] = r.rejected_reason
        if c is not None and c.action_type != "do_nothing":
            row["why"] = c.rationale
        rows.append(row)
    chosen = by_key.get(result.candidate_key)
    safest = by_key.get(result.safest_candidate_key or "")
    return control_advisor_messages(
        recommended=describe(chosen) if chosen else "Do nothing",
        ranking=rows,
        horizon_min=horizon_min,
        near_tie=result.is_near_tie,
        safest=describe(safest) if safest else None,
        note=None,
    )


async def explain(
    result: RecommendationResult, candidates: list[Candidate], deps: WorkflowDeps
) -> RecommendationResult:
    """Plain-language wording from the LLM, checked to quote only the simulator's numbers;
    the rules' wording when the gateway is off, fails or the budget is spent. The ranking
    itself is never touched (rule 2)."""
    if deps.llm is None or result.end_reason not in ("recommended", "escalated"):
        return result
    if not any(r.candidate_key == result.candidate_key for r in result.ranking):
        return result
    messages = explanation_context(result, candidates, deps.horizon_min)

    def invented(e: Explanation) -> list[str]:
        extra = numbers_not_in(e.text, messages)
        return [f"text: uses numbers not in the data: {', '.join(extra)}"] if extra else []

    answer = await deps.llm.complete(
        agent=AGENT,
        messages=messages,
        output=Explanation,
        rules=lambda: Explanation(text=result.explanation),
        run_id=deps.run_id,
        budget=deps.budget,
        check=invented,
    )
    if answer.from_rules:
        return result
    return result.model_copy(
        update={"explanation": answer.value.text.strip(), "explanation_source": "llm"}
    )


async def control_advisor(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    round_ = state.get("round", 0)
    candidates = [c for c in state.get("candidates", []) if c.round == round_]
    outcomes = [o for o in state.get("sim_outcomes", []) if o.round == round_]
    result = await explain(rank(outcomes, candidates, deps.horizon_min), candidates, deps)
    if result.end_reason == "do_nothing_recommended" and any(
        c.key.startswith("edit:") for c in candidates
    ):
        # The officer asked for this action: show them the result instead of ending silently.
        result = result.model_copy(
            update={
                "end_reason": "escalated",
                "explanation": "The officer's edit does not beat doing nothing. "
                + result.explanation,
            }
        )
    chosen = next((c for c in candidates if c.key == result.candidate_key), None)
    if chosen is not None and chosen.db_id is not None:
        async with deps.sessionmaker() as session:
            row = Recommendation(
                run_id=deps.run_id,
                candidate_id=chosen.db_id,
                ranking=[r.model_dump() for r in result.ranking],
                benefit_passenger_minutes=result.benefit_passenger_minutes,
                explanation=result.explanation,
                is_near_tie=result.is_near_tie,
                regeneration_round=round_,
                expires_at=datetime.now(UTC) + RECOMMENDATION_TTL,
            )
            session.add(row)
            await session.commit()
            result = result.model_copy(
                update={"recommendation_id": row.id, "expires_at": row.expires_at}
            )
    routes = {"recommended": "recommend", "escalated": "escalate"}
    return {
        "recommendation": result,
        "end_reason": result.end_reason,
        "route": routes.get(result.end_reason, "do_nothing"),
    }
