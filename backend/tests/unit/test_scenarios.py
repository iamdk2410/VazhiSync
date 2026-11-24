"""B5 without a real LLM: golden scenarios, plausibility checks, conversion to the simulator,
the generate/check/regenerate loop (fake model) and the Ollama client (mock transport)."""

import json
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from app.llm.ollama import LlmError, OllamaClient, model_name
from scenario_gen.checks import ScenarioContext, dropouts, problems, to_sim_config
from scenario_gen.director import LlmReply, generate, plan_schema
from scenario_gen.golden import load_golden
from scenario_gen.preview import preview
from scenario_gen.schema import Scenario, ScenarioPlan
from simulator import Breakdown, DemandSurge, Simulation, Slowdown, Weather
from tests.sim_fixtures import route_32b

CTX = ScenarioContext.for_route(route_32b())
GOLDEN = load_golden()

# Two valid stop ids per direction of 32B (D1 Police Station / Adham Market).
D0 = ["MTC-1091", "MTC-9547"]
D1 = ["MTC-1090", "MTC-9553"]


def _plan(**changes: Any) -> ScenarioPlan:
    base: dict[str, Any] = {
        "name": "Test",
        "summary": "A scenario for tests.",
        "start_time": "18:00",
        "duration_min": 60,
        "demand_multiplier": 1.0,
    }
    return ScenarioPlan.model_validate({**base, **changes})


# ------------------------------------------------------------------ golden


def test_there_are_five_or_six_golden_scenarios() -> None:
    assert 5 <= len(GOLDEN) <= 6
    assert len({g.key for g in GOLDEN}) == len(GOLDEN)


@pytest.mark.parametrize("golden", GOLDEN, ids=lambda g: g.key)
def test_golden_scenario_behaves_as_expected(golden) -> None:  # type: ignore[no-untyped-def]
    assert problems(golden.plan, CTX) == []
    assert golden.failures(preview(golden.scenario, CTX)) == []


def test_previews_are_deterministic() -> None:
    g = GOLDEN[0]
    assert preview(g.scenario, CTX) == preview(g.scenario, CTX)


# ------------------------------------------------------------------ plausibility


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"start_time": "23:30"}, "before midnight"),
        (
            {
                "weather": [
                    {
                        "kind": "heavy_rain",
                        "start_min": 0,
                        "end_min": 30,
                        "speed_factor": 0.95,
                        "demand_factor": 1,
                    }
                ]
            },
            "speed_factor 0.95 should be between 0.55 and 0.85",
        ),
        (
            {
                "weather": [
                    {
                        "kind": "light_rain",
                        "start_min": 20,
                        "end_min": 90,
                        "speed_factor": 0.9,
                        "demand_factor": 1,
                    }
                ]
            },
            "after the scenario ends",
        ),
        (
            {
                "surges": [
                    {
                        "direction_id": 0,
                        "stop_ids": ["NOPE"],
                        "start_min": 0,
                        "end_min": 30,
                        "multiplier": 2,
                        "reason": "x y z",
                    }
                ]
            },
            "NOPE is not a stop of direction 0",
        ),
        (
            {
                "surges": [
                    {
                        "direction_id": 0,
                        "stop_ids": D1[:1],
                        "start_min": 0,
                        "end_min": 30,
                        "multiplier": 2,
                        "reason": "wrong way",
                    }
                ]
            },
            "is not a stop of direction 0",
        ),
        (
            {
                "surges": [
                    {
                        "direction_id": 0,
                        "stop_ids": ["MTC-5916"],
                        "start_min": 0,
                        "end_min": 30,
                        "multiplier": 2,
                        "reason": "terminal",
                    }
                ]
            },
            "is the last stop",
        ),
        (
            {
                "slowdowns": [
                    {
                        "direction_id": 0,
                        "from_stop_id": D0[1],
                        "to_stop_id": D0[0],
                        "start_min": 0,
                        "end_min": 30,
                        "speed_factor": 0.5,
                        "reason": "backwards",
                    }
                ]
            },
            "must come before",
        ),
        (
            {"breakdowns": [{"bus_number": 30, "at_min": 5, "duration_min": 10}]},
            "only has buses 1-14",
        ),
        (
            {
                "breakdowns": [
                    {"bus_number": 2, "at_min": 5, "duration_min": 10},
                    {"bus_number": 2, "at_min": 30, "duration_min": 10},
                ]
            },
            "at most once",
        ),
        (
            {
                "gps_dropouts": [
                    {"bus_number": n, "at_min": 5, "duration_min": 5} for n in (1, 2, 3)
                ]
            },
            None,  # 3 of 14 buses is fine
        ),
        (
            {
                "demand_multiplier": 2.5,
                "surges": [
                    {
                        "direction_id": 0,
                        "stop_ids": D0,
                        "start_min": 0,
                        "end_min": 30,
                        "multiplier": 5,
                        "reason": "huge crowd",
                    }
                ],
            },
            "keep it at or below 8",
        ),
    ],
)
def test_plausibility_checks(changes: dict[str, Any], message: str | None) -> None:
    found = problems(_plan(**changes), CTX)
    if message is None:
        assert found == []
    else:
        assert any(message in p for p in found), found


