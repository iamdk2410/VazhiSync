# VazhiSync — Build Modules Plan

VazhiSync is built as 24 build modules (B1–B24) across 8 phases, in the order below. Each module lists its goal, what to build, and a "done when" check that tells you when to move on.

Sizes are relative: S = small, M = medium, L = large.

**Build order**
- **Backend:** B1 → B3 → B2 → B4 → B6 → B5 → B7 → B8 → B10 (rules only) → B11 → B12 → B9 → B21 → B22 → B17 → B18 → B19
- **Frontend:** B13 → B14 → B15 → B16 → B23
- **Last:** B24 (optional) → B20

Design decisions and the reasons behind them: `docs/decisions.md`.

---

## Phase A: Foundation

### B1. Project setup and infrastructure (S)

**Goal:** one command starts everything.

- Repo layout: `backend/` (with `simulator/`, `scenario_gen/`, `mcp_servers/`, `app/`, `tests/`), `frontend/`, `docs/` — see the CLAUDE.md files
- Docker Compose (repo root) with Postgres + PostGIS, Redis, Ollama, Langfuse, plus a service each for api, worker, avl-mcp, demand-mcp and sim-runner
- `.env` config (API keys, model names, thresholds such as the stale-GPS limit and the bunching ratio) loaded with `pydantic-settings`
- Alembic for database migrations
- A basic CI job: lint (ruff), type check (mypy) and tests on every push

**Done when:** `docker compose up` starts all services and they pass health checks.

> **Follow-up:** the `postgis/postgis` image does not include pgvector; the database image change (and CI service image) is part of **B22**.

### B2. Static data: Chennai GTFS import (S)

**Goal:** one real Chennai route in the database.

- Download the Chennai unified GTFS
- Pick one busy trunk route (15–25 stops, frequent service)
- `import_gtfs.py` loads the route, its stops (PostGIS points), its shape (linestring) and the scheduled headway
- Validate that the stops are in order, the shape passes near each stop, and the headway is sensible

**Done when:** `GET /routes/{id}` returns the stops and shape as GeoJSON, and they line up with the roads when drawn on a map.

**Geometry decision (as built):** the Chennai GTFS has no bus shapes, so route geometry comes from a one-time OSRM road snap through the stops of each direction, committed as GeoJSON (`data/gtfs/routes/<id>.dir<N>.geojson`) with OpenStreetMap attribution (ODbL). Each segment is checked against its straight-line distance; segments with a road/straight ratio > 2.5 are flagged and repaired (re-snapped as a stop pair, or drawn straight), and the repairs are recorded in `patched_segments`. Imports and tests never call the network; only the `snap_shape` tool does, by hand.

> **Follow-up:** per-stop-pair snapping, 3–4 short-turn connection paths, per-segment road distance/duration, optional `waypoints.json` and `geometry_source` per segment were agreed later and moved to **B6** ("Route geometry follow-up").

### B3. Database schema, login and roles (M)

**Goal:** every table and every role in place.

- All tables: `users`, `driver_assignments`, `vehicles`, `vehicle_positions`, `stop_queues`, `bunching_events`, `workflow_runs`, `candidate_actions`, `simulation_jobs`, `recommendations`, `approvals`, `driver_instructions` (unique idempotency key), `intervention_outcomes`, `notifications`, `favourite_stops`, `agent_steps`, `llm_usage`
- JWT login with the role inside the token, and argon2 password hashing
- Seed script for officers and drivers; public `POST /auth/register` that always creates a passenger
- `require_role()` dependency on every endpoint, and rate limiting on login and register

**Done when:** each role logs in and gets a 403 on another role's endpoints, and registering cannot create an officer.

> **Follow-up:** schema additions agreed later go into new migrations, not into B3: predicted benefit on `intervention_outcomes`, `expires_at` on `recommendations` and a "can't comply" instruction status + reason → **B12**; a "predicted" bunching-event kind → **B21**; the pgvector extension and `cases` table → **B22**.

---

## Phase B: The simulated world

### B4. Simulation engine (L), the heart of the system

**Goal:** realistic buses where bunching emerges by itself.

