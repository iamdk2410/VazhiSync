"""AgentSpecs: what each agent is for, what it may use and what it produces (docs/agents.md).

Allowed tools are the harness allowlists (app/harness/specs.py): the spec reads them so the
documentation and the enforced gate can never disagree.
"""

from dataclasses import dataclass

from app.harness.specs import AGENT_TOOLS, AgentName


@dataclass(frozen=True)
class AgentSpec:
    name: AgentName
    role: str
    objective: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    uses_llm: bool  # rules-only first (B10); LLM proposals/explanations arrive with B9

    @property
    def allowed_tools(self) -> frozenset[str]:
        return AGENT_TOOLS[self.name]


AGENTS: dict[AgentName, AgentSpec] = {
    s.name: s
    for s in (
        AgentSpec(
            name="headway_monitor",
            role="Checks that the data is fresh and that the reported pair is really bunched.",
            objective="Stop the workflow early unless acting is both possible and needed.",
            inputs=("bunching event (leader, follower)", "avl-mcp positions and headways"),
            outputs=("snapshot of observations", "route: stale | not_bunched | generate"),
            uses_llm=False,
        ),
        AgentSpec(
            name="action_generator",
            role="Proposes 2-4 concrete corrective actions plus 'do nothing'.",
            objective="Cover the sensible options; drop anything the rulebook forbids.",
            inputs=("snapshot", "boarding estimates (optional)", "rulebook", "past outcomes"),
            outputs=("candidates", "dropped candidates with rule reasons"),
            uses_llm=False,
        ),
        AgentSpec(
            name="impact_simulator",
            role="Simulates each candidate from the observations, in parallel.",
            objective="Measure passenger-minutes, bunching and people left behind per option.",
            inputs=("snapshot", "candidates"),
            outputs=("one simulation outcome per candidate (seeds averaged)",),
            uses_llm=False,
        ),
        AgentSpec(
            name="control_advisor",
            role="Ranks the simulated options and explains the best one.",
            objective="Recommend what saves the most passenger time; escalate near-ties.",
            inputs=("simulation outcomes", "candidates"),
            outputs=("ranked recommendation", "explanation", "route: recommend | escalate"),
            uses_llm=False,
        ),
        AgentSpec(
            name="dispatch",
            role="Sends the officer-approved instruction to the driver (B12).",
            objective="Deliver exactly what was approved, once, to a bus with fresh GPS.",
            inputs=("approval",),
            outputs=("driver instruction",),
            uses_llm=False,
        ),
    )
}
