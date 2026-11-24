"""Golden scenarios: hand-written, committed, with known expected outcomes.

Each file in `scenario_gen/golden/` holds a plan, a seed and expectations about how the
scenario compares with a normal day (same start time and seed), checked by the simulator.
They are the regression suite for the simulator and, later, the evaluation set (B19).
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from scenario_gen.preview import Preview, PreviewMetrics
from scenario_gen.schema import Scenario, ScenarioPlan

GOLDEN_DIR = Path(__file__).parent / "golden"

Metric = Literal[
    "avg_wait_s",
    "passenger_wait_minutes",
    "boarded",
    "left_behind",
    "bunching_count",
    "first_bunching_min",
    "headway_cv",
]


class Expectation(BaseModel):
    """`relation` compares the scenario with the normal day; `between` bounds the scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: Metric
    relation: Literal["higher", "lower", "same"] | None = None
    min_ratio: Annotated[float, Field(gt=0)] | None = None  # higher: >= x, lower: <= x
    between: tuple[float, float] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> "Expectation":
        if (self.relation is None) == (self.between is None):
            raise ValueError("give either relation or between")
        return self

    def failure(self, preview: Preview) -> str | None:
        base = getattr(preview.baseline, self.metric)
        value = getattr(preview.scenario, self.metric)
        label = f"{self.metric}: scenario {value} vs normal {base}"
        if self.between is not None:
            lo, hi = self.between
            return None if value is not None and lo <= value <= hi else f"{label}, want {lo}-{hi}"
        if value is None or base is None:
            return None if value == base else f"{label}, want {self.relation}"
        if self.relation == "same":
            return None if value == base else f"{label}, want the same"
        if self.relation == "higher":
            ok = value > base and (self.min_ratio is None or value >= base * self.min_ratio)
        else:
            ok = value < base and (self.min_ratio is None or value <= base * self.min_ratio)
        ratio = f" x{self.min_ratio}" if self.min_ratio else ""
        return None if ok else f"{label}, want {self.relation}{ratio}"


class GoldenScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{3,64}$")]
    description: Annotated[str, StringConstraints(min_length=10, max_length=500)]
    route_id: str
    seed: int
    plan: ScenarioPlan
    expected: Annotated[list[Expectation], Field(min_length=1)]

    @property
    def scenario(self) -> Scenario:
        return Scenario(route_id=self.route_id, seed=self.seed, plan=self.plan)

    def failures(self, preview: Preview) -> list[str]:
        return [f for e in self.expected if (f := e.failure(preview)) is not None]


def load_golden(directory: Path = GOLDEN_DIR) -> list[GoldenScenario]:
    scenarios = [
        GoldenScenario.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]
    for s, path in zip(scenarios, sorted(directory.glob("*.json")), strict=True):
        if s.key != path.stem:
            raise ValueError(f"{path.name}: key {s.key!r} must match the file name")
    return scenarios


__all__ = ["GOLDEN_DIR", "Expectation", "GoldenScenario", "PreviewMetrics", "load_golden"]
