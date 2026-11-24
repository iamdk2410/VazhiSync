"""ARQ job: generate a scenario with the local LLM, check it, preview it, store it.

Every LLM attempt is logged to `llm_usage` (rule 9; local model, so cost 0). The job never
raises for expected failures (implausible output, LLM down): the row ends `failed` with the
reason, which the Lab page shows. ARQ itself does not retry (max_tries=1): the client already
retries transient errors and the director already regenerates (rules 7 and 8).
"""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.enums import ScenarioStatus
from app.db.models import LlmUsage, ScenarioRecord
from app.db.session import get_sessionmaker
from app.lab.library import scenario_context
from app.llm.ollama import LlmError, OllamaClient
from app.runner.route_loader import RouteNotImported
from scenario_gen.director import ChatModel, Generated, generate
from scenario_gen.preview import preview

log = structlog.get_logger()

AGENT = "scenario_director"
LOCAL_FALLBACK_LEVEL = 2  # 0 primary, 1 backup, 2 local, 3 rule-based (llm_usage)


def default_model() -> ChatModel:
    settings = get_settings()
    return OllamaClient(settings.ollama_url, settings.local_model)


def _usage(result: Generated) -> list[LlmUsage]:
    return [
        LlmUsage(
            run_id=None,
            agent=AGENT,
            provider="ollama",
            model=a.reply.model,
            fallback_level=LOCAL_FALLBACK_LEVEL,
            input_tokens=a.reply.input_tokens,
            output_tokens=a.reply.output_tokens,
            cost_usd=Decimal(0),
            latency_ms=a.reply.latency_ms,
            success=not a.problems,
            error_kind=None if not a.problems else "implausible_output",
        )
        for a in result.attempts
    ]


async def _finish(session: AsyncSession, record: ScenarioRecord, **values: Any) -> None:
    for key, value in values.items():
        setattr(record, key, value)
    record.completed_at = datetime.now(UTC)
    await session.commit()


async def run_generation(scenario_id: uuid.UUID, model: ChatModel | None = None) -> ScenarioStatus:
    model = model or default_model()
    async with get_sessionmaker()() as session:
        record = await session.get(ScenarioRecord, scenario_id)
        if record is None or record.status != ScenarioStatus.PENDING:
            log.warning("scenario_job_skipped", scenario_id=str(scenario_id))
            return record.status if record else ScenarioStatus.FAILED
        record.status = ScenarioStatus.GENERATING
        await session.commit()
        log.info("scenario_generating", scenario_id=str(scenario_id))
        try:
            ctx = await scenario_context(session, record.route_id)
            result = await generate(record.description, ctx, model, seed=record.seed)
        except RouteNotImported as exc:
            await _finish(session, record, status=ScenarioStatus.FAILED, error=str(exc))
            return ScenarioStatus.FAILED
        except LlmError as exc:
            session.add(
                LlmUsage(
                    run_id=None,
                    agent=AGENT,
                    provider="ollama",
                    model=get_settings().local_model,
                    fallback_level=LOCAL_FALLBACK_LEVEL,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=Decimal(0),
                    latency_ms=0,
                    success=False,
                    error_kind=exc.kind,
                )
            )
            await _finish(
                session,
                record,
                status=ScenarioStatus.FAILED,
                error=f"local LLM unavailable ({exc.kind}): {exc}",
            )
            log.warning("scenario_llm_unavailable", scenario_id=str(scenario_id), kind=exc.kind)
            return ScenarioStatus.FAILED

        session.add_all(_usage(result))
        if result.scenario is None:
            await _finish(
                session,
                record,
                status=ScenarioStatus.FAILED,
                attempts=len(result.attempts),
                problems=result.problems,
                error="the model did not produce a plausible scenario",
            )
            log.warning("scenario_rejected", scenario_id=str(scenario_id), problems=result.problems)
            return ScenarioStatus.FAILED
        await _finish(
            session,
            record,
            status=ScenarioStatus.READY,
            attempts=len(result.attempts),
            plan=result.scenario.plan.model_dump(mode="json"),
            preview=dataclasses.asdict(preview(result.scenario, ctx)),
            problems=None,
        )
        log.info(
            "scenario_ready",
            scenario_id=str(scenario_id),
            attempts=len(result.attempts),
            name=result.scenario.plan.name,
        )
        return ScenarioStatus.READY


async def generate_scenario(ctx: dict[str, Any], scenario_id: str) -> str:
    """ARQ entry point."""
    return (await run_generation(uuid.UUID(scenario_id))).value
