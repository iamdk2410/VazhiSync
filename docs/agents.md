# VazhiSync — Agents

The advisor is one LangGraph graph (`backend/app/graph/workflow.py`). Each agent has an
**AgentSpec** in code (`backend/app/agents/specs.py`); its allowed tools are the harness
allowlist (`backend/app/harness/specs.py`), so this table and the enforced gate can't disagree
(checked by `tests/unit/test_agents.py`). With the LLM gateway (B9) the Action Generator gets
up to 3 extra candidate ideas and the Control Advisor gets plain-language wording; both work on
rules alone when every model fails or the token budget is spent, and the simulator still
decides (rule 2).

```
START -> Headway Monitor --stale--------> END   (flagged; nobody instructed)
                         --not_bunched--> END   (event resolved)
                         --generate-----> Action Generator -> Impact Simulator
         Impact Simulator --Send, one branch per candidate--> simulate (parallel)
         simulate -> Control Advisor --do_nothing--> END
                                      --recommend | escalate--> await_officer (pause)
         await_officer --approve--> Dispatch (re-validate, send) --> await_driver (pause)
                       --edit--> re-simulate --> await_officer
                       --reject--> Action Generator (next round, max 2)
                       --expired--> Headway Monitor (fresh data)
         await_driver --ack--> await_outcome (pause, ~15 world-min) --> measure --> END
                      --can't comply / no ack--> Headway Monitor (re-plan)
```

Pauses are LangGraph interrupts checkpointed in Postgres; the API records the human's decision
and a worker resumes the run (B12).

**Watching a run (AG-UI, decision 10):** each worker segment is one AG-UI run on the thread
`run_id`, published while the graph runs and served at `GET /officer/runs/{run_id}/events`
(SSE; attach any time, `Last-Event-ID` to reconnect):

| Graph | AG-UI event |
|---|---|
| segment starts (event or resume) | RUN_STARTED (+ STATE_SNAPSHOT of the run view) |
| a node starts / finishes (`tasks` stream) | STEP_STARTED / STEP_FINISHED (`simulate:<candidate>` per branch) |
| a node's state update (`updates` stream) | STATE_DELTA (JSON Patch on the run view) |
| Control Advisor's checked explanation | TEXT_MESSAGE_START / CONTENT / END |
| pause for the officer | RUN_FINISHED with an interrupt outcome (id = recommendation) |
| pause for the driver / outcome, or the run ends | RUN_FINISHED (`result.waiting_for` when paused) |
| time limit, budget, tools down | RUN_ERROR (`code` = the run's end reason) |

The officer resolves the interrupt with `POST /officer/runs/{run_id}/agui` (AG-UI
`RunAgentInput.resume`: approve, edit with `seconds`, reject with `reason`).

| Agent | Role | Objective | Tools | Inputs | Outputs |
|---|---|---|---|---|---|
| Headway Monitor | Checks the data is fresh and the reported pair is really bunched | Stop early unless acting is possible and needed | `get_vehicle_positions`, `get_headways` | Bunching event (leader, follower) | Snapshot of observations; route `stale` / `not_bunched` / `generate` |
| Action Generator | Proposes 2–4 concrete actions + "do nothing" | Cover the sensible options; drop what the rulebook forbids | `get_headways`, `get_boarding_estimates` | Snapshot, demand estimates (optional), rulebook, past outcomes | Candidates; dropped candidates with rule reasons |
| Impact Simulator | Simulates every candidate from the observations, in parallel | Measure passenger-minutes, bunching, people left behind | `get_vehicle_positions`, `get_headways`, `get_boarding_estimates` | Snapshot, candidates | One outcome per candidate (3 seeds, common random numbers) |
| Control Advisor | Ranks the outcomes and explains the best | Recommend what saves the most passenger time; escalate near-ties | none | Outcomes, candidates | Ranked recommendation, explanation; route `recommend` / `escalate` / `do_nothing` |
| Dispatch (B12) | Sends the approved instruction | Deliver exactly what was approved, once, to a bus with fresh GPS | `send_driver_instruction` (the only caller, rule 5) | Approval | Driver instruction |
| Early Warning (B21) | Looks ahead every 30 s with "do nothing" from the observations | Start the workflow before a bunch forms, while a small fix still works | `get_vehicle_positions`, `get_headways`, `get_boarding_estimates` | MCP observations | Predicted bunching event (pair, lead time) |

## How the agents decide (rules)

- **Early Warning (B21):** builds the what-if model from the same observations as the Impact
  Simulator and steps "do nothing" 15 minutes over 3 seeds, checking pair headways with the
  live detector's rule. A pair that is fresh and ok/closing now but bunches in at least 2 seeds
  raises a predicted event (not while an event is open for the pair, nor within 5 minutes of
  the last prediction). The workflow then runs as usual, except that the Headway Monitor and
  dispatch's last check accept the pair before it has bunched.

- **Headway Monitor:** finds the event's pair in `get_headways`. Stale pair → end
  (`stale_data`, event stays open and is retried). Pair missing (overtaken, terminal) or no
  longer `bunched` → end (`not_bunched`, event resolved).
- **Action Generator:** hold the follower at its next stop for the headway deficit (30 s steps,
  capped by R1) and for half of it; let the leader skip its two quietest upcoming stops (needs
  demand data — skipped when demand-mcp is down, the run notes it as degraded); short-turn the
  follower when the opposite direction has a gap ≥ 1.5× scheduled. Candidates are checked
  against the rulebook (R1–R6) and against past measured outcomes (an action type that made
  waits worse in ≥ 3 interventions is dropped). LLM ideas (B9): the model sees the context
  builder's summary (pair, the two buses, the skippable and turn stops the rules allow,
  existing and rejected options, past outcomes) and may suggest up to 3 actions; ideas naming
  other stops are repaired or dropped, the rest become canonical candidates checked exactly
  like the rule ones (rules' best two first, at most 4 actions). Past cases (B22): the three
  nearest past interventions in feature space go into the LLM context and onto the officer's
  card; a hold that saved passenger time in a similar case is offered again.
- **Impact Simulator:** builds the what-if model once with `from_observations` from the tool
  results only (positions, loads, staleness, waiting counts, world clock — rule 1), then runs
  each candidate for 15 minutes over the same 3 seeds, in parallel branches with a
  per-simulation timeout and a concurrency semaphore. Timed-out or invalid runs are reported,
  not hidden.
- **Control Advisor:** benefit = do-nothing passenger-minutes − candidate's. R7 rejects
  candidates that add bunching. Nothing positive → "do nothing" wins. Top two within
  max(20 passenger-minutes, 10 %) → escalated to the officer. No usable baseline → escalated.
  The explanation is a template; with the gateway an LLM rewords it from the top options'
  simulated figures, and any number not in those figures rejects the wording (template kept).

## Limits and logging

Graph step budget (`MAX_GRAPH_STEPS`), whole-run time limit (`RUN_TIME_LIMIT_SECONDS`),
per-simulation timeout (`SIM_TIMEOUT_SECONDS`), concurrency (`SIM_CONCURRENCY`), tool-call cap
per agent (`AGENT_MAX_TOOL_CALLS`), LLM token budgets per run and per agent
(`LLM_RUN_TOKEN_BUDGET`, `LLM_AGENT_TOKEN_BUDGET`) and an LLM time cap (run time left minus
`LLM_TIME_RESERVE_SECONDS`). Every LLM attempt is an `llm_usage` row. One active run per bus pair. Every node writes an
`agent_steps` row (input/output summary, routing decision, duration, error) and every log line
carries the `run_id`.
