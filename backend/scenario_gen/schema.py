"""The Scenario schema: what the local LLM fills in, what golden files contain, what the
simulator and the live world load (rule 6: every scenario is validated here).

Times are minutes from the scenario's start, stops are GTFS stop ids, buses are numbers
(1 = "<route>-01"), so the model never has to do clock arithmetic or index counting.
Field bounds are hard limits; `scenario_gen.checks` adds the cross-field plausibility rules.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ClockTime = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
Minute = Annotated[int, Field(ge=0, le=240)]
StopId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")]
BusNumber = Annotated[int, Field(ge=1, le=40)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=160)]
WeatherKind = Literal["light_rain", "heavy_rain", "waterlogging"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WeatherWindow(_Strict):
    kind: WeatherKind
    start_min: Minute
    end_min: Minute
    speed_factor: Annotated[float, Field(ge=0.3, le=1.0)]  # 0.7 = traffic 30 % slower
    demand_factor: Annotated[float, Field(ge=0.5, le=2.0)]  # rain changes how many people ride


class Surge(_Strict):
    """Extra passengers at a few stops, e.g. a match ending at Chepauk."""

    direction_id: Literal[0, 1]
    stop_ids: Annotated[list[StopId], Field(min_length=1, max_length=4)]
    start_min: Minute
    end_min: Minute
    multiplier: Annotated[float, Field(ge=1.2, le=6.0)]
    reason: Reason


class Slowdown(_Strict):
    """Traffic on one stretch of one direction, between two of its stops."""

    direction_id: Literal[0, 1]
    from_stop_id: StopId
    to_stop_id: StopId
    start_min: Minute
    end_min: Minute
    speed_factor: Annotated[float, Field(ge=0.2, le=0.95)]
    reason: Reason


class BreakdownPlan(_Strict):
    bus_number: BusNumber
    at_min: Minute
    duration_min: Annotated[int, Field(ge=5, le=60)]


class GpsDropoutPlan(_Strict):
    bus_number: BusNumber
    at_min: Minute
    duration_min: Annotated[int, Field(ge=1, le=30)]


class ScenarioPlan(_Strict):
    """What the LLM produces (and golden files contain)."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=80)]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)]
    start_time: ClockTime
    duration_min: Annotated[int, Field(ge=30, le=180)]
    demand_multiplier: Annotated[float, Field(ge=0.3, le=3.0)]
    weather: Annotated[list[WeatherWindow], Field(max_length=3)] = []
    surges: Annotated[list[Surge], Field(max_length=4)] = []
    slowdowns: Annotated[list[Slowdown], Field(max_length=3)] = []
    breakdowns: Annotated[list[BreakdownPlan], Field(max_length=2)] = []
    gps_dropouts: Annotated[list[GpsDropoutPlan], Field(max_length=3)] = []

    @property
    def start_time_s(self) -> int:
        hours, minutes = self.start_time.split(":")
        return int(hours) * 3600 + int(minutes) * 60


class Scenario(_Strict):
    """A complete, loadable scenario: the plan plus the route and seed (set by code)."""

    route_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,32}$")]
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)]
    plan: ScenarioPlan
