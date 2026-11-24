"""After the recommendation (B12): officer approval, dispatch, driver feedback, outcome.

    await_officer  (interrupt) -- approve --> dispatch -- sent --> await_driver (interrupt)
                               -- edit -----> apply_edit -> re-simulate -> await_officer
                               -- reject ---> action_generator (next round; max 2)
                               -- expired --> headway_monitor (re-plan with fresh data)
    await_driver -- ack --> await_outcome (interrupt, ~15 world-min) -> measure -> END
                 -- cant_comply / timeout --> headway_monitor (re-plan)

Interrupt nodes call `interrupt()` first: a node re-runs from the top when resumed, so nothing
before the interrupt may have side effects (job scheduling uses deterministic job ids).
Dispatch re-validates with fresh MCP data right before sending; the instruction id is derived
from the approval, so a replayed or double-clicked approval can only ever send once.
"""

import itertools
import statistics
from typing import Any

import structlog
from langgraph.types import interrupt
from sqlalchemy import select

from app.agents.headway_monitor import find_pair
from app.db.enums import (
    ActionType,
    ApprovalDecision,
    CandidateSource,
    NotificationKind,
)
from app.db.models import (
    Approval,
    CandidateAction,
    DriverInstruction,
    InterventionOutcome,
    Notification,
    Recommendation,
    StopQueue,
)
from app.graph.deps import WorkflowDeps
from app.graph.state import (
    MAX_ROUNDS,
    Candidate,
    DriverReply,
    OfficerDecision,
    OutcomeDue,
    WorkflowState,
)
from app.harness.errors import HookRejected, ToolFailed
from mcp_servers.schemas import Headways, InstructionSent

log = structlog.get_logger()

DRIVER_ACK_TIMEOUT_S = 120  # real seconds for the driver to acknowledge
OUTCOME_WINDOW_S = 900.0  # world seconds measured after the ack (the simulated horizon)
PASSENGER_NOTICE = "Service adjusted to even out the gaps between buses; the next bus is close."
REPLAN_CODES = {"stale_vehicle", "no_position", "invalid_action", "freshness_unknown"}


def _params_key(params: dict[str, Any]) -> str:
    return "-".join(f"{k}={v}" for k, v in sorted(params.items()))


def _next_round(state: WorkflowState, end_reason: str) -> dict[str, Any]:
    """Go round again if allowed, else end with `end_reason`."""
    current = state.get("round", 0)
    if current >= MAX_ROUNDS:
        return {"route": "exhausted", "end_reason": end_reason}
    return {"route": "replan", "round": current + 1}


def _current_candidate(state: WorkflowState) -> Candidate:
    rec = state["recommendation"]
    round_ = state.get("round", 0)
    return next(
        c for c in state.get("candidates", []) if c.key == rec.candidate_key and c.round == round_
    )


# ------------------------------------------------------------------ officer


