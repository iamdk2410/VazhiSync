"""B9: the LLM gateway. Fallback chain, bounded retries, JSON repair, token budgets, logging.

Done when: a fake failure from the primary provider produces a successful answer from the
backup, with both attempts logged, and an exhausted token budget triggers the fallback.
The hosted tiers run the real OpenAI-compatible client over a mock HTTP transport.
"""

import asyncio
import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel, Field, SecretStr

from app.config import Settings
from app.db.models import LlmUsage
from app.harness.budget import RunBudget
from app.llm.gateway import (
    BACKUP,
    FALLBACK,
    LOCAL,
    PRIMARY,
    RULES,
    LlmGateway,
    Tier,
    extract_json,
    tiers_from_settings,
)
from app.llm.langfuse import LangfuseTracer
from app.llm.ollama import LlmError
from app.llm.openai_compat import OpenAICompatClient
from scenario_gen.director import LlmReply, Message


class Answer(BaseModel):
    action: str = Field(pattern=r"^(hold|skip_stop|do_nothing)$")
    seconds: int = Field(ge=0, le=180)


RULES_ANSWER = Answer(action="do_nothing", seconds=0)
MESSAGES: list[Message] = [
    {"role": "system", "content": "You suggest one action."},
    {"role": "user", "content": '{"headway_s": 120}'},
]


class Capture:
    """Stands in for the sessionmaker: collects the llm_usage rows the gateway writes."""

    def __init__(self) -> None:
        self.rows: list[LlmUsage] = []

    def __call__(self) -> "Capture":
        return self

    async def __aenter__(self) -> "Capture":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def add(self, row: LlmUsage) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        return None


def completion(content: str, prompt: int = 50, out: int = 10) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "served-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": out},
        },
    )


Handler = Callable[[httpx.Request], httpx.Response]


def hosted(level: int, model: str, handler: Handler, calls: list[str]) -> Tier:
    def record(request: httpx.Request) -> httpx.Response:
        calls.append(model)
        return handler(request)

    client = OpenAICompatClient(
        "groq", model, "test-key", attempts=2, transport=httpx.MockTransport(record)
    )
    return Tier(level, "groq", model, client)


def gateway(*tiers: Tier, capture: Capture | None = None, **kw: Any) -> LlmGateway:
    return LlmGateway(tiers, sessionmaker=cast(Any, capture), **kw)


def budget(run_tokens: int = 10_000, agent_tokens: int = 10_000) -> RunBudget:
    return RunBudget(
        run_id=uuid.uuid4(),
        time_limit_s=60,
        max_tool_calls_per_agent=10,
        max_steps=10,
        run_tokens=run_tokens,
        agent_tokens=agent_tokens,
    )


async def ask(gw: LlmGateway, b: RunBudget | None = None, **kw: Any) -> Any:
    return await gw.complete(
        agent="action_generator",
        messages=MESSAGES,
        output=Answer,
        rules=lambda: RULES_ANSWER,
        run_id=str(uuid.uuid4()),
        budget=b,
        **kw,
    )


# ------------------------------------------------------------------ Done when


async def test_primary_failure_is_answered_by_the_backup_and_both_attempts_are_logged() -> None:
    calls: list[str] = []
    capture = Capture()
    gw = gateway(
        hosted(PRIMARY, "primary-model", lambda r: httpx.Response(503), calls),
        hosted(
            BACKUP, "backup-model", lambda r: completion('{"action":"hold","seconds":90}'), calls
        ),
        capture=capture,
    )

    result = await ask(gw)

    assert result.value == Answer(action="hold", seconds=90)
    assert result.level == BACKUP and not result.from_rules
    assert calls == ["primary-model", "primary-model", "backup-model"]  # 5xx retried once
    rows = capture.rows
    assert [(r.fallback_level, r.success, r.error_kind) for r in rows] == [
        (PRIMARY, False, "unavailable"),
        (BACKUP, True, None),
    ]
    assert rows[1].input_tokens == 50 and rows[1].output_tokens == 10
    assert rows[1].model == "groq/served-model" and rows[1].agent == "action_generator"
    assert all(r.run_id is not None and r.provider == "groq" for r in rows)


