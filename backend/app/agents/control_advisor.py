"""Control Advisor: ranks the simulated options and explains the winner.

The ranking comes from the simulator's numbers only (rule 2): benefit = passenger-minutes of
waiting saved versus doing nothing, over the same horizon and seeds. R7: a fix that creates
more bunching than doing nothing is rejected. "Do nothing" wins when nothing beats it; a
near-tie between the top two is escalated to the officer. Explanations are a template for
now (B9: the LLM writes them, from these same numbers).
"""

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

NEAR_TIE_MIN_PAX_MIN = 20.0
NEAR_TIE_SHARE = 0.10  # top two within 10 % of the best benefit


def describe(c: Candidate) -> str:
    if c.action_type == "hold":
        return f"Hold {c.fleet_number} for {c.params['seconds']} s"
    if c.action_type == "skip_stop":
        return f"{c.fleet_number} skips {len(c.params['stop_ids'])} stop(s)"
    if c.action_type == "short_turn":
        return f"Short-turn {c.fleet_number}"
    return "Do nothing"


def rank(
    outcomes: list[SimOutcome], candidates: list[Candidate], horizon_min: float
) -> RecommendationResult:
    by_key = {c.key: c for c in candidates}
    baseline = next(
        (o for o in outcomes if o.candidate_key == "do_nothing" and o.status == "completed"), None
    )
    failed = sorted(o.candidate_key for o in outcomes if o.status != "completed")
    if baseline is None or baseline.passenger_wait_minutes is None:
        return RecommendationResult(
            candidate_key="do_nothing",
            benefit_passenger_minutes=0.0,
            ranking=[],
            explanation="The do-nothing baseline could not be simulated, so the options can't "
            "be compared. Escalated to the officer.",
            is_near_tie=False,
            end_reason="escalated",
        )
    base_wait = baseline.passenger_wait_minutes
    base_bunch = baseline.bunching_count or 0.0
    ranking: list[RankedCandidate] = []
    for o in outcomes:
        if o.status != "completed" or o.candidate_key == "do_nothing":
            continue
        wait = o.passenger_wait_minutes or 0.0
        bunch = o.bunching_count or 0.0
        ranking.append(
            RankedCandidate(
                candidate_key=o.candidate_key,
                benefit_passenger_minutes=round(base_wait - wait, 1),
                passenger_wait_minutes=wait,
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
    margin = max(NEAR_TIE_MIN_PAX_MIN, NEAR_TIE_SHARE * best.benefit_passenger_minutes)
    near_tie = runner_up is not None and (
        best.benefit_passenger_minutes - runner_up.benefit_passenger_minutes < margin
    )
    c = by_key[best.candidate_key]
    explanation = (
        f"{describe(c)}: saves about {best.benefit_passenger_minutes:.0f} passenger-minutes of "
        f"waiting over the next {horizon_min:.0f} minutes versus doing nothing "
        f"({best.passenger_wait_minutes:.0f} vs {base_wait:.0f}), with "
        f"{best.bunching_count:.1f} bunching episodes vs {base_bunch:.1f}. {c.rationale}"
    )
    if near_tie and runner_up is not None:
        explanation += (
            f" Near-tie with '{describe(by_key[runner_up.candidate_key])}' "
            f"({runner_up.benefit_passenger_minutes:.0f}): the officer should choose."
        )
    return RecommendationResult(
        candidate_key=best.candidate_key,
        benefit_passenger_minutes=best.benefit_passenger_minutes,
        ranking=ranking,
        explanation=explanation + note,
        is_near_tie=near_tie,
        end_reason="escalated" if near_tie else "recommended",
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
    route = {"recommended": "recommend", "escalated": "escalate"}.get(
        result.end_reason, "do_nothing"
    )
    return {"recommendation": result, "end_reason": result.end_reason, "route": route}