async def await_officer(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    rec = state["recommendation"]
    value = interrupt(
        {
            "kind": "officer",
            "run_id": deps.run_id,
            "recommendation_id": str(rec.recommendation_id),
            "round": state.get("round", 0),
        }
    )
    decision = OfficerDecision.model_validate(value)  # rule 6: validated at the boundary
    out: dict[str, Any] = {"decision": decision.model_dump(mode="json"), "route": decision.decision}
    if decision.decision == "reject":
        out |= _next_round(state, "rejected")
        out["rejected_keys"] = [rec.candidate_key]
        if out["route"] == "replan":
            out["route"] = "reject"
    elif decision.decision == "expired":
        out |= _next_round(state, "replans_exhausted")
    elif decision.decision == "edit":
        out |= _next_round(state, "replans_exhausted")
        if out["route"] == "replan":
            out["route"] = "edit"
    return out


async def apply_edit(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    """The officer changed the parameters: simulate exactly that, against doing nothing."""
    base = next(
        c
        for c in state.get("candidates", [])
        if c.key == state["recommendation"].candidate_key and c.round == state["round"] - 1
    )
    params = dict(state["decision"].get("params") or {})
    round_ = state["round"]
    edited = base.model_copy(
        update={
            "key": f"edit:{base.fleet_number}:{_params_key(params)}",
            "params": params,
            "rationale": f"The officer's edit of: {base.rationale}",
            "round": round_,
            "db_id": None,
        }
    )
    nothing = Candidate(
        key="do_nothing", action_type="do_nothing", vehicle_id=None, fleet_number=None,
        params={}, rationale="Let the buses run; the baseline.", round=round_,
    )  # fmt: skip
    async with deps.sessionmaker() as session:
        rows = [
            CandidateAction(
                run_id=deps.run_id,
                action_type=ActionType(c.action_type),
                vehicle_id=c.vehicle_id,
                params=c.params,
                source=CandidateSource.OFFICER_EDIT if c is edited else CandidateSource.RULE,
                rationale=c.rationale,
                regeneration_round=round_,
            )
            for c in (edited, nothing)
        ]
        session.add_all(rows)
        await session.commit()
    fresh = [
        c.model_copy(update={"db_id": r.id}) for c, r in zip((edited, nothing), rows, strict=True)
    ]
    return {"candidates": [*state.get("candidates", []), *fresh], "route": "simulate"}


# ------------------------------------------------------------------ dispatch


async def _notify_passengers(deps: WorkflowDeps, c: Candidate, sent: InstructionSent) -> int:
    """Public service alerts for riders whose stop loses a bus (skip-stop / short-turn)."""
    if c.action_type not in ("skip_stop", "short_turn"):
        return 0
    route = deps.route.model
    stops: list[str] = list(c.params.get("stop_ids", []))
    if c.action_type == "short_turn":
        # Riders after the turn point (in that direction) lose this bus.
        for d in route.directions:
            ids = [s.stop_id for s in d.stops]
            if c.params["at_stop_id"] in ids:
                stops = ids[ids.index(c.params["at_stop_id"]) + 1 : -1][:10]
                break
    names = {s.stop_id: s.name for d in route.directions for s in d.stops}
    kind = (
        NotificationKind.SKIP_STOP if c.action_type == "skip_stop" else NotificationKind.SHORT_TURN
    )
    verb = "will not stop at" if c.action_type == "skip_stop" else "is turning back before"
    async with deps.sessionmaker() as session:
        session.add_all(
            Notification(
                route_id=route.route_id,
                stop_id=stop_id,
                driver_instruction_id=sent.id,
                kind=kind,
                title=f"Bus {sent.fleet_number} {verb} {names.get(stop_id, stop_id)}",
                body=PASSENGER_NOTICE,
            )
            for stop_id in stops
        )
        await session.commit()
    return len(stops)


async def dispatch(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    decision = OfficerDecision.model_validate(state["decision"])
    rec = state["recommendation"]
    c = _current_candidate(state)
    async with deps.sessionmaker() as session:
        row = await session.get(Recommendation, rec.recommendation_id)
        approval = await session.get(Approval, decision.approval_id)
    if row is None or approval is None:
        raise RuntimeError("approval or recommendation missing")
    if c.action_type == "do_nothing":  # the officer accepted "let the buses run": nothing to send
        return {"route": "resolved", "end_reason": "do_nothing_recommended"}
    if row.expires_at is not None and row.expires_at < approval.decided_at:
        # Approved too late: the situation may have moved on. Re-plan from fresh data.
        return _next_round(state, "replans_exhausted")

    # Re-validate against fresh MCP data right before sending.
    headways = await deps.tool(
        "dispatch", "get_headways", {"route_id": state["route_id"]}, Headways
    )
    pair = find_pair(headways, state["leader_vehicle_id"], state["follower_vehicle_id"])
    if pair is None or pair.status == "ok":
        return {"route": "resolved", "end_reason": "not_bunched"}

    params = (
        approval.edited_params
        if approval.decision == ApprovalDecision.EDITED and approval.edited_params
        else c.params
    )
    arguments = {
        "instruction_id": f"apr-{approval.id}",  # deterministic: a replay can't send twice
        "approval_id": str(approval.id),
        "vehicle_id": str(c.vehicle_id),
        "action": {"type": c.action_type, **params},
    }
    try:
        sent = await deps.tool("dispatch", "send_driver_instruction", arguments, InstructionSent)
    except (ToolFailed, HookRejected) as exc:
        if exc.code in REPLAN_CODES:
            log.info("dispatch_replan", code=exc.code, run_id=deps.run_id)
            return _next_round(state, "replans_exhausted")
        raise
    notified = await _notify_passengers(deps, c, sent)
    if deps.job_queue is not None:
        await deps.job_queue.enqueue_job(
            "driver_ack_timeout", deps.run_id, sent.instruction_id,
            _job_id=f"ack-timeout:{sent.instruction_id}", _defer_by=DRIVER_ACK_TIMEOUT_S,
        )  # fmt: skip
    return {
        "instruction": sent.model_dump(mode="json") | {"notified_stops": notified},
        "route": "sent",
    }


# ------------------------------------------------------------------ driver and outcome


async def await_driver(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    instruction = state["instruction"]
    value = interrupt({"kind": "driver", "instruction_id": instruction["instruction_id"]})
    reply = DriverReply.model_validate(value)
    out: dict[str, Any] = {"driver_reply": reply.model_dump(mode="json")}
    if reply.kind == "ack":
        return out | {"route": "ack", "acked_world_time_s": reply.world_time_s or 0.0}
    return out | _next_round(state, "replans_exhausted")


async def await_outcome(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    if deps.job_queue is not None:  # deterministic job id: re-running this node doesn't duplicate
        await deps.job_queue.enqueue_job(
            "measure_outcome", deps.run_id, state.get("acked_world_time_s", 0.0),
            _job_id=f"measure:{deps.run_id}", _defer_by=5,
        )  # fmt: skip
    OutcomeDue.model_validate(interrupt({"kind": "outcome", "run_id": deps.run_id}))
    return {"route": "measure"}


def observed_wait_minutes(
    samples: list[tuple[int | None, str, float, int]], start: float, end: float
) -> float:
    """Passenger-minutes spent waiting: the integral of counted queues over the world window."""
    by_stop: dict[tuple[int | None, str], list[tuple[float, int]]] = {}
    for d, stop_id, t, n in samples:
        if start <= t <= end:
            by_stop.setdefault((d, stop_id), []).append((t, n))
    total_s = 0.0
    for points in by_stop.values():
        points.sort()
        for (t0, n0), (t1, _) in itertools.pairwise(points):
            total_s += n0 * (t1 - t0)
    return round(total_s / 60, 1)


def headway_cv(headways: Headways, direction_id: int) -> float | None:
    values = [p.headway_seconds for p in headways.pairs if p.direction_id == direction_id]
    if len(values) < 2 or statistics.fmean(values) == 0:
        return None
    return round(statistics.stdev(values) / statistics.fmean(values), 3)


async def measure(state: WorkflowState, deps: WorkflowDeps) -> dict[str, Any]:
    start = state.get("acked_world_time_s", 0.0)
    end = start + OUTCOME_WINDOW_S
    async with deps.sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    StopQueue.direction_id,
                    StopQueue.stop_id,
                    StopQueue.sim_time_s,
                    StopQueue.waiting_count,
                ).where(
                    StopQueue.route_id == state["route_id"],
                    StopQueue.sim_time_s >= start,
                    StopQueue.sim_time_s <= end,
                )
            )
        ).all()
        instruction = await session.scalar(
            select(DriverInstruction).where(
                DriverInstruction.instruction_id == state["instruction"]["instruction_id"]
            )
        )
    samples = [(d, s, float(t), n) for d, s, t, n in rows if t is not None]
    observed = observed_wait_minutes(samples, start, end)
    rec = state["recommendation"]
    baseline = next((r for r in rec.ranking if r.candidate_key == "do_nothing"), None)
    predicted_baseline = baseline.passenger_wait_minutes if baseline else None
    actual_benefit = (
        round(predicted_baseline - observed, 1)
        if predicted_baseline is not None and samples
        else None
    )

    after = await deps.tool("dispatch", "get_headways", {"route_id": state["route_id"]}, Headways)
    pair = find_pair(after, state["leader_vehicle_id"], state["follower_vehicle_id"])
    snapshot = state["snapshot"]
    d = snapshot.pair.direction_id
    resolved = pair is None or pair.status in ("ok", "closing")
    if instruction is not None:
        async with deps.sessionmaker() as session:
            session.add(
                InterventionOutcome(
                    driver_instruction_id=instruction.id,
                    action_type=instruction.action_type,
                    headway_cv_before=headway_cv(snapshot.headways, d),
                    headway_cv_after=headway_cv(after, d),
                    bunching_resolved=resolved,
                    predicted_benefit_passenger_minutes=rec.benefit_passenger_minutes,
                    actual_benefit_passenger_minutes=actual_benefit,
                    metrics={
                        "observed_wait_minutes": observed,
                        "predicted_baseline_wait_minutes": predicted_baseline,
                        "window_world_s": [start, end],
                        "queue_samples": len(samples),
                        "pair_status_after": pair.status if pair else "not_adjacent",
                    },
                )
            )
            await session.commit()
    log.info(
        "outcome_measured", run_id=deps.run_id, predicted=rec.benefit_passenger_minutes,
        actual=actual_benefit, resolved=resolved,
    )  # fmt: skip
    return {"route": "done", "end_reason": "outcome_measured"}