async def test_an_exhausted_token_budget_falls_back_without_calling_any_model() -> None:
    calls: list[str] = []
    capture = Capture()
    gw = gateway(
        hosted(
            PRIMARY, "primary-model", lambda r: completion('{"action":"hold","seconds":60}'), calls
        ),
        capture=capture,
        max_output_tokens=800,
    )
    b = budget(run_tokens=500)  # less than one call's estimate (prompt + 800 output tokens)

    result = await ask(gw, b)

    assert result.from_rules and result.value == RULES_ANSWER
    assert calls == []  # the call was never made
    (row,) = capture.rows
    assert (row.fallback_level, row.provider, row.error_kind) == (RULES, "rules", "token_budget")


async def test_the_per_agent_budget_falls_back_once_that_agent_has_spent_it() -> None:
    calls: list[str] = []
    gw = gateway(
        hosted(
            PRIMARY, "m", lambda r: completion('{"action":"hold","seconds":60}', 600, 300), calls
        ),
        max_output_tokens=100,
    )
    b = budget(run_tokens=10_000, agent_tokens=1_200)

    first = await ask(gw, b)
    second = await ask(gw, b)  # 900 spent + ~110 estimated > 1200? no: 1010 fits
    third = await ask(gw, b)  # 1800 spent: over the agent's budget

    assert not first.from_rules and not second.from_rules
    assert third.from_rules and third.attempts[-1].error_kind == "token_budget"
    assert b.tokens_used["action_generator"] == 1800
    assert b.can_spend_tokens("control_advisor", 500)  # other agents keep their own share


# ------------------------------------------------------------------ retries and fallbacks


async def test_rate_limited_tier_is_not_retried_and_cools_down() -> None:
    calls: list[str] = []
    now = [0.0]
    gw = gateway(
        hosted(PRIMARY, "primary-model", lambda r: httpx.Response(429), calls),
        hosted(
            BACKUP, "backup-model", lambda r: completion('{"action":"hold","seconds":30}'), calls
        ),
        rate_limit_cooldown_s=60,
        clock=lambda: now[0],
    )

    assert (await ask(gw)).level == BACKUP
    assert calls == ["primary-model", "backup-model"]  # 429: straight to the next tier
    now[0] = 30.0
    assert (await ask(gw)).level == BACKUP
    assert calls[2:] == ["backup-model"]  # primary skipped while cooling down
    now[0] = 61.0
    await ask(gw)
    assert calls[3] == "primary-model"  # tried again after the cool-down


async def test_permanent_errors_are_not_retried() -> None:
    calls: list[str] = []
    gw = gateway(
        hosted(PRIMARY, "primary-model", lambda r: httpx.Response(401, text="bad key"), calls),
        hosted(
            BACKUP, "backup-model", lambda r: completion('{"action":"hold","seconds":30}'), calls
        ),
    )
    result = await ask(gw)
    assert calls == ["primary-model", "backup-model"]
    assert result.attempts[0].error_kind == "rejected"


async def test_timeouts_are_retried_a_bounded_number_of_times() -> None:
    calls: list[str] = []

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    gw = gateway(hosted(PRIMARY, "primary-model", slow, calls))
    result = await ask(gw)
    assert calls == ["primary-model", "primary-model"]  # LLM_ATTEMPTS = 2
    assert result.from_rules and result.attempts[0].error_kind == "timeout"


async def test_every_tier_failing_ends_with_the_rules() -> None:
    calls: list[str] = []
    capture = Capture()
    gw = gateway(
        hosted(PRIMARY, "a", lambda r: httpx.Response(500), calls),
        hosted(BACKUP, "b", lambda r: httpx.Response(429), calls),
        hosted(FALLBACK, "c", lambda r: httpx.Response(404), calls),
        capture=capture,
    )
    result = await ask(gw)
    assert result.from_rules and result.value == RULES_ANSWER
    assert [r.fallback_level for r in capture.rows] == [PRIMARY, BACKUP, FALLBACK, RULES]
    assert capture.rows[-1].error_kind == "all_tiers_failed"


async def test_no_configured_tier_uses_the_rules() -> None:
    result = await ask(gateway())
    assert result.from_rules and result.attempts[-1].error_kind == "no_tiers"


# ------------------------------------------------------------------ JSON repair


async def test_malformed_json_is_repaired_once_with_the_validation_errors() -> None:
    calls: list[str] = []
    bodies: list[dict[str, Any]] = []
    replies = iter(['{"action": "teleport", "seconds": 900}', '{"action":"hold","seconds":120}'])

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return completion(next(replies))

    capture = Capture()
    gw = gateway(hosted(PRIMARY, "m", handler, calls), capture=capture)
    result = await ask(gw)

    assert result.value == Answer(action="hold", seconds=120) and result.level == PRIMARY
    repair = bodies[1]["messages"]
    assert repair[-2]["role"] == "assistant" and "teleport" in repair[-2]["content"]
    assert "action" in repair[-1]["content"] and "seconds" in repair[-1]["content"]
    assert [(r.success, r.error_kind) for r in capture.rows] == [
        (False, "invalid_output"),
        (True, None),
    ]


