"""Context builder (B9): exactly what each agent's LLM call sees, nothing else.

A compact summary of the state (the pair, the two buses, the options the rules allow) plus the
relevant memory (what past interventions measured, what the officer rejected). Never the raw
tool payloads, the database or the world simulator (rule 1). Numbers the LLM may quote come
only from here, and `numbers_not_in` checks its text against them (rule 2: the simulator's
figures, not the model's).
"""

import json
import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from mcp_servers.schemas import HeadwayPair, VehiclePosition

MAX_CONTEXT_CHARS = 6000  # keeps every call well inside the token budgets
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


class StopOption(BaseModel):
    """A stop an LLM idea may name: an upcoming stop the leader could skip, or a stop where
    the follower could short-turn."""

    model_config = ConfigDict(frozen=True)

    stop_id: str
    name: str
    expected_boardings: float | None = None


class PastOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_type: str
    interventions: int
    avg_wait_change_minutes: float  # positive = waits got shorter after the intervention


def _bus(v: VehiclePosition | None, next_stop: str | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {
        "fleet_number": v.fleet_number,
        "next_stop": next_stop,
        "load": v.load,
        "gps_age_s": round(v.age_seconds),
    }


def _compact(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(text) > MAX_CONTEXT_CHARS:
        raise ValueError(f"context is {len(text)} chars (max {MAX_CONTEXT_CHARS})")
    return text


ACTION_GENERATOR_SYSTEM = (
    "You help a bus-control officer on one Chennai MTC route. Two buses are bunched: the "
    "follower is too close behind the leader. Suggest up to 3 corrective actions that the "
    "existing rule-based options miss. Allowed actions: hold the follower (30-180 s, in 30 s "
    "steps), let the leader skip 1-2 of the listed skippable stops, or short-turn the follower "
    "at one of the listed turn stops. Use only stop ids from the lists. A simulator will test "
    "every idea; you do not need to estimate passenger impact. Never repeat a rejected or "
    "existing option. Similar past cases show what was done before and what it really saved."
)

CONTROL_ADVISOR_SYSTEM = (
    "You explain a simulator's recommendation to a bus-control officer in 2-3 short plain "
    "sentences. The ranking is final: do not change, question or re-rank it. Quote only numbers "
    "that appear in the data, with the meaning their field names give; do not compute new ones. "
    "Write numbers plainly, never in quotes. Mention the near-tie or the safer option if the "
    "data flags one."
)


def action_generator_messages(
    *,
    pair: HeadwayPair,
    leader: VehiclePosition | None,
    follower: VehiclePosition | None,
    leader_next_stop: str | None,
    follower_next_stop: str | None,
    skippable: list[StopOption],
    turn_stops: list[StopOption],
    existing: Iterable[str],
    rejected: Iterable[str],
    past: list[PastOutcome],
    max_hold_s: int,
    similar: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    context = {
        "pair": {
            "leader": pair.leader_fleet_number,
            "follower": pair.follower_fleet_number,
            "headway_s": round(pair.headway_seconds),
            "scheduled_headway_s": round(pair.scheduled_headway_seconds),
        },
        "leader": _bus(leader, leader_next_stop),
        "follower": _bus(follower, follower_next_stop),
        "skippable_stops_ahead_of_leader": [s.model_dump(exclude_none=True) for s in skippable],
        "turn_stops_ahead_of_follower": [s.model_dump(exclude_none=True) for s in turn_stops],
        "max_hold_s": max_hold_s,
        "existing_options": sorted(existing),
        "rejected_by_officer": sorted(rejected),
        "past_outcomes": [p.model_dump() for p in past],
        "similar_past_cases": [  # B22: the closest situations and what happened
            {
                "action": c["action_type"],
                "params": c["params"],
                "predicted_saving": round(c["predicted_benefit_passenger_minutes"]),
                "actual_saving": (
                    None
                    if c["actual_benefit_passenger_minutes"] is None
                    else round(c["actual_benefit_passenger_minutes"])
                ),
                "similarity_distance": c["distance"],
            }
            for c in (similar or [])
        ],
    }
    return [
        {"role": "system", "content": ACTION_GENERATOR_SYSTEM},
        {"role": "user", "content": _compact(context)},
    ]


def control_advisor_messages(
    *,
    recommended: str,
    ranking: list[dict[str, Any]],
    horizon_min: float,
    near_tie: bool,
    safest: str | None,
    note: str | None,
) -> list[dict[str, str]]:
    context = {
        "recommended": recommended,
        "horizon_minutes": round(horizon_min),
        "ranking": ranking,
        "near_tie": near_tie,
        "safest_option": safest,
        "officer_note": note,
    }
    return [
        {"role": "system", "content": CONTROL_ADVISOR_SYSTEM},
        {"role": "user", "content": _compact(context)},
    ]


def _numbers(text: str) -> set[float]:
    return {float(n) for n in _NUMBER.findall(text)}


def numbers_not_in(text: str, messages: list[dict[str, str]]) -> list[str]:
    """Numbers in `text` that the context never gave (rounded to whole numbers counts as
    given). An explanation that invents a figure is rejected and repaired, or replaced by
    the rules' wording."""
    given = _numbers(" ".join(m["content"] for m in messages))
    allowed = given | {float(round(n)) for n in given} | {float(int(n)) for n in given}
    return sorted(
        {n for n in _NUMBER.findall(text) if float(n) not in allowed},
        key=lambda n: float(n),
    )
