"""Step and time budgets per run and per agent (rule 8: every workflow has limits).

One RunBudget per workflow run: a whole-run deadline, a cap on tool calls per agent and on
graph steps, and LLM token budgets for the whole run and for each agent (B9). Token budgets
never raise: the gateway asks first and uses the rules instead of a call that won't fit.
"""

import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import Settings
from app.harness.errors import BudgetExceeded


@dataclass
class RunBudget:
    run_id: uuid.UUID
    time_limit_s: float
    max_tool_calls_per_agent: int
    max_steps: int
    run_tokens: int = 0  # LLM token budget for the whole run (B9); 0 = no LLM calls
    agent_tokens: int = 0  # ... and for one agent within the run
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    tool_calls: Counter[str] = field(default_factory=Counter)
    steps: int = 0
    tokens_used: Counter[str] = field(default_factory=Counter)

    def __post_init__(self) -> None:
        self.started = self.clock()

    @classmethod
    def from_settings(cls, run_id: uuid.UUID, settings: Settings) -> "RunBudget":
        return cls(
            run_id=run_id,
            time_limit_s=settings.run_time_limit_seconds,
            max_tool_calls_per_agent=settings.agent_max_tool_calls,
            max_steps=settings.max_graph_steps,
            run_tokens=settings.llm_run_token_budget,
            agent_tokens=settings.llm_agent_token_budget,
        )

    @property
    def remaining_s(self) -> float:
        return self.time_limit_s - (self.clock() - self.started)

    def charge_tool_call(self, agent: str, tool: str) -> None:
        if self.remaining_s <= 0:
            raise BudgetExceeded(
                f"run {self.run_id} is past its {self.time_limit_s:.0f} s limit",
                tool=tool, code="run_time",
            )  # fmt: skip
        if self.tool_calls[agent] >= self.max_tool_calls_per_agent:
            raise BudgetExceeded(
                f"{agent} used its {self.max_tool_calls_per_agent} tool calls",
                tool=tool, code="agent_tool_calls",
            )  # fmt: skip
        self.tool_calls[agent] += 1

    def charge_step(self, node: str) -> None:
        """Called by the graph (B10) for every node it runs."""
        if self.steps >= self.max_steps:
            raise BudgetExceeded(f"run hit {self.max_steps} steps", tool=node, code="steps")
        if self.remaining_s <= 0:
            raise BudgetExceeded("run time limit reached", tool=node, code="run_time")
        self.steps += 1

    @property
    def run_tokens_used(self) -> int:
        return sum(self.tokens_used.values())

    def can_spend_tokens(self, agent: str, estimate: int) -> bool:
        """Would a call estimated at `estimate` tokens stay inside both budgets?"""
        return (
            self.run_tokens_used + estimate <= self.run_tokens
            and self.tokens_used[agent] + estimate <= self.agent_tokens
        )

    def spend_tokens(self, agent: str, tokens: int) -> None:
        self.tokens_used[agent] += max(0, tokens)
