"""Which agent step is running right now (B18), so tool calls and LLM calls can be linked to it.

`recorded()` (app/graph/deps.py) opens a step scope around every node. Each graph node (and
each parallel `simulate` branch) runs in its own asyncio task, so the context variable is
per step even when steps run concurrently. The step's trace observation id is derived from
(run id, step index), so a span for a tool call can name its parent before the step's own
span is sent (that happens when the step ends).
"""

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

_NAMESPACE = uuid.UUID("9b8f2d4e-3c1a-4f6e-9d2b-7a5c4e3f2b1a")


@dataclass(frozen=True)
class StepScope:
    run_id: str
    index: int
    node: str

    @property
    def observation_id(self) -> str:
        return observation_id(self.run_id, self.index)


_current: ContextVar[StepScope | None] = ContextVar("vazhisync_step", default=None)


def observation_id(run_id: str, index: int) -> str:
    """The Langfuse observation id of a run's agent step (deterministic)."""
    return str(uuid.uuid5(_NAMESPACE, f"{run_id}:step:{index}"))


def current_step() -> StepScope | None:
    return _current.get()


@contextlib.contextmanager
def step_scope(run_id: str, index: int, node: str) -> Iterator[StepScope]:
    scope = StepScope(run_id, index, node)
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