def test_conversion_to_simulator_events() -> None:
    plan = _plan(
        weather=[
            {
                "kind": "heavy_rain",
                "start_min": 10,
                "end_min": 40,
                "speed_factor": 0.7,
                "demand_factor": 1.2,
            }
        ],
        surges=[
            {
                "direction_id": 1,
                "stop_ids": D1,
                "start_min": 15,
                "end_min": 45,
                "multiplier": 4,
                "reason": "match",
            }
        ],
        slowdowns=[
            {
                "direction_id": 0,
                "from_stop_id": "MTC-10797",
                "to_stop_id": "MTC-6795",
                "start_min": 0,
                "end_min": 30,
                "speed_factor": 0.5,
                "reason": "flood",
            }
        ],
        breakdowns=[{"bus_number": 3, "at_min": 20, "duration_min": 15}],
        gps_dropouts=[{"bus_number": 7, "at_min": 5, "duration_min": 10}],
        demand_multiplier=1.4,
    )
    cfg = to_sim_config(plan, CTX)
    t0 = 18 * 3600
    assert cfg.start_time_s == t0 and cfg.demand_multiplier == 1.4
    weather, surge, slowdown, breakdown = cfg.events
    assert weather == Weather(t0 + 600, t0 + 2400, 0.7, 1.2)
    assert isinstance(surge, DemandSurge) and surge.direction == 1 and surge.stop_indices == (6, 5)
    assert slowdown == Slowdown(0, 3, 5, t0, t0 + 1800, 0.5)
    assert breakdown == Breakdown("32B-03", t0 + 1200, 900)
    (dropout,) = dropouts(plan, CTX)
    assert (dropout.bus_id, dropout.start_s, dropout.duration_s) == ("32B-07", 300, 600)
    sim = Simulation.new(CTX.route, cfg, seed=1)  # loads into the simulator
    sim.step(60)
    assert sim.state.time_s == t0 + 60


# ------------------------------------------------------------------ director (fake model)


