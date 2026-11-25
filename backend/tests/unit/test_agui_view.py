"""AG-UI run stream, without Redis or the graph: patches, the view, the publisher's events."""

import json
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.agents.control_advisor import rank
from app.agui import patch
from app.agui.publisher import RunEventPublisher, officer_interrupt
from app.agui.stream import candidate_key, sse
from app.agui.view import RunView, fold, step_name
from app.graph.state import DroppedCandidate
from app.officer.decisions import OfficerDecisionPayload
from tests.unit.test_agents import CANDS, NOISE, A, B, outcome, snapshot

RUN_ID = "11111111-1111-1111-1111-111111111111"


# ------------------------------------------------------------------ JSON Patch


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({"a": 1}, {"a": 2, "b": [1]}),
        ({"a": [1, 2, 3]}, {"a": [1, 5]}),
        ({"a": [{"x": 1}]}, {"a": [{"x": 1, "y": 2}, {"x": 3}]}),
        ({"k/with~slash": 1, "gone": True}, {"k/with~slash": None}),
        ({"a": 1}, {"a": 1.0}),  # int -> float is a change
        ({"a": None}, {"a": {"nested": [1, {"b": "c"}]}}),
    ],
)
def test_diff_then_apply_reproduces_the_new_document(old: Any, new: Any) -> None:
    ops = patch.diff(old, new)
    assert patch.apply(old, ops) == new
    assert patch.diff(new, new) == []


# ------------------------------------------------------------------ the view


def test_fold_projects_each_node_update() -> None:
    v = RunView(run_id=RUN_ID)
    v = fold(v, "headway_monitor", {"snapshot": snapshot(), "route": "generate"})
    assert v.pair is not None and (v.pair.leader, v.pair.follower) == ("32B-04", "32B-05")
    dropped = [DroppedCandidate(key="skip:32B-04:X", reasons=["R3: never skip X"])]
    v = fold(v, "action_generator", {"candidates": CANDS, "dropped": dropped, "degraded": ["d"]})
    assert [c.key for c in v.candidates] == [c.key for c in CANDS] and v.degraded == ["d"]
    v = fold(v, "simulate", {"sim_outcomes": [outcome(A, 700)]})
    v = fold(v, "simulate", {"sim_outcomes": [outcome("do_nothing", 1000)]})
    assert set(v.simulations) == {A, "do_nothing"}
    rec = rank([outcome("do_nothing", 1000, spread=NOISE), outcome(A, 700, spread=NOISE),
                outcome(B, 900, spread=NOISE)], CANDS, 15)  # fmt: skip
    v = fold(v, "control_advisor", {"recommendation": rec, "end_reason": rec.end_reason})
    assert v.recommendation is not None and v.recommendation.candidate_key == A
    assert v.end_reason == "recommended"
    # A new round (officer rejected) starts the simulations and recommendation afresh.
    v2 = fold(v, "await_officer", {"round": 1, "decision": {"decision": "reject"}})
    assert v2.round == 1 and v2.simulations == {} and v2.recommendation is None
    assert v2.decision == {"decision": "reject"}
    assert v.recommendation is not None  # fold is pure


def test_simulate_branches_get_their_own_step_names() -> None:
    assert step_name("simulate", {"candidate": CANDS[0]}) == f"simulate:{CANDS[0].key}"
    assert step_name("control_advisor", {"candidate": CANDS[0]}) == "control_advisor"


def test_candidate_keys_rebuilt_from_the_database_match_the_workflows() -> None:
    assert candidate_key("hold", "32B-05", {"seconds": 120}, "rule") == "hold:32B-05:120"
    assert candidate_key("skip_stop", "32B-04", {"stop_ids": ["S1", "S2"]}, "llm") == (
        "skip:32B-04:S1+S2"
    )
    assert candidate_key("short_turn", "32B-05", {"at_stop_id": "S9"}, "rule") == (
        "short_turn:32B-05:S9"
    )
    assert candidate_key("hold", "32B-05", {"seconds": 90}, "officer_edit") == (
        "edit:32B-05:seconds=90"
    )
    assert candidate_key("do_nothing", None, {}, "rule") == "do_nothing"


# ------------------------------------------------------------------ the publisher


class FakeRedis:
    """Runs the publisher's Lua script semantics in memory: append events, record the view."""

    def __init__(self, fail: bool = False) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []
        self.hash: dict[str, str] = {}
        self.fail = fail

    async def eval(self, script: str, numkeys: int, *args: str) -> str:
        if self.fail:
            raise RedisConnectionError("redis is down")
        _, _, ttl, maxlen, view, meta, n, *events = args
        assert int(ttl) > 0 and int(maxlen) > 0 and int(n) == len(events)
        for e in events:
            self.entries.append((f"{len(self.entries) + 1}-0", json.loads(e)))
        self.hash = {"view": view, "meta": meta, "last_id": self.entries[-1][0]}
        return self.entries[-1][0]

    async def hget(self, key: str, field: str) -> str | None:
        if self.fail:
            raise RedisConnectionError("redis is down")
        return self.hash.get(field)

    def types(self) -> list[str]:
        return [e["type"] for _, e in self.entries]


def task_start(task_id: str, name: str, task_input: Any = None) -> dict[str, Any]:
    return {"id": task_id, "name": name, "input": task_input or {}, "triggers": ("x",)}


def task_end(task_id: str, name: str, result: Any = None, interrupts: Any = ()) -> dict[str, Any]:
    return {"id": task_id, "name": name, "error": None, "result": result or {},
            "interrupts": list(interrupts)}  # fmt: skip


