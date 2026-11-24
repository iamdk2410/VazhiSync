"""The corrective actions the advisor can simulate and recommend."""

from dataclasses import dataclass


class ActionError(ValueError):
    """The action is not valid for the bus's current position or state."""


@dataclass(frozen=True)
class DoNothing:
    kind: str = "do_nothing"


@dataclass(frozen=True)
class Hold:
    """Keep the bus at its current/next stop for extra seconds (boarding continues)."""

    bus_id: str
    seconds: int
    kind: str = "hold"

    def __post_init__(self) -> None:
        if not 0 < self.seconds <= 600:
            raise ActionError("hold must be between 1 and 600 seconds")


@dataclass(frozen=True)
class SkipStops:
    """Run past these upcoming stops (current direction) without stopping.

    Passengers bound for a skipped stop ride on and get off at the next served stop.
    """

    bus_id: str
    stop_indices: tuple[int, ...]
    kind: str = "skip_stop"

    def __post_init__(self) -> None:
        if not self.stop_indices:
            raise ActionError("skip_stop needs at least one stop")
        if len(self.stop_indices) > 5:
            raise ActionError("skip at most 5 stops at once")


@dataclass(frozen=True)
class ShortTurn:
    """End the trip early at `at_stop` and turn back into the opposite direction.

    Passengers travelling beyond `at_stop` must get off there (counted as displaced).
    """

    bus_id: str
    at_stop: int
    kind: str = "short_turn"


Action = DoNothing | Hold | SkipStops | ShortTurn
