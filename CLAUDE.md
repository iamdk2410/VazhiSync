# VazhiSync — Bus Bunching Control Advisor (CAT2-074)

VazhiSync detects bus bunching on one Chennai MTC route, simulates corrective actions
(hold, skip stop, short-turn, do nothing) and recommends the best one to a human officer,
who approves it before it is sent to the driver. The data is simulated; the route is real
(Chennai GTFS).

Full build plan: `docs/build-plan.md` (build modules B1–B24). Progress: `docs/progress.md`.
Design decisions: `docs/decisions.md`.

## Repo layout
```
vazhisync/
├── CLAUDE.md            this file — shared context and rules
├── docker-compose.yml   all services (postgres, redis, ollama, langfuse, api, worker, mcp servers, sim-runner, web)
├── .env.example
├── docs/                build-plan.md, progress.md, failure-matrix.md, decisions.md, agents.md, rulebook.md, architecture
├── backend/             Python: API, agents, simulator, MCP servers  → see backend/CLAUDE.md
└── frontend/            React + Vite + TypeScript PWA               → see frontend/CLAUDE.md (later)
```
Backend and frontend talk only through the HTTP API and the WebSocket. The frontend never
reads the database or calls MCP servers directly.

## Users (one app, three roles)
- **officer** — sees AI recommendations, approves/edits/rejects. Created by seed script only.
- **driver** — sees own bus + approved instructions, acknowledges. Created by seed script only.
- **passenger** — self-registers; sees arrivals, crowding, service alerts.

## Non-negotiable rules
1. **The advisor never sees the world simulator's hidden state.** Agents and what-if
   simulations only use what the MCP tools return. The world sim is ground truth; the
   what-if model is built with `from_observations(...)`.
2. **The simulator decides, the LLM explains.** No LLM computes passenger-minutes,
   headways or rankings. LLMs propose candidates and write explanations only.
3. **`send_driver_instruction` enforces its own guards** (do not rely on the agent):
   approved `approval_id` required, unique `instruction_id` (idempotency key),
   stale-GPS check at send time, action valid for the bus position.
4. **Stale vehicles are never instructed.** Stale = GPS age > `STALE_GPS_SECONDS`
   (config, default 60). Staleness is computed server-side in avl-mcp.
5. **Least privilege for tools.** Only the post-approval dispatch node may call
   `send_driver_instruction`.
6. **Every boundary is validated** — Pydantic in the backend (tool inputs/outputs, LLM
   outputs, API bodies, scenario files), typed API clients in the frontend.
7. **Retries are bounded** (backoff, transient errors only — timeouts, rate limits,
   connection errors). Never retry validation errors or permanent failures.
8. **Every workflow has limits**: max graph steps, whole-run time limit, per-simulation
   timeout, concurrency semaphore.
9. **Everything is logged with a `run_id`**: agent steps, routing decisions, tool calls,
   state transitions, LLM tokens + cost.
10. **Roles are enforced on the server** (`require_role`), never only in the frontend.
    `POST /auth/register` always creates a passenger.
11. **Deterministic simulation**: every run takes a seed; same seed → same result.
12. **Every tool call goes through the harness tool gate and its hooks**; risky actions are
    checked by both the harness and the MCP server.
13. **Hard operating rules (`docs/rulebook.md`) are code constraints**, never left to a model.
14. **Neither Jev nor the LLM chooses the final action**; the simulator's ranking does.
15. **Recommendations expire and are re-validated before sending**; cooldown rules prevent
    repeated instructions to the same bus.
16. **Unscheduled stretch items are not built** unless the user asks.

## How to work in this repo
- Build **one build module (B-number) at a time**, in the order in `docs/build-plan.md`.
  Backend: B1 → B3 → B2 → B4 → B6 → B5 → B7 → B8 → B10 (rules only) → B11 → B12 → B9 → B21 → B22
  → B17 → B18 → B19. Frontend: B13 → B14 → B15 → B16 → B23. Last: B24 (optional) → B20.
- Before coding a module: read its section in the build plan, propose a short plan, then implement.
- Each module's **"Done when"** line is its acceptance test — write a test for it.
- Run the lint, type and test commands for the part you changed before declaring a module done.
- After a module: tick it in `docs/progress.md` with a one-line note, and add any new
  failure cases to `docs/failure-matrix.md`.
- Never hard-code secrets; never commit `.env`.
- Don't add dependencies outside the listed stacks without saying why.