async def a_paused_segment(redis: FakeRedis) -> tuple[RunEventPublisher, Any]:
    pub = RunEventPublisher(redis, RUN_ID, chunk_delay_s=0)  # type: ignore[arg-type]
    rec = rank([outcome("do_nothing", 1000, spread=NOISE), outcome(A, 700, spread=NOISE),
                outcome(B, 900, spread=NOISE)], CANDS, 15)  # fmt: skip
    rec = rec.model_copy(update={"recommendation_id": "22222222-2222-2222-2222-222222222222"})
    await pub.start()
    await pub.on_chunk("tasks", task_start("t1", "headway_monitor"))
    await pub.on_chunk(
        "updates", {"headway_monitor": {"snapshot": snapshot(), "route": "generate"}}
    )
    await pub.on_chunk("tasks", task_end("t1", "headway_monitor", {"route": "generate"}))
    await pub.on_chunk("updates", {"action_generator": {"candidates": CANDS, "dropped": []}})
    for i, c in enumerate(CANDS):
        await pub.on_chunk("tasks", task_start(f"s{i}", "simulate", {"candidate": c}))
        await pub.on_chunk("updates", {"simulate": {"sim_outcomes": [outcome(c.key, 800)]}})
        await pub.on_chunk("tasks", task_end(f"s{i}", "simulate"))
    await pub.on_chunk("updates", {"control_advisor": {"recommendation": rec,
                                                        "end_reason": rec.end_reason}})  # fmt: skip
    await pub.on_chunk("tasks", task_start("o", "await_officer"))
    await pub.on_chunk("updates", {"__interrupt__": ("ignored",)})
    await pub.on_chunk("tasks", task_end("o", "await_officer", interrupts=[{"value": {}}]))
    await pub.finish("officer", rec.end_reason)
    return pub, rec


async def test_a_segment_is_one_ag_ui_run_ending_in_the_approval_interrupt() -> None:
    redis = FakeRedis()
    pub, rec = await a_paused_segment(redis)
    types = redis.types()
    assert types[:2] == ["RUN_STARTED", "STATE_SNAPSHOT"] and types[-1] == "RUN_FINISHED"
    started = [e["stepName"] for _, e in redis.entries if e["type"] == "STEP_STARTED"]
    assert started == ["headway_monitor", *[f"simulate:{c.key}" for c in CANDS], "await_officer"]
    assert types.count("STEP_FINISHED") == len(started)
    run_started = redis.entries[0][1]
    assert run_started["threadId"] == RUN_ID and run_started["runId"] == pub.segment_id

    # The explanation is streamed as one text message, after the number check (it's final).
    text = "".join(e["delta"] for _, e in redis.entries if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == rec.explanation
    assert types.index("TEXT_MESSAGE_START") < types.index("TEXT_MESSAGE_END")

    # The approval pause is an AG-UI interrupt the officer resolves.
    finished = redis.entries[-1][1]
    (interrupt,) = finished["outcome"]["interrupts"]
    assert finished["outcome"]["type"] == "interrupt"
    assert interrupt["id"] == "22222222-2222-2222-2222-222222222222"
    assert interrupt["reason"] == "officer_approval" and interrupt["message"] == rec.explanation
    assert interrupt["responseSchema"] == OfficerDecisionPayload.model_json_schema()


async def test_snapshot_plus_every_delta_equals_the_recorded_view() -> None:
    redis = FakeRedis()
    pub, _ = await a_paused_segment(redis)
    doc = redis.entries[1][1]["snapshot"]
    for _, e in redis.entries[2:]:
        if e["type"] == "STATE_DELTA":
            doc = patch.apply(doc, e["delta"])
    assert doc == pub.view.doc() == json.loads(redis.hash["view"])
    assert doc["status"] == "paused" and doc["waiting_for"] == "officer"
    assert [s["status"] for s in doc["steps"]][-1] == "paused"
    meta = json.loads(redis.hash["meta"])
    assert meta["segment_state"] == "finished" and json.loads(meta["finish"])["type"] == (
        "RUN_FINISHED"
    )


async def test_a_resumed_segment_continues_the_same_thread_and_view() -> None:
    redis = FakeRedis()
    first, _ = await a_paused_segment(redis)
    second = RunEventPublisher(redis, RUN_ID, chunk_delay_s=0)  # type: ignore[arg-type]
    await second.start()
    assert second.segment_id != first.segment_id
    assert second.view.candidates == first.view.candidates  # rehydrated from the hash
    assert second.view.status == "running" and second.view.waiting_for is None
    await second.finish("driver", None)
    last = redis.entries[-1][1]
    assert last["result"] == {"status": "paused", "waiting_for": "driver"}  # not an interrupt
    assert "outcome" not in last


async def test_failures_end_the_segment_with_run_error() -> None:
    redis = FakeRedis()
    pub = RunEventPublisher(redis, RUN_ID, chunk_delay_s=0)  # type: ignore[arg-type]
    await pub.start()
    await pub.error("run_time_limit", "the run hit its time limit")
    assert redis.entries[-1][1] == {
        "type": "RUN_ERROR", "message": "the run hit its time limit", "code": "run_time_limit",
    }  # fmt: skip
    assert pub.view.status == "failed"


async def test_redis_down_never_breaks_the_run() -> None:
    redis = FakeRedis(fail=True)
    pub, _ = await a_paused_segment(redis)
    assert pub.failures > 0 and pub.view.waiting_for == "officer"  # carried on regardless


def test_no_interrupt_without_a_stored_recommendation() -> None:
    assert officer_interrupt(RunView(run_id=RUN_ID)) is None


def test_sse_frames() -> None:
    assert sse('{"type":"X"}', "5-0") == 'id: 5-0\ndata: {"type":"X"}\n\n'
    assert sse("{}") == "data: {}\n\n"