async def test_a_reply_still_wrong_after_repair_moves_to_the_next_tier() -> None:
    calls: list[str] = []
    gw = gateway(
        hosted(PRIMARY, "a", lambda r: completion("Sure! I think holding is best."), calls),
        hosted(BACKUP, "b", lambda r: completion('{"action":"skip_stop","seconds":0}'), calls),
    )
    result = await ask(gw)
    assert calls == ["a", "a", "b"]
    assert result.level == BACKUP
    assert [a.error_kind for a in result.attempts] == [
        "invalid_output",
        "invalid_after_repair",
        None,
    ]


async def test_caller_checks_are_repaired_like_validation_errors() -> None:
    replies = iter(['{"action":"hold","seconds":45}', '{"action":"hold","seconds":60}'])
    calls: list[str] = []
    gw = gateway(hosted(PRIMARY, "m", lambda r: completion(next(replies)), calls))

    def multiple_of_30(a: Answer) -> list[str]:
        return [] if a.seconds % 30 == 0 else ["seconds: use a multiple of 30"]

    result = await ask(gw, check=multiple_of_30)
    assert result.value.seconds == 60 and len(calls) == 2


def test_json_is_found_inside_fences_or_prose() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Here you go: {"a": {"b": 2}} hope it helps') == '{"a": {"b": 2}}'
    assert extract_json("no json") == "no json"


# ------------------------------------------------------------------ local tier and config


class FakeLocal:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply:
        self.calls += 1
        if not self.content:
            raise LlmError("Ollama unreachable", "unavailable")
        return LlmReply(self.content, "ollama/qwen3:8b", 40, 8, 900)


async def test_hosted_tiers_down_fall_back_to_the_local_model() -> None:
    calls: list[str] = []
    local = FakeLocal('{"action":"hold","seconds":60}')
    gw = gateway(
        hosted(PRIMARY, "a", lambda r: httpx.Response(503), calls),
        Tier(LOCAL, "ollama", "qwen3:8b", local),
    )
    result = await ask(gw)
    assert result.level == LOCAL and local.calls == 1


def settings(**env: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "jwt_secret": SecretStr("x" * 32),
        "groq_api_key": None,
        "openrouter_api_key": None,
    }
    return Settings(_env_file=None, **(base | env))


def test_tiers_without_a_key_or_model_are_left_out() -> None:
    no_keys = tiers_from_settings(settings())
    assert [(t.level, t.provider) for t in no_keys] == [(LOCAL, "ollama")]

    groq_only = tiers_from_settings(settings(groq_api_key=SecretStr("k")))
    assert [(t.level, t.model) for t in groq_only] == [
        (PRIMARY, "openai/gpt-oss-120b"),
        (BACKUP, "qwen/qwen3.8-27b"),
        (LOCAL, "qwen3:8b"),
    ]

    everything = tiers_from_settings(
        settings(groq_api_key=SecretStr("k"), openrouter_api_key=SecretStr("k"), local_model="")
    )
    assert [(t.level, t.provider, t.model) for t in everything] == [
        (PRIMARY, "groq", "openai/gpt-oss-120b"),
        (BACKUP, "groq", "qwen/qwen3.8-27b"),
        (FALLBACK, "openrouter", "google/gemma-4-31b-it:free"),
    ]

    hosted_only = tiers_from_settings(settings(groq_api_key=SecretStr("k")), include_local=False)
    assert LOCAL not in [t.level for t in hosted_only]  # live runs: no slow local tier
    assert settings().llm_local_in_runs is False

    unknown = tiers_from_settings(settings(groq_api_key=SecretStr("k"), primary_model="acme/x"))
    assert [t.level for t in unknown] == [BACKUP, LOCAL]