class FakeModel:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    async def chat(self, messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> LlmReply:
        self.calls.append((list(messages), schema))
        return LlmReply(self.replies.pop(0), "fake/model", 100, 50, 10)


GOOD = _plan(
    weather=[
        {
            "kind": "heavy_rain",
            "start_min": 0,
            "end_min": 60,
            "speed_factor": 0.7,
            "demand_factor": 1.2,
        }
    ],
    surges=[
        {
            "direction_id": 0,
            "stop_ids": D0,
            "start_min": 10,
            "end_min": 50,
            "multiplier": 4,
            "reason": "match crowd",
        }
    ],
).model_dump_json()
BAD_STOP = json.loads(GOOD) | {
    "surges": [
        {
            "direction_id": 0,
            "stop_ids": ["CHEPAUK"],
            "start_min": 10,
            "end_min": 50,
            "multiplier": 4,
            "reason": "match crowd",
        }
    ]
}


async def test_good_first_answer_becomes_a_scenario() -> None:
    model = FakeModel([GOOD])
    result = await generate("Monsoon evening, IPL at Chepauk", CTX, model, seed=42)
    assert isinstance(result.scenario, Scenario)
    assert result.scenario.seed == 42 and result.scenario.route_id == "32B"
    assert len(result.attempts) == 1
    messages, schema = model.calls[0]
    assert schema == plan_schema(CTX) and "seed" not in json.dumps(schema)
    surge_ids = schema["$defs"]["Surge"]["properties"]["stop_ids"]["items"]["enum"]
    assert "MTC-1091" in surge_ids and "D1PoliceStation" not in surge_ids  # real ids only
    assert schema["$defs"]["BreakdownPlan"]["properties"]["bus_number"]["maximum"] == 14
    assert "MTC-1091 = D1 Police Station" in messages[0]["content"]  # the route is in the prompt
    assert "33-35 lakh" in messages[0]["content"]  # ridership context
    assert messages[1]["content"].endswith("Monsoon evening, IPL at Chepauk")


async def test_problems_are_sent_back_for_a_regeneration() -> None:
    model = FakeModel([json.dumps(BAD_STOP), GOOD])
    result = await generate("IPL at Chepauk", CTX, model, seed=1)
    assert result.scenario is not None and len(result.attempts) == 2
    repair = model.calls[1][0][-1]["content"]
    assert "CHEPAUK is not a stop of direction 0" in repair
    assert model.calls[1][0][-2] == {"role": "assistant", "content": json.dumps(BAD_STOP)}


async def test_invalid_json_counts_as_a_problem() -> None:
    model = FakeModel(["{not json", '{"name": "x"}', GOOD])
    result = await generate("anything", CTX, model, seed=1)
    assert result.scenario is not None
    assert [bool(a.problems) for a in result.attempts] == [True, True, False]


async def test_gives_up_after_two_regenerations() -> None:
    model = FakeModel([json.dumps(BAD_STOP)] * 5)
    result = await generate("IPL at Chepauk", CTX, model, seed=1)
    assert result.scenario is None
    assert len(result.attempts) == 3 and len(model.replies) == 2  # never a 4th call
    assert result.problems


# ------------------------------------------------------------------ Ollama client


def _client(handler: Any, attempts: int = 3) -> OllamaClient:
    return OllamaClient(
        "http://ollama.test",
        "ollama/qwen3:8b",
        attempts=attempts,
        transport=httpx.MockTransport(handler),
    )


def _ok(content: str = "{}") -> httpx.Response:
    body = {
        "model": "qwen3:8b",
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": 120,
        "eval_count": 40,
        "done": True,
    }
    return httpx.Response(200, json=body)


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("app.llm.ollama.asyncio.sleep", instant)


async def test_ollama_request_disables_thinking_and_sends_the_schema() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok('{"a": 1}')

    reply = await _client(handler).chat([{"role": "user", "content": "hi"}], {"type": "object"})
    body = seen[0]
    assert body["think"] is False and body["stream"] is False
    assert body["model"] == "qwen3:8b" and body["format"] == {"type": "object"}
    assert (reply.content, reply.input_tokens, reply.output_tokens) == ('{"a": 1}', 120, 40)
    assert reply.model == "ollama/qwen3:8b"


async def test_ollama_retries_transient_errors_only() -> None:
    calls = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else _ok()

    await _client(flaky).chat([], {})
    assert len(calls) == 3

    calls.clear()

    def rejected(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="model not found")

    with pytest.raises(LlmError) as err:
        await _client(rejected).chat([], {})
    assert err.value.kind == "rejected" and len(calls) == 1  # never retried


async def test_ollama_timeouts_are_bounded() -> None:
    calls = []

    def slow(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LlmError) as err:
        await _client(slow, attempts=2).chat([], {})
    assert err.value.kind == "timeout" and len(calls) == 2


async def test_ollama_unexpected_body_is_a_bad_response() -> None:
    with pytest.raises(LlmError) as err:
        await _client(lambda r: httpx.Response(200, json={"hello": 1})).chat([], {})
    assert err.value.kind == "bad_response"


def test_model_name() -> None:
    assert model_name("ollama/qwen3:8b") == "qwen3:8b"
    assert model_name("qwen3:8b") == "qwen3:8b"
