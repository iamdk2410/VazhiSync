"""Generate a scenario from plain English: LLM proposes, code checks, up to 2 regenerations.

The model is injected (any object with an async `chat(messages, schema)`), so tests use a fake
and the app passes the Ollama client. Transport errors are the client's job (bounded retries,
rule 7); here a malformed or implausible answer is fed back once or twice with the exact
problems, never retried blindly.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from scenario_gen.checks import ScenarioContext, problems
from scenario_gen.prompt import repair_prompt, system_prompt, user_prompt
from scenario_gen.schema import Scenario, ScenarioPlan

MAX_REGENERATIONS = 2
Message = dict[str, str]


@dataclass(frozen=True)
class LlmReply:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float = 0.0  # as reported by the provider (free tiers and local: 0)


class ChatModel(Protocol):
    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply: ...


@dataclass(frozen=True)
class Attempt:
    number: int  # 1 = first try, 2-3 = regenerations
    reply: LlmReply
    problems: list[str]


@dataclass
class Generated:
    scenario: Scenario | None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        return self.attempts[-1].problems if self.attempts else []


def plan_schema(ctx: ScenarioContext) -> dict[str, Any]:
    """JSON schema for structured output, narrowed to this route.

    Ollama constrains generation to the schema, so listing the route's real stop ids as an
    enum (and capping bus numbers at the fleet size) makes invented ids impossible: an 8B
    model otherwise writes names like "D1PoliceStation". Direction and order are still
    checked in code. The LLM never sets the route or the seed.
    """
    schema = ScenarioPlan.model_json_schema()
    stop_ids = sorted({s.stop_id for d in ctx.route.directions for s in d.stops})
    defs = schema["$defs"]
    defs["Surge"]["properties"]["stop_ids"]["items"] = {"type": "string", "enum": stop_ids}
    for key in ("from_stop_id", "to_stop_id"):
        defs["Slowdown"]["properties"][key] = {"type": "string", "enum": stop_ids}
    for plan in ("BreakdownPlan", "GpsDropoutPlan"):
        defs[plan]["properties"]["bus_number"]["maximum"] = ctx.fleet_size
    return schema


def _validate(content: str, ctx: ScenarioContext) -> tuple[ScenarioPlan | None, list[str]]:
    try:
        plan = ScenarioPlan.model_validate_json(content)
    except ValidationError as exc:
        found = [
            f"{'.'.join(str(p) for p in err['loc']) or 'response'}: {err['msg']}"
            for err in exc.errors(include_url=False)
        ]
        return None, found[:10]
    return plan, problems(plan, ctx)


async def generate(
    description: str,
    ctx: ScenarioContext,
    model: ChatModel,
    seed: int,
    max_regenerations: int = MAX_REGENERATIONS,
) -> Generated:
    messages: list[Message] = [
        {"role": "system", "content": system_prompt(ctx)},
        {"role": "user", "content": user_prompt(description)},
    ]
    schema = plan_schema(ctx)
    result = Generated(scenario=None)
    for number in range(1, max_regenerations + 2):
        reply = await model.chat(messages, schema)
        plan, found = _validate(reply.content, ctx)
        result.attempts.append(Attempt(number=number, reply=reply, problems=found))
        if plan is not None and not found:
            result.scenario = Scenario(route_id=ctx.route.route_id, seed=seed, plan=plan)
            return result
        messages += [
            {"role": "assistant", "content": reply.content},
            {"role": "user", "content": repair_prompt(found)},
        ]
    return result
