"""B9: what the agents do with the LLM. Context builder, idea checks, the explanation check.

The LLM only adds candidate ideas (checked, then simulated like any other) and rewords the
explanation from the simulator's numbers; it never changes the ranking (rule 2).
"""

import json
import uuid
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.agents.action_generator import (
    MAX_ACTIONS,
    LlmIdea,
    LlmIdeas,
    idea_problems,
    ideas_to_candidates,
    merge,
    propose,
    skip_options,
    turn_options,
)
from app.agents.control_advisor import explain, explanation_context, rank
from app.agents.rulebook import violations
from app.graph.deps import WorkflowDeps
from app.harness.budget import RunBudget
from app.harness.context import (
    MAX_CONTEXT_CHARS,
    PastOutcome,
    StopOption,
    action_generator_messages,
    numbers_not_in,
)
from app.llm.gateway import LOCAL, LlmGateway, Tier
from scenario_gen.director import LlmReply, Message
from tests.unit.test_agents import (
    CANDS,
    NOISE,
    ROUTE,
    A,
    B,
    ctx,
    estimates,
    outcome,
    pair,
    snapshot,
)


class Scripted:
    """A model that answers from a list, recording what it was shown."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.seen: list[list[Message]] = []

    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply:
        self.seen.append(list(messages))
        return LlmReply(self.answers.pop(0), "ollama/qwen3:8b", 300, 60, 500)


def deps_with(model: Scripted | None, run_tokens: int = 10_000) -> WorkflowDeps:
    llm = None if model is None else LlmGateway([Tier(LOCAL, "ollama", "qwen3:8b", model)])
    budget = RunBudget(uuid.uuid4(), 60, 10, 10, run_tokens=run_tokens, agent_tokens=run_tokens)
    return cast(
        WorkflowDeps,
        SimpleNamespace(llm=llm, budget=budget, run_id=str(uuid.uuid4()), horizon_min=15.0),
    )


# ------------------------------------------------------------------ context builder


def test_action_generator_sees_a_compact_summary_and_its_memory_only() -> None:
    snap = snapshot(other_gap=1.8, estimates=estimates())
    skippable = skip_options(snap, ROUTE, snap.estimates)
    turns = turn_options(snap, ROUTE)
    messages = action_generator_messages(
        pair=snap.pair,
        leader=snap.positions.vehicles[0],
        follower=snap.positions.vehicles[1],
        leader_next_stop="Central",
        follower_next_stop="Park Town",
        skippable=skippable,
        turn_stops=turns,
        existing=["hold:32B-05:180"],
        rejected=["skip:32B-04:S1"],
        past=[PastOutcome(action_type="hold", interventions=4, avg_wait_change_minutes=1.5)],
        max_hold_s=180,
    )
    context = json.loads(messages[1]["content"])
    assert set(context) == {
        "pair", "leader", "follower", "skippable_stops_ahead_of_leader",
        "turn_stops_ahead_of_follower", "max_hold_s", "existing_options",
        "rejected_by_officer", "past_outcomes", "similar_past_cases",
    }  # fmt: skip
    assert context["leader"] == {
        "fleet_number": "32B-04", "next_stop": "Central", "load": 30, "gps_age_s": 2,
    }  # fmt: skip
    assert "lon" not in messages[1]["content"]  # no raw tool payloads
    assert context["rejected_by_officer"] == ["skip:32B-04:S1"]
    assert len(messages[1]["content"]) <= MAX_CONTEXT_CHARS
    assert 1 <= len(skippable) <= 6 and 1 <= len(turns) <= 3


def test_an_oversized_context_is_refused() -> None:
    huge = [StopOption(stop_id=f"S{i}", name="x" * 200) for i in range(60)]
    with pytest.raises(ValueError, match="context is"):
        action_generator_messages(
            pair=pair(), leader=None, follower=None, leader_next_stop=None,
            follower_next_stop=None, skippable=huge, turn_stops=[], existing=[], rejected=[],
            past=[], max_hold_s=180,
        )  # fmt: skip


def test_numbers_not_in_the_context_are_reported() -> None:
    messages: list[dict[str, str]] = [
        {"role": "user", "content": '{"saves": 312.4, "bus": "32B-05"}'}
    ]
    assert numbers_not_in("Saves about 312 passenger-minutes for 32B-05.", messages) == []
    assert numbers_not_in("Saves 312.4, so 40% fewer waits.", messages) == ["40"]


# ------------------------------------------------------------------ candidate ideas


def test_ideas_are_checked_against_the_stops_the_rules_allow() -> None:
    snap = snapshot(other_gap=1.8, estimates=estimates())
    skippable = skip_options(snap, ROUTE, snap.estimates)
    turns = turn_options(snap, ROUTE)
    bad = LlmIdeas(
        ideas=[
            LlmIdea(action_type="hold", hold_seconds=45, why="a bit"),
            LlmIdea(action_type="skip_stop", stop_ids=["NOPE"], why="quiet"),
            LlmIdea(action_type="short_turn", at_stop_id=skippable[0].stop_id + "x", why="gap"),
        ]
    )
    problems = idea_problems(bad, skippable, turns)
    assert len(problems) == 3
    assert "multiple of 30" in problems[0] and skippable[0].stop_id in problems[1]

    good = LlmIdeas(
        ideas=[
            LlmIdea(action_type="hold", hold_seconds=90, why="moderate"),
            LlmIdea(action_type="skip_stop", stop_ids=[skippable[-1].stop_id], why="one stop"),
            LlmIdea(action_type="short_turn", at_stop_id=turns[-1].stop_id, why="other gap"),
        ]
    )
    assert idea_problems(good, skippable, turns) == []
    hold, skip, turn = ideas_to_candidates(good, snap, skippable, turns)
    assert hold.key == "hold:32B-05:90" and hold.params == {"seconds": 90}
    assert skip.key == f"skip:32B-04:{skippable[-1].stop_id}" and skip.fleet_number == "32B-04"
    assert turn.params == {"at_stop_id": turns[-1].stop_id}
    assert {c.source for c in (hold, skip, turn)} == {"llm"}


def test_hard_limits_hold_for_llm_ideas_too() -> None:
    with pytest.raises(ValueError):
        LlmIdea(action_type="hold", hold_seconds=600, why="long")  # R1 in the schema itself
    snap = snapshot(estimates=estimates())
    skippable = [StopOption(stop_id="S9", name="General Hospital")]
    (skip,) = ideas_to_candidates(
        LlmIdeas(ideas=[LlmIdea(action_type="skip_stop", stop_ids=["S9"], why="quiet")]),
        snap, skippable, [],
    )  # fmt: skip
    rules = ctx(stop_names={"S9": "General Hospital"})
    assert any(v.startswith("R3") for v in violations(skip, rules, 0))  # dropped like any other


def test_merge_keeps_the_rules_best_adds_new_ideas_and_caps_the_count() -> None:
    snap = snapshot(other_gap=1.8, estimates=estimates())
    rule = propose(snap, ROUTE, snap.estimates)
    skippable = skip_options(snap, ROUTE, snap.estimates)
    ideas = ideas_to_candidates(
        LlmIdeas(
            ideas=[
                LlmIdea(action_type="hold", hold_seconds=180, why="same as rules"),
                LlmIdea(action_type="skip_stop", stop_ids=[skippable[0].stop_id], why="new"),
            ]
        ),
        snap, skippable, [],
    )  # fmt: skip
    merged = merge(rule, ideas)
    keys = [c.key for c in merged]
    assert keys[:2] == [c.key for c in rule[:2]]
    assert keys[2] == ideas[1].key  # the duplicate hold is not added twice
    assert keys.count("hold:32B-05:180") == 1
    assert len(merged) == MAX_ACTIONS + 1 and keys[-1] == "do_nothing"
    assert merge(rule, []) == rule


# ------------------------------------------------------------------ explanations


def ranked() -> Any:
    base = outcome("do_nothing", 1000, spread=NOISE)
    return rank(
        [base, outcome(A, 700, spread=[-30, -10, 0, 10, 30]), outcome(B, 900, spread=NOISE)],
        CANDS,
        15,
    )


async def test_the_llm_words_the_explanation_but_never_changes_the_ranking() -> None:
    result = ranked()
    model = Scripted(
        json.dumps({"text": "Hold 32B-05 for 120 s: it saves about 300 passenger-minutes over "
                    "the next 15 minutes, far more than the other option."})
    )  # fmt: skip
    explained = await explain(result, CANDS, deps_with(model))
    assert explained.explanation_source == "llm"
    assert explained.explanation.startswith("Hold 32B-05 for 120 s")
    assert explained.model_dump(exclude={"explanation", "explanation_source"}) == result.model_dump(
        exclude={"explanation", "explanation_source"}
    )
    shown = json.loads(model.seen[0][1]["content"])
    assert shown["ranking"][0]["passenger_minutes_saved_vs_doing_nothing"] == 300
    assert shown["ranking"][-1] == {
        "option": "Do nothing",
        "total_passenger_minutes_of_waiting": 1000,
    }


async def test_an_explanation_with_invented_numbers_is_repaired_then_replaced() -> None:
    result = ranked()
    invented = json.dumps({"text": "This saves 450 passenger-minutes and cuts waits by 37%."})
    model = Scripted(invented, invented)
    explained = await explain(result, CANDS, deps_with(model))
    assert explained.explanation_source == "rules"
    assert explained.explanation == result.explanation  # the template stays
    repair = model.seen[1][-1]["content"]
    assert "450" in repair and "37" in repair


async def test_no_gateway_or_no_budget_keeps_the_template() -> None:
    result = ranked()
    assert (await explain(result, CANDS, deps_with(None))) == result
    model = Scripted('{"text": "never used"}')
    assert (await explain(result, CANDS, deps_with(model, run_tokens=100))) == result
    assert model.seen == []


def test_explanation_context_quotes_the_simulator() -> None:
    messages = explanation_context(ranked(), CANDS, 15)
    shown = json.loads(messages[1]["content"])
    assert shown["recommended"] == "Hold 32B-05 for 120 s"
    assert shown["horizon_minutes"] == 15 and shown["near_tie"] is False