- Core models: `Bus` (position along route, load, state), `Stop` (queue), `Passenger` (counted, not individual objects)
- Passenger arrivals per stop: random (Poisson) with a time-of-day profile
- Dwell rule: `10s + 2.5s × boarders + 1.5s × alighters`, plus capacity limits and passengers left behind
- Travel time per segment with random noise and event modifiers (rain, slowdowns)
- Ways to apply the three actions: hold, skip stop, short-turn
- `step(dt)`, `snapshot()`, `from_observations(...)` and `fast_forward(state, action, minutes)`
- Metrics: total passenger wait, headway variation, bunching count, people left behind
- Deterministic with a seed

**Done when:** with no control applied, a seeded run reliably bunches within 15–30 simulated minutes, and the same seed always gives the same result.

### B5. Scenario generator with a local LLM (M)

**Goal:** realistic Chennai scenarios from a plain-English description.

- Pydantic `Scenario` schema (demand multiplier, weather, surges, slowdowns, GPS dropouts, breakdowns)
- Ollama client with structured output tied to that schema; the local model is `qwen3:8b`, with thinking mode disabled so it returns plain structured JSON
- A prompt that includes the route's stops, the GTFS headway and real MTC ridership figures
- Plausibility checks in code, with up to 2 regenerations
- A scenario library saved as JSON (plan plus seed)
- 5–6 hand-written "golden" scenarios with known expected outcomes
- `POST /lab/scenarios` for the Simulation Lab page

**Done when:** "Monsoon evening, IPL at Chepauk" produces a valid scenario that loads into the simulator and changes how it behaves.

### B6. Live world runner and streaming (M)

**Goal:** the simulated world runs continuously and streams to the app.

- An async loop that ticks the world simulator every 1–5 s (with adjustable speed: 1×, 5×, 10×)
- Writes "what GPS would report" to `vehicle_positions` and queues to `stop_queues`, honouring GPS dropouts
- Publishes updates to Redis
- FastAPI WebSocket with role-based channels: the officer gets everything, a driver gets only their own bus, passengers get public data
- Detects bunching and raises an event that starts the workflow
- **Route geometry follow-up (from B2):** one-time OSRM snap per stop pair in both directions plus 3–4 short-turn connection paths, cached as committed GeoJSON with OSM attribution; per-segment road distance and duration saved for the simulator; segments with road/straight ratio > 2.5 flagged; optional `waypoints.json` to force a road; `geometry_source` recorded per segment (osrm / osrm-pair / waypoints / straight); imports and tests still never call the network

**Done when:** buses visibly move on a test map in real time, and dropout buses stop updating.

---

## Phase C: Tools (MCP)

### B7. MCP servers (M)

**Goal:** four tools across two FastMCP servers.

- `avl-mcp`: `get_vehicle_positions` (with `age_seconds` and `is_stale` computed by the server) and `get_headways` (status and data quality per pair), plus a `route://{id}/shape` resource
- `demand-mcp`: `get_boarding_estimates` and `send_driver_instruction`
- The send tool enforces its own guards: approval required, idempotency key, stale check at send time, valid action
- Tool annotations (`readOnlyHint`, `idempotentHint`), Pydantic input/output schemas, structured errors, health endpoints

**Done when:** both servers show up in MCP Inspector with their schemas, every tool can be called, and the send tool rejects unapproved, duplicate and stale requests.

### B8. MCP client and agentic harness (part 1) (M)

**Goal:** agents discover and call tools safely, and every call goes through one harness.

