# VazhiSync — Design decisions

Short ADR-style records. Each entry: **Context** (the problem), **Decision** (what we chose),
**Why** (the reasons). Newest decisions are added at the end.

---

## 1. LangGraph over CrewAI

**Context:** the advisor is a decision workflow with four agents, a parallel what-if fan-out, a
human approval step that can wait minutes (and survive an API restart), and strict limits on
steps, time and cost.

**Decision:** build the workflow as a LangGraph `StateGraph`, with `Send` for the fan-out,
`interrupt()` for officer approval and the Postgres checkpointer for durability.

**Why:**
- A controlled decision workflow: edges and end states are explicit code, not emergent
  role-play between autonomous agents, so routing (stale → end, near-tie → escalate) is testable.
- A durable interrupt plus Postgres checkpointer lets a run wait for approval and resume after a
  restart — CrewAI has no equivalent built-in.
- Explicit cost: LLM calls happen only in the nodes that need them; most nodes are plain code.

## 2. The simulator decides, the LLM explains

**Context:** an LLM can sound confident about passenger-minutes and headways it cannot compute.

**Decision:** candidates are scored and ranked only by the what-if simulator. LLMs may propose
candidates and write explanations; they never compute metrics or rankings (root CLAUDE.md, rules
2 and 14).

**Why:** numbers are reproducible (seeded), testable and auditable; a wrong explanation is a
cosmetic bug, a wrong ranking would be a safety bug.

## 3. Rolling-horizon model predictive control as the framing

**Context:** we need a principled description of "observe, simulate options, act, repeat".

**Decision:** treat the advisor as rolling-horizon model predictive control: at each event, build
a model from current observations (`from_observations`), simulate each candidate action over a
short horizon (~15 min, several seeds), apply the best first action, then re-observe and re-plan.
Early warning (B21) uses the same loop with "do nothing" to look ahead.

**Why:** it matches how the system works, explains why the model never needs the hidden world
state, and makes "do nothing" and re-planning natural parts of the loop.

## 4. OSRM snap-once geometry

**Context:** the Chennai GTFS has no bus shapes; straight lines between stops don't follow roads.

**Decision:** snap the route to roads once with the public OSRM server, repair detours (segments
with road/straight ratio > 2.5), and commit the result as GeoJSON with OpenStreetMap attribution.
Imports and tests never call the network. (Per-stop-pair snapping and short-turn paths: B6.)

**Why:** reproducible offline builds and CI, no dependency on a public server at runtime, and the
geometry is reviewable in git.

## 5. Langfuse Cloud or a compose profile

**Context:** self-hosted Langfuse v3 needs ClickHouse, MinIO and two app containers (~3 GB RAM),
which strained the development machine.

**Decision:** use Langfuse Cloud for development, or the self-hosted stack behind the
`observability` docker compose profile (off by default). The app's own `llm_usage` and
`agent_steps` tables remain the source of truth for reports.

**Why:** traces are nice to have but not required for the system to run; reports must not depend
on an external service.

## 6. Case retrieval with numeric features in pgvector

**Context:** the Action Generator should learn from past interventions (B22).

**Decision:** store each case as a normalised numeric feature vector (gap ratio, route position,
time of day, weather, loads, next-stop demand) plus the action and predicted/actual outcome, in a
`cases` table using the pgvector extension in the existing Postgres. Retrieve by vector
similarity in the harness context builder.

**Why:** the situation is numeric, so numeric similarity is more meaningful than text embeddings;
no embedding model or extra service is needed; it stays in the same transactional database.

## 7. Jev as an optional fast tier only

**Context:** some steps are simple classifications (driver replies, reject reasons, which action
types are worth simulating) where a full LLM call is slow and costly.

**Decision:** add Jev to the LLM gateway behind a feature flag (`JEV_ENABLED`, off without an API
key), built last (B24). Low-confidence results fall back to the LLM. Jev never chooses the final
action.

**Why:** cheaper and faster where it fits, but the system must work fully without it, and the
final decision stays with the simulator's ranking (rule 14).

## 8. MCP client: the official SDK, not langchain-mcp-adapters

