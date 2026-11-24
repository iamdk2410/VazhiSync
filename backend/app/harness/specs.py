"""Which tools exist, where they live, what they return, and which agent may call which.

Least privilege (rule 5): only the post-approval dispatch step may call send_driver_instruction.
B10 extends these into full AgentSpecs (role, objective, inputs, outputs) in docs/agents.md.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from mcp_servers.schemas import BoardingEstimates, Headways, InstructionSent, VehiclePositions

ServerName = Literal["avl", "demand"]
ToolName = Literal[
    "get_vehicle_positions", "get_headways", "get_boarding_estimates", "send_driver_instruction"
]
AgentName = Literal[
    "headway_monitor", "action_generator", "impact_simulator", "control_advisor", "dispatch"
]


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    server: ServerName
    output: type[BaseModel]  # every result is validated against this (post-hook, rule 6)
    read_only: bool


TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in (
        ToolSpec("get_vehicle_positions", "avl", VehiclePositions, read_only=True),
        ToolSpec("get_headways", "avl", Headways, read_only=True),
        ToolSpec("get_boarding_estimates", "demand", BoardingEstimates, read_only=True),
        ToolSpec("send_driver_instruction", "demand", InstructionSent, read_only=False),
    )
}

# The tool gate: an agent may call only the tools listed for it.
AGENT_TOOLS: dict[AgentName, frozenset[str]] = {
    "headway_monitor": frozenset({"get_vehicle_positions", "get_headways"}),
    "action_generator": frozenset({"get_headways", "get_boarding_estimates"}),
    "impact_simulator": frozenset(
        {"get_vehicle_positions", "get_headways", "get_boarding_estimates"}
    ),
    "control_advisor": frozenset(),  # ranks simulator results; needs no tools
    # rule 5: the only caller of send; reads headways to re-validate right before sending and
    # to measure the outcome afterwards (B12).
    "dispatch": frozenset({"send_driver_instruction", "get_headways"}),
}