async def test_openai_compatible_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "google/gemma-4-31b-it:free",
                "choices": [{"message": {"content": '{"a":1}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3, "cost": 0.0},
            },
        )

    client = OpenAICompatClient(
        "openrouter", "google/gemma-4-31b-it:free", "secret-key", max_tokens=321,
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    reply = await client.chat(MESSAGES, {"type": "object"})

    (request,) = seen
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-key"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 321 and body["usage"] == {"include": True}
    assert "reasoning_effort" not in body
    assert body["messages"][0]["role"] == "system"
    assert '"type":"object"' in body["messages"][0]["content"]  # schema in the prompt
    assert reply.model == "openrouter/google/gemma-4-31b-it:free"
    assert (reply.input_tokens, reply.output_tokens) == (12, 3)


# ------------------------------------------------------------------ Langfuse


async def test_langfuse_gets_a_generation_per_attempt_and_its_failures_are_harmless() -> None:
    posted: list[dict[str, Any]] = []

    def ingest(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(207)

    tracer = LangfuseTracer("http://langfuse", "pk", "sk", transport=httpx.MockTransport(ingest))
    calls: list[str] = []
    gw = gateway(
        hosted(PRIMARY, "a", lambda r: httpx.Response(503), calls),
        hosted(BACKUP, "b", lambda r: completion('{"action":"hold","seconds":30}'), calls),
        tracer=tracer,
    )
    await ask(gw)
    await tracer.flush()

    generations = [
        e["body"] for p in posted for e in p["batch"] if e["type"] == "generation-create"
    ]
    assert [g["level"] for g in generations] == ["ERROR", "DEFAULT"]
    assert generations[1]["usage"] == {"input": 50, "output": 10, "unit": "TOKENS"}

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no langfuse", request=request)

    broken = LangfuseTracer("http://langfuse", "pk", "sk", transport=httpx.MockTransport(down))
    gw2 = gateway(
        hosted(PRIMARY, "a", lambda r: completion('{"action":"hold","seconds":30}'), calls),
        tracer=broken,
    )
    assert (await ask(gw2)).level == PRIMARY  # tracing down never breaks a call
    await broken.flush()


def test_langfuse_is_off_with_placeholder_keys() -> None:
    assert LangfuseTracer.from_settings(settings()) is None
    off = settings(
        langfuse_public_key="pk-lf-change-me", langfuse_secret_key=SecretStr("sk-lf-change-me")
    )
    assert LangfuseTracer.from_settings(off) is None
    on = settings(langfuse_public_key="pk-lf-1", langfuse_secret_key=SecretStr("sk-lf-1"))
    assert LangfuseTracer.from_settings(on) is not None


@pytest.mark.parametrize("estimate", [0, 1])
def test_budget_bookkeeping(estimate: int) -> None:
    b = budget(run_tokens=100, agent_tokens=60)
    b.spend_tokens("a", 60)
    assert not b.can_spend_tokens("a", estimate or 1)
    assert b.can_spend_tokens("b", 40) and not b.can_spend_tokens("b", 41)


# ------------------------------------------------------------------ run time


class Slow:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply:
        self.calls += 1
        await asyncio.sleep(5)
        return LlmReply('{"action":"hold","seconds":30}', "ollama/qwen3:8b", 1, 1, 5000)


async def test_a_slow_model_is_cut_off_before_it_eats_the_run_time() -> None:
    now = [0.0]
    b = RunBudget(uuid.uuid4(), 47.3, 10, 10, run_tokens=10_000, agent_tokens=10_000,
                  clock=lambda: now[0])  # fmt: skip
    slow = Slow()
    gw = gateway(Tier(LOCAL, "ollama", "qwen3:8b", slow), time_reserve_s=47.0)

    result = await ask(gw, b)  # 0.3 s left for the call: not even started
    assert result.from_rules and result.attempts[-1].error_kind == "run_time"
    assert slow.calls == 0

    b2 = RunBudget(uuid.uuid4(), 49.5, 10, 10, run_tokens=10_000, agent_tokens=10_000)
    capture = Capture()
    gw2 = gateway(Tier(LOCAL, "ollama", "qwen3:8b", slow), time_reserve_s=47.0, capture=capture)
    result = await ask(gw2, b2)  # ~2.5 s allowed, the model needs 5 s
    assert result.from_rules and slow.calls == 1
    assert [r.error_kind for r in capture.rows] == ["timeout", "all_tiers_failed"]


def test_gpt_oss_gets_low_reasoning_effort() -> None:
    client = OpenAICompatClient("groq", "openai/gpt-oss-120b", "k")
    assert client._body(MESSAGES, {})["reasoning_effort"] == "low"
    assert "reasoning_effort" not in OpenAICompatClient("groq", "qwen/qwen3.8-27b", "k")._body(
        MESSAGES, {}
    )
