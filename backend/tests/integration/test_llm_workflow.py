"""B9 in the workflow: LLM ideas and explanations go through the gateway, every attempt lands
in `llm_usage` with the run id, and a spent token budget leaves the run on its rules.

The primary tier is the real Groq-style client over a mock transport that always fails (503);
the backup is a scripted model that answers from the context it is shown, as a real one would.
"""

import json
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
import pytest
from arq import ArqRedis
from arq.worker import Worker
from sqlalchemy import select

from app.config import get_settings
from app.db.enums import CandidateSource
from app.db.models import CandidateAction, LlmUsage, Recommendation
from app.db.session import get_sessionmaker
from app.graph.workflow import start_workflow
from app.llm.gateway import BACKUP, PRIMARY, RULES, LlmGateway, Tier
from app.llm.openai_compat import OpenAICompatClient
from app.runner.world import WorldRunner
from scenario_gen.director import LlmReply, Message
from tests.integration.test_workflow import bunched, clock, harness, only_event, redis, state_of

pytestmark = pytest.mark.services
__all__ = ["bunched", "clock", "redis"]  # fixtures used by name


class ContextReader:
    """Answers like a sensible model: one new idea from the listed stops, and an explanation
    that quotes the recommended option's figures."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat(self, messages: Sequence[Message], schema: dict[str, Any]) -> LlmReply:
        context = json.loads(messages[1]["content"])
        self.calls.append(schema["title"])
        if schema["title"] == "LlmIdeas":
            skippable = context["skippable_stops_ahead_of_leader"]
            idea: dict[str, Any] = (
                {"action_type": "skip_stop", "stop_ids": [skippable[0]["stop_id"]],
                 "why": "One quiet stop is enough to pull ahead."}
                if skippable
                else {"action_type": "hold", "hold_seconds": 150, "why": "A middle-length hold."}
            )  # fmt: skip
            content = json.dumps({"ideas": [idea]})
        else:
            saved = context["ranking"][0]["passenger_minutes_saved_vs_doing_nothing"]
            content = json.dumps(
                {
                    "text": f"{context['recommended']}: about {saved} "
                    f"passenger-minutes saved over the next {context['horizon_minutes']} minutes."
                }
            )
        return LlmReply(content, "test/context-reader", 400, 60, 50)


def failing_primary() -> Tier:
    client = OpenAICompatClient(
        "groq", "openai/gpt-oss-120b", "test-key", attempts=1,
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )  # fmt: skip
    return Tier(PRIMARY, "groq", "openai/gpt-oss-120b", client)


async def usage(run_id: uuid.UUID) -> list[LlmUsage]:
    async with get_sessionmaker()() as session:
        return list(
            await session.scalars(
                select(LlmUsage).where(LlmUsage.run_id == run_id).order_by(LlmUsage.id)
            )
        )


async def test_llm_ideas_and_explanation_flow_through_the_gateway_with_fallback(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
) -> None:
    settings = get_settings()
    backup = ContextReader()
    llm = LlmGateway(
        [failing_primary(), Tier(BACKUP, "test", "context-reader", backup)],
        sessionmaker=get_sessionmaker(),
    )
    event = await only_event()

    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer, llm
    )

    assert result.run_id is not None
    state = state_of(result)
    llm_candidates = [c for c in state["candidates"] if c.source == "llm"]
    assert len(llm_candidates) == 1  # the idea was checked, kept and simulated
    assert llm_candidates[0].key in [o.candidate_key for o in state["sim_outcomes"]]

    rows = await usage(result.run_id)
    by_agent = [(r.agent, r.fallback_level, r.success) for r in rows]
    assert by_agent[:2] == [
        ("action_generator", PRIMARY, False),  # the fake 503 from the primary...
        ("action_generator", BACKUP, True),  # ...answered by the backup, both logged
    ]
    async with get_sessionmaker()() as session:
        stored = (
            await session.scalars(
                select(CandidateAction).where(
                    CandidateAction.run_id == result.run_id,
                    CandidateAction.source == CandidateSource.LLM,
                )
            )
        ).all()
        rec = await session.scalar(
            select(Recommendation).where(Recommendation.run_id == result.run_id)
        )
    assert len(stored) == 1

    recommendation = state["recommendation"]
    if result.end_reason in {"recommended", "escalated"}:
        assert by_agent[2:] == [
            ("control_advisor", PRIMARY, False),
            ("control_advisor", BACKUP, True),
        ]
        assert recommendation.explanation_source == "llm"
        assert "passenger-minutes saved" in recommendation.explanation
        assert rec is not None and rec.explanation == recommendation.explanation
    else:  # do nothing won: no explanation call
        assert len(rows) == 2
    assert backup.calls[0] == "LlmIdeas"


async def test_a_spent_token_budget_leaves_the_run_on_its_rules(
    bunched: tuple[WorldRunner, list[uuid.UUID]],
    sim_worker: Worker,
    job_queue: ArqRedis,
    checkpointer: Any,
) -> None:
    settings = get_settings().model_copy(update={"llm_run_token_budget": 0})
    backup = ContextReader()
    llm = LlmGateway(
        [Tier(BACKUP, "test", "context-reader", backup)], sessionmaker=get_sessionmaker()
    )
    event = await only_event()

    result = await start_workflow(
        event.id, harness(settings), get_sessionmaker(), settings, job_queue, checkpointer, llm
    )

    assert result.run_id is not None and result.end_reason in {
        "recommended", "escalated", "do_nothing_recommended",
    }  # fmt: skip
    state = state_of(result)
    assert all(c.source == "rule" for c in state["candidates"])
    assert state["recommendation"].explanation_source == "rules"
    assert backup.calls == []  # no model was called
    rows = await usage(result.run_id)
    assert rows and all(
        (r.fallback_level, r.provider, r.error_kind) == (RULES, "rules", "token_budget")
        for r in rows
    )