- An MCP client connected to both servers, with tool discovery logged at startup (the official
  SDK client; `langchain-mcp-adapters` doesn't support MCP SDK v2, see `docs/decisions.md`)
- `app/harness/` with a **tool gate**: per-agent tool allowlists (least privilege); a call to a tool not on the agent's list is refused and logged
- **Pre-tool hooks:** block `send_driver_instruction` without a valid approval or for a stale bus — this deliberately duplicates the MCP server's own guards (defence in depth)
- **Post-tool hooks:** validate every tool result against its Pydantic model
- **Budgets:** step and time limits per run and per agent
- Timeouts, retries with backoff for temporary errors only, and marking a tool "unavailable" when its server is down

**Done when:** killing `demand-mcp` produces a logged "tool unavailable" and the system falls back gracefully instead of crashing, and the gate/pre-hook refuses an unapproved or stale send before it reaches the server.

---

## Phase D: The brain

### B9. LLM gateway and agentic harness (part 2) (M)

**Goal:** one place where every model call goes through, with bounded tokens and controlled context.

- LiteLLM (or your own wrapper) with the fallback chain: primary provider → backup provider → local Ollama → rule-based
- Per-call timeout; retries only for timeouts and rate limits
- Repair for malformed JSON (one retry with the validation error, then fall back)
- Logs tokens in/out, cost, latency, model and agent for every call into `llm_usage` and Langfuse
- **Token budgets** per run and per agent; a call that would exceed the budget falls back instead of running
- **Context builder** in `app/harness/`: assembles exactly what each agent sees — a compact state summary plus relevant memory, nothing else

**Done when:** a fake failure from the primary provider produces a successful answer from the backup, with both attempts logged, and an exhausted token budget triggers the fallback.

### B10. Agent workflow with LangGraph (L)

**Goal:** the four agents working as one graph.

- `WorkflowState` schema (event, observations, candidates, sim results, recommendation, approval, instruction, step count, errors)
- An **AgentSpec** (role, objective, allowed tools, inputs, outputs) for each of the four agents, in code and in `docs/agents.md`
- **Headway Monitor:** checks data freshness and confirms bunching
- **Action Generator:** LLM plus rules produce 2–4 concrete candidates, using past outcomes from `intervention_outcomes`
- **Passenger Impact Simulator:** fans out with `Send`, one branch per candidate plus "do nothing"
- **Control Advisor:** ranks results and writes the explanation; near-ties are escalated; "do nothing wins" is a valid answer
- Headway Monitor and Impact Simulator get all their data via MCP tools and the what-if model is built with `from_observations()` — no agent reads the database or the world simulator directly
- Explicit conditional edges: stale → flag and end; not bunched → end; all candidates worse than baseline → recommend "do nothing"; near-tie → escalate; partial simulation timeouts → rank the completed ones
- One active workflow per bus pair, with dedupe of repeated events
- Cooldown and anti-oscillation rules: no new instruction to the same bus within N minutes, no holding an already-held bus, reject fixes that create a new bunch behind
- A rulebook (`docs/rulebook.md`) whose hard rules are enforced as code constraints on candidates (e.g. max hold 180 s, no skips at listed hospital stops, no short-turns on the last two trips)
- A maximum step count, a whole-workflow time limit, and explicit end states
- Built rules-only first; the LLM is added once the graph runs end to end

**Done when:** one bunching event goes all the way through the graph to a ranked recommendation, and each step appears in `agent_steps`.

### B11. Counterfactual simulation workers (M)

**Goal:** fast, parallel, bounded what-if runs.

- ARQ (or Celery) jobs, one per candidate
- Fan-out = one LangGraph `Send` branch per candidate; each branch submits exactly one ARQ job and awaits it with a timeout (no nested parallelism)
- Job states (pending, running, completed, failed) stored in `simulation_jobs`
- Initialise the what-if model only from MCP observations, never from the world simulator's hidden state
- 3–5 random seeds per candidate, averaged, so the results aren't luck
- Confidence ranges across seeds (e.g. "saves 310 ± 60 passenger-minutes"), plus a safest-option ranking by a bad-case percentile (e.g. P90) alongside the mean; overlapping ranges count as a near-tie
- `asyncio.Semaphore` concurrency limit and a timeout per job; recommend from the jobs that finished if some time out

**Done when:** four candidates finish in parallel in under about 10 s, and a deliberately slow job times out without blocking the rest.

---

## Phase E: Human control

### B12. Approval, dispatch and driver feedback (M)

**Goal:** an officer-approved, safe, closed loop.

- LangGraph `interrupt()` before dispatch, with a Postgres checkpointer
- The full post-interrupt graph:
  - Approve → dispatch
  - Edit (e.g. change the hold seconds) → re-simulate → back to the officer
  - Reject with a reason → back to the Action Generator, at most 2 rounds
  - After dispatch → wait for the driver's acknowledgement → measure the outcome after ~15 simulated minutes → write `intervention_outcomes`, storing predicted AND actual benefit
- Approvals recorded with the officer's ID
- Recommendations expire (e.g. after 90 s) and are re-validated against fresh MCP data before sending
- Re-check staleness right before calling `send_driver_instruction` with a fresh idempotency key
- Push to the driver, then the driver's Acknowledge and Done; the world simulator applies the action
- Driver "can't comply" quick replies (blocking traffic / bus full / other) cancel the instruction, notify the officer and trigger a re-plan
- Passenger notifications for skip-stop and short-turn
- New migration: predicted benefit on `intervention_outcomes`, `expires_at` on `recommendations`, "can't comply" status and reason on `driver_instructions` (B3 follow-up)

**Done when:** you can restart the API while a run waits for approval, it resumes, and double-clicking Approve sends exactly one instruction.

---

## Phase F: Interfaces (React PWA)

### B13. Frontend shell and shared map (M)

**Goal:** the base every screen builds on.

- React + Vite + TypeScript, set up as a PWA
- React Router redirecting to the right page by role, plus login and register screens
- VazhiSync theme tokens (the neon palette)
- A shared `<TransitMap>` component: MapLibre with the CARTO Dark Matter basemap, the route line, stops, SDF bus icons recolored by status and rotated by heading, bus-number pills, a glow layer, and stale styling
- A WebSocket hook that keeps the map live

**Done when:** all three roles land on their own page showing the live map with moving bus icons.

### B14. Officer console (L)

**Goal:** the main decision screen.

- A single map with layered overlays: bunched bracket, pink hold ring, violet skip-stop segment, lime short-turn path, ghost buses
- Candidate chips at the top, and a floating recommendation card with the benefit and explanation, plus Approve / Edit / Reject
- Alert queue, instructions log, and a Simulation Lab panel

**Done when:** an officer can see a bunching event, compare the candidates on the map, and approve one, all from this screen.

### B15. Driver view (S–M)

**Goal:** a glanceable, safe screen for drivers.

- Map centred on the driver's bus, showing the gap to the buses ahead and behind
- One large instruction card with a sound or vibration alert and a countdown
- Acknowledge and Done buttons, plus a connection-lost warning
- "Can't comply" quick-reply buttons (blocking traffic / bus full / other)

**Done when:** an approved hold appears on the driver's phone within about 2 s, and acknowledging it updates the officer's screen.

### B16. Passenger app (M)

**Goal:** show the benefit to riders.

- Stop search, live arrival times, and crowding level per bus
- Service change alerts (skip-stop, short-turn)
- Favourite stops with arrival alerts; optional "I'm waiting here" check-in and feedback form

**Done when:** after a skip-stop is approved, a passenger whose favourite stop is affected gets the alert.

---

## Phase G: Hardening and proof

### B17. Reliability hardening (M)

**Goal:** every failure in the course brief has a tested response.

- A failure matrix document listing each failure, how it's detected, how the system responds, and the test that proves it: wrong tool chosen, invalid tool output, tool timeout, MCP disconnect, LLM timeout or rate limit, unavailable model, malformed response, all providers down, conflicting agent outputs, duplicate send, stale vehicle, failed background job
- A failure-injection toggle per scenario (kill a server, slow down a tool, return bad JSON)
- Clear status messages in the UI ("Running in degraded mode: demand data unavailable")

**Done when:** every row in the matrix has a passing automated test.

### B18. Observability and cost reporting (M)

**Goal:** everything is traceable and measurable.

- structlog JSON logs carrying a `run_id`, and Langfuse traces linking agents, tools and LLM calls
- Langfuse runs as Langfuse Cloud for development, or self-hosted behind the docker compose profile `observability`; the app's own `llm_usage` / `agent_steps` tables remain the source for reports
- Metrics: tool latency, workflow duration, success/failure rates, retry counts, tokens and cost per agent and per run
- A Runs page with a trace timeline, and a Metrics page
- `generate_report.py` produces the token/cost and failure report as Markdown or PDF

**Done when:** you can open any past run and follow every step it took, and the report generates in one command.

### B19. Evaluation harness (M)

**Goal:** proof that it works and helps.

- Unit tests (simulator, validators, routing logic) and integration tests (graph plus MCP plus database)
- The edge-case suite (golden scenarios plus failure matrix)
- An evaluation runner (`app/eval/`) that plays golden and generated scenarios through the whole system and scores: passenger-minutes saved vs "do nothing", unsafe or invalid instructions, predicted-vs-actual benefit error, and cost
- A/B evaluation: the same scenarios with no control vs with the advisor (auto-approve in evaluation mode), comparing headway variation, average wait and bunching count, with charts

**Done when:** you have a results table showing the improvement (or an honest explanation of where it didn't help).

### B20. Documentation and submission evidence (S)

**Goal:** everything the graders need.

- Final architecture diagram, README with setup steps, MCP Inspector screenshots, the cost report, test results and the evaluation charts
- A 5-minute demo script and a recorded video: normal running, bunching, recommendation, approval, driver acknowledgement, a failure injected and recovered, then the metrics

**Done when:** someone else can clone the repo, run it, and follow the demo.

---

## Phase H: Extensions

### B21. Early warning (M)

**Goal:** act before the bunch forms.

- Periodically fast-forward "do nothing" from MCP observations (`from_observations()` + `fast_forward`)
- If bunching is predicted within 10–15 minutes, raise a "predicted" bunching event that starts the workflow early
- Predicted events go through the same dedupe, cooldown and rulebook checks as actual ones
- New migration: a "predicted" kind on `bunching_events` (B3 follow-up)

**Done when:** a golden scenario triggers a predicted event before actual bunching, and the early action needs a smaller intervention than waiting.

### B22. Case retrieval (M)

**Goal:** learn from past interventions without text embeddings.

- pgvector extension (database image with pgvector, B1 follow-up) and a `cases` table in a new migration
- Each case = a normalised numeric feature vector (gap ratio, route position, time of day, weather, loads, next-stop demand) + the action + predicted and actual outcome
- Retrieval runs in the harness context builder; the top similar cases go to the Action Generator and are shown on the officer's recommendation card
- Lower the confidence when similar cases underperformed their predictions
- No text embeddings needed

**Done when:** a repeated scenario retrieves its earlier case and the recommendation card shows it.

### B23. Tamil language (S)

**Goal:** passenger and driver screens in Tamil.

- Tamil/English toggle (react-i18next) for the passenger and driver screens
- Driver instructions spoken aloud via the Web Speech API in Tamil, falling back to English or a sound alert if no Tamil voice is available

**Done when:** an approved hold is spoken in Tamil on a device with a Tamil voice.

### B24. Jev decision tier (OPTIONAL, build last) (M)

**Goal:** a fast, cheap classification tier in front of the LLM.

- Jev added to the LLM gateway behind a feature flag (`JEV_ENABLED`, off without `JEV_API_KEY`)
- Uses: picking which action types to simulate from a one-record summary (low confidence → LLM fallback), and classifying driver replies, passenger reports and officer reject reasons
- Jev never chooses the final action; the simulator's ranking does

**Done when:** with the flag on, the classifications work and low-confidence cases fall back to the LLM; with it off, nothing breaks.

---

## Stretch (not scheduled)

Not built unless explicitly asked for.

- Replay of past runs
- "Ask VazhiSync" officer chat (read-only tools, no LLM-written SQL)
- Learning ranking weights from officer feedback
- Weighted-priority modes
- Low-floor / accessibility information
- Settings page for thresholds
- Rulebook citations in explanations

---

## Course module coverage

| Course module | Covered in |
| --- | --- |
| 1. Agent and tool fundamentals | B7, B8, B9, B10, B24 |
| 2. MCP server and client | B7, B8 |
| 3. Multi-agent orchestration | B10, B11 |
| 4. State, memory, human control | B3, B10, B12, B21, B22 |
| 5. Async and background | B6, B11, B21 |
| 6. Reliability and failure handling | B8, B9, B11, B12, B17, B19 |
| 7. Logging, monitoring, cost | B9, B18, B19 |

## Critical path

Backbone, built in order: B1 → B2 → B4 → B6 → B7 → B8 → B10 → B11 → B12 → B17–B20.

Built alongside once the module they hang off works:
- B5 (scenarios) after B4
- B13–B16 (frontend) after B6; B23 (Tamil) after B15 and B16
- B9 (LLM gateway, harness part 2) feeds B10 — add it once the graph runs end to end on rules
- B21 (early warning) after B12, once the workflow can be started by an event
- B22 (case retrieval) after B9 (context builder) and B12 (outcomes with predicted vs actual)
- B24 (Jev, optional) last, before B20; nothing depends on it

Do B17–B20 last, but write the failure matrix early so failure handling is built in as you go rather than added at the end.
