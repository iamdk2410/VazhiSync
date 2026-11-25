"""The LLM gateway (B9): every model call in a workflow goes through `LlmGateway.complete`.

Fallback chain: primary -> backup -> fallback (hosted, free tiers) -> local Ollama -> the
caller's rules. A tier without a model or key is left out. Per attempt:
- per-call timeout; retries for timeouts / connection errors / 5xx inside the client (rule 7);
- 429 -> next tier at once, and that tier is skipped for a cool-down;
- a reply that isn't valid JSON for the output model (or fails the caller's checks) gets one
  repair attempt with the exact problems, then the next tier;
- token budgets per run and per agent: a call that would exceed them is not made; the rules
  answer instead;
- inside a workflow run, a call never eats the time the run still needs: it is cut off at the
  run's remaining time minus a reserve (simulations, dispatch), and skipped if none is left.
Every attempt, and every rules answer, is a row in `llm_usage` (rule 9) and, if configured, a
Langfuse generation. The LLM never decides: callers use it for candidate ideas and wording
only, and check everything it returns (rules 2 and 6).
"""

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx
import structlog
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import LlmUsage
from app.harness.budget import RunBudget
from app.llm.langfuse import LangfuseTracer, get_tracer
from app.llm.ollama import LlmError, OllamaClient
from app.llm.openai_compat import BASE_URLS, OpenAICompatClient
from app.observability.tracing import current_step
from scenario_gen.director import LlmReply, Message

log = structlog.get_logger()

PRIMARY, BACKUP, FALLBACK, LOCAL, RULES = 0, 1, 2, 3, 4  # llm_usage.fallback_level
LEVEL_NAMES = {PRIMARY: "primary", BACKUP: "backup", FALLBACK: "fallback", LOCAL: "local"}
CHARS_PER_TOKEN = 4  # rough estimate, used only to budget before a call
MAX_REPAIR_PROBLEMS = 8
MIN_CALL_SECONDS = 2.0  # less time than this left for a call: don't start it


class ChatClient(Protocol):
    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply: ...


@dataclass(frozen=True)
class Tier:
    level: int
    provider: str
    model: str
    client: ChatClient

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class AttemptLog:
    level: int
    model: str
    success: bool
    error_kind: str | None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LlmResult[T: BaseModel]:
    value: T
    level: int  # the tier that answered; RULES if the rules did
    model: str
    attempts: list[AttemptLog] = field(default_factory=list)

    @property
    def from_rules(self) -> bool:
        return self.level == RULES


def estimate_tokens(messages: Sequence[Message], max_output: int) -> int:
    return sum(len(m["content"]) for m in messages) // CHARS_PER_TOKEN + max_output


def extract_json(content: str) -> str:
    """Tolerate code fences or a preamble around the object; validation decides the rest."""
    start, end = content.find("{"), content.rfind("}")
    return content[start : end + 1] if 0 <= start < end else content


