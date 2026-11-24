"""Counterfactual runs: what happens over the next N minutes if we apply an action."""

from simulator.actions import Action
from simulator.engine import Simulation
from simulator.metrics import Metrics


def fast_forward(sim: Simulation, action: Action, minutes: float) -> Metrics:
    """Copy `sim`, apply `action`, run `minutes`, and return metrics for that window only.

    The original simulation is not modified. Raises ActionError if the action is invalid.
    """
    clone = sim.copy()
    clone.reset_metrics()
    clone.apply(action)
    clone.step(minutes * 60)
    return clone.metrics()