**Context:** the stack named `langchain-mcp-adapters` (`MultiServerMCPClient`) as the MCP
client. Its latest release (0.3.1) imports `mcp.shared.context.RequestContext`, which MCP SDK v2
removed; the B7 servers are built on SDK v2 (`MCPServer`). The package installs cleanly (it
doesn't pin `mcp<2`) and then fails on import.

**Decision:** the harness (`app/harness/client.py`) uses the official SDK client
(`mcp.Client`) over streamable HTTP directly, one session per call. Revisit if the adapters
add SDK v2 support and we need LangChain tool objects (e.g. LLM tool-calling in B10).

**Why:** downgrading to SDK v1 would mean rewriting the servers; the SDK client returns
structured results natively, which the harness validates against the same Pydantic models the
servers declare; and every call must pass through our own gate, hooks and budgets anyway,
which a generic tool wrapper would bypass.

## 9. LLM gateway: our own thin wrapper, free hosted tiers first

**Context:** B9 allows LiteLLM or our own wrapper. LiteLLM 1.110 pulls in boto3, tokenizers,
huggingface-hub, tiktoken, aiohttp and the openai SDK (much larger API/worker images), and its
PyPI package was compromised in March 2026 (1.82.7 / 1.82.8). The chosen providers (Groq and
OpenRouter) both serve the OpenAI-compatible `/chat/completions` API.

**Decision:** `app/llm/gateway.py` with one httpx client for the OpenAI-compatible tiers and
the existing Ollama client for the local tier; no new dependency. Chain: Groq
`openai/gpt-oss-120b` → Groq `qwen/qwen3.8-27b` (separate per-model quota) → OpenRouter
`google/gemma-4-31b-it:free` (different provider) → local `qwen3:8b` → rules. JSON mode plus the
schema in the prompt, validated with Pydantic and repaired once. A 429 moves to the next tier
at once and cools that tier down instead of waiting. Inside a run, each call is cut off at the
run's remaining time minus a reserve, so a slow model can't starve the simulations. The local
model is left out of live workflow runs by default (`LLM_LOCAL_IN_RUNS=false`): on this machine it
needs ~75 s per call, so when every hosted tier is rate-limited the rules answer at once; it
stays the model for scenario generation.

**Why:** fewer moving parts and a small supply-chain surface; timeouts, retries, budgets and
logging have to be ours anyway (rules 7–9); free tiers keep the project at zero cost, with
two independent providers and an offline model behind them.

## 10. AG-UI run stream: our own SSE endpoint, not ag-ui-langgraph

**Context:** the officer console should watch each workflow run as an AG-UI event stream (run
lifecycle, a step per agent node, state as candidates and simulation results arrive, the
explanation text, and the approval pause as an AG-UI interrupt resolved with approve / edit /
reject). Runs are started by the backend (sim-runner → ARQ worker), so the officer must be
able to attach to a run already in progress and rehydrate it. The official adapter
(`ag-ui-langgraph` 0.0.48) was checked: `LangGraphAgent.run()` always executes the graph itself
(`graph.astream_events`) in the process that holds the compiled graph; it has no attach,
observe or reconnect mode (its only non-executing path replays a stored interrupt). Our graph
runs in the worker, not the API, and the adapter also pulls in `langchain`.

**Decision:** the worker drives each segment with `graph.astream(stream_mode=["tasks",
"updates", "values"])` and turns it into `ag_ui.core` events (`ag-ui-protocol` 1.0.0, which only
depends on pydantic): `threadId` = the workflow run id (the LangGraph thread), `runId` = one
segment (the start or a resume); STEP_STARTED / STEP_FINISHED per node (simulate branches as
`simulate:<candidate>`); STATE_SNAPSHOT + STATE_DELTA (JSON Patch) of an officer-facing
`RunView`; the validated explanation as TEXT_MESSAGE_* (sent only after the number check,
rule 2); RUN_FINISHED with an interrupt outcome (id = the recommendation) for the officer
pause, a plain RUN_FINISHED for driver / outcome waits, RUN_ERROR for failures. Events go to a
Redis Stream per run, and the latest view plus the stream id it includes is written in the same
Lua call, so `GET /officer/runs/{id}/events` can send a consistent snapshot and then tail from
exactly that id (`Last-Event-ID` reconnects too). `POST /officer/runs/{id}/agui` takes an AG-UI
`RunAgentInput` whose `resume` resolves the interrupt through the same decision service as the
REST endpoint, and streams the resumed segment. Runs without a stream (older than 24 h) are
rebuilt from the database. Live positions, instructions and alerts stay on `/ws/live`.

**Why:** attach and rehydration are the core requirement and the adapter can't do them; one
small dependency instead of langchain; the stream survives API restarts and multiple officers
watching; publishing is best effort and can never break a run.

## 11. Skipped items and why

| Skipped | Why |
| --- | --- |
| GTFS-Realtime output feed | No consumer in scope; the app's own API and WebSocket serve all three roles. |
| Generic-document RAG | No document corpus drives decisions; operating rules are code constraints (rulebook) and past cases are numeric (decision 6). |
| Knowledge graphs | One route, a handful of entity types; relational tables already model it. |
| Separate vector database | pgvector in the existing Postgres is enough for a few thousand cases and avoids another service. |
| LLM-written SQL | Unbounded, hard to validate and a data-access risk; agents only use typed MCP tools and fixed queries. |