def _problems(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc']) or 'reply'}: {e['msg']}" for e in exc.errors()]


def split_model(spec: str) -> tuple[str, str]:
    """'groq/openai/gpt-oss-120b' -> ('groq', 'openai/gpt-oss-120b')."""
    provider, _, model = spec.partition("/")
    return provider, model


def _secret(value: Any) -> str:
    return str(value.get_secret_value()) if value is not None else ""


def tiers_from_settings(
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    include_local: bool = True,
) -> list[Tier]:
    keys = {
        "groq": _secret(settings.groq_api_key),
        "openrouter": _secret(settings.openrouter_api_key),
    }
    hosted = [
        (PRIMARY, settings.primary_model),
        (BACKUP, settings.backup_model),
        (FALLBACK, settings.fallback_model),
    ]
    tiers: list[Tier] = []
    for level, spec in hosted:
        provider, model = split_model(spec)
        if not model:
            continue
        if provider not in BASE_URLS:
            log.warning("llm_tier_unknown_provider", level=level, provider=provider)
            continue
        if not keys[provider]:
            log.info("llm_tier_without_key", level=level, provider=provider)
            continue
        client = OpenAICompatClient(
            provider,
            model,
            keys[provider],
            timeout_s=settings.llm_timeout_seconds,
            attempts=settings.llm_attempts,
            max_tokens=settings.llm_max_output_tokens,
            transport=transport,
        )
        tiers.append(Tier(level, provider, model, client))
    if include_local and settings.local_model:
        local = OllamaClient(
            settings.ollama_url,
            settings.local_model,
            timeout_s=settings.llm_timeout_seconds * 3,  # CPU/GPU on the host is slower
            attempts=settings.llm_attempts,
            max_tokens=settings.llm_max_output_tokens,
            transport=transport,
        )
        tiers.append(Tier(LOCAL, "ollama", local.model, local))
    return tiers


class LlmGateway:
    def __init__(
        self,
        tiers: Sequence[Tier],
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        tracer: LangfuseTracer | None = None,
        max_output_tokens: int = 800,
        rate_limit_cooldown_s: float = 60.0,
        time_reserve_s: float = 45.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.tiers = list(tiers)
        self._sessionmaker = sessionmaker
        self._tracer = tracer
        self._max_output = max_output_tokens
        self._cooldown_s = rate_limit_cooldown_s
        self._reserve_s = time_reserve_s
        self._clock = clock
        self._cooling_until: dict[int, float] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        include_local: bool = True,
    ) -> "LlmGateway":
        gateway = cls(
            tiers_from_settings(settings, transport, include_local=include_local),
            sessionmaker=sessionmaker,
            tracer=get_tracer(),
            max_output_tokens=settings.llm_max_output_tokens,
            rate_limit_cooldown_s=settings.llm_rate_limit_cooldown_seconds,
            time_reserve_s=settings.llm_time_reserve_seconds,
        )
        log.info("llm_gateway_ready", tiers=gateway.describe())
        return gateway

    def describe(self) -> list[str]:
        return [f"{LEVEL_NAMES[t.level]}={t.label}" for t in self.tiers]

    async def complete[T: BaseModel](
        self,
        *,
        agent: str,
        messages: Sequence[Message],
        output: type[T],
        rules: Callable[[], T],
        run_id: str | None = None,
        budget: RunBudget | None = None,
        check: Callable[[T], list[str]] | None = None,
    ) -> LlmResult[T]:
        """The first tier's valid answer, or the rules' answer. Never raises for model
        failures: the caller always gets something it can use."""
        base = [dict(m) for m in messages]
        schema = output.model_json_schema()
        attempts: list[AttemptLog] = []
        reason = "no_tiers"
        for tier in self.tiers:
            reason = "all_tiers_failed"
            if self._cooling_until.get(tier.level, 0.0) > self._clock():
                log.info("llm_tier_cooling", agent=agent, tier=tier.label)
                continue
            convo = base
            for repairing in (False, True):
                if not self._affordable(agent, convo, budget):
                    return await self._rules(agent, run_id, rules, attempts, "token_budget")
                time_left = self._time_left(budget)
                if time_left is not None and time_left < MIN_CALL_SECONDS:
                    return await self._rules(agent, run_id, rules, attempts, "run_time")
                started = datetime.now(UTC)
                try:
                    reply = await asyncio.wait_for(tier.client.chat(convo, schema), time_left)
                except TimeoutError:
                    exc = LlmError(f"{tier.label}: cut off at the run's time limit", "timeout")
                    attempts.append(AttemptLog(tier.level, tier.label, False, exc.kind))
                    await self._log(agent, run_id, tier, None, exc.kind, convo, started)
                    break  # next tier (which will find no time left -> rules)
                except LlmError as exc:
                    attempts.append(AttemptLog(tier.level, tier.label, False, exc.kind))
                    await self._log(agent, run_id, tier, None, exc.kind, convo, started)
                    if exc.kind == "rate_limited":
                        self._cooling_until[tier.level] = self._clock() + self._cooldown_s
                    break  # next tier
                if budget is not None:
                    budget.spend_tokens(agent, reply.input_tokens + reply.output_tokens)
                value, problems = self._validate(reply.content, output, check)
                kind = None
                if value is None:
                    kind = "invalid_after_repair" if repairing else "invalid_output"
                attempts.append(
                    AttemptLog(
                        tier.level,
                        tier.label,
                        value is not None,
                        kind,
                        reply.input_tokens,
                        reply.output_tokens,
                    )
                )
                await self._log(agent, run_id, tier, reply, kind, convo, started)
                if value is not None:
                    return LlmResult(value, tier.level, tier.label, attempts)
                if repairing:
                    break  # repaired once and still wrong: next tier
                convo = [*base, *_repair_turns(reply.content, problems)]
        return await self._rules(agent, run_id, rules, attempts, reason)

    def _time_left(self, budget: RunBudget | None) -> float | None:
        """Seconds a call may take inside a run (None: no run limit, the client's timeout)."""
        return None if budget is None else budget.remaining_s - self._reserve_s

    def _affordable(
        self, agent: str, messages: Sequence[Message], budget: RunBudget | None
    ) -> bool:
        if budget is None:
            return True
        return budget.can_spend_tokens(agent, estimate_tokens(messages, self._max_output))

    @staticmethod
    def _validate[T: BaseModel](
        content: str, output: type[T], check: Callable[[T], list[str]] | None
    ) -> tuple[T | None, list[str]]:
        try:
            value = output.model_validate_json(extract_json(content))
        except ValidationError as exc:
            return None, _problems(exc)
        problems = check(value) if check else []
        return (None, problems) if problems else (value, [])

    async def _rules[T: BaseModel](
        self,
        agent: str,
        run_id: str | None,
        rules: Callable[[], T],
        attempts: list[AttemptLog],
        reason: str,
    ) -> LlmResult[T]:
        log.info("llm_fell_back_to_rules", agent=agent, reason=reason, attempts=len(attempts))
        attempts.append(AttemptLog(RULES, "rules", True, reason))
        await self._write(
            LlmUsage(
                run_id=_uuid(run_id),
                agent=agent,
                provider="rules",
                model="rules",
                fallback_level=RULES,
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal(0),
                latency_ms=0,
                success=True,
                error_kind=reason,
                step_index=_step_index(run_id),
            )
        )
        return LlmResult(rules(), RULES, "rules", attempts)

    async def _log(
        self,
        agent: str,
        run_id: str | None,
        tier: Tier,
        reply: LlmReply | None,
        error_kind: str | None,
        messages: list[dict[str, str]],
        started: datetime,
    ) -> None:
        elapsed = round((datetime.now(UTC) - started).total_seconds() * 1000)
        latency = reply.latency_ms if reply else elapsed
        tokens_in = reply.input_tokens if reply else 0
        tokens_out = reply.output_tokens if reply else 0
        success = reply is not None and error_kind is None
        log.info(
            "llm_call",
            agent=agent,
            tier=LEVEL_NAMES.get(tier.level),
            model=tier.label,
            success=success,
            error_kind=error_kind,
            latency_ms=latency,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
        )
        await self._write(
            LlmUsage(
                run_id=_uuid(run_id),
                agent=agent,
                provider=tier.provider,
                model=reply.model if reply else tier.label,
                fallback_level=tier.level,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                cost_usd=Decimal(str(reply.cost_usd)) if reply else Decimal(0),
                latency_ms=latency,
                success=success,
                error_kind=error_kind,
                step_index=_step_index(run_id),
            )
        )
        if self._tracer is not None:
            self._tracer.generation(
                run_id=run_id,
                agent=agent,
                model=tier.label,
                level=tier.level,
                messages=messages,
                output=reply.content if reply else None,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                started=started,
                error=error_kind,
            )

    async def _write(self, row: LlmUsage) -> None:
        if self._sessionmaker is None:
            return
        async with self._sessionmaker() as session:
            session.add(row)
            await session.commit()


def _repair_turns(content: str, problems: list[str]) -> list[dict[str, str]]:
    listed = "\n- ".join(problems[:MAX_REPAIR_PROBLEMS])
    return [
        {"role": "assistant", "content": content[:4000]},
        {
            "role": "user",
            "content": f"That reply was not valid. Problems:\n- {listed}\n"
            "Reply again with only the corrected JSON object.",
        },
    ]


def _step_index(run_id: str | None) -> int | None:
    step = current_step()
    return step.index if step is not None and step.run_id == run_id else None


def _uuid(run_id: str | None) -> uuid.UUID | None:
    return uuid.UUID(run_id) if run_id else None
