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

## 8. Skipped items and why

| Skipped | Why |
| --- | --- |
| GTFS-Realtime output feed | No consumer in scope; the app's own API and WebSocket serve all three roles. |
| Generic-document RAG | No document corpus drives decisions; operating rules are code constraints (rulebook) and past cases are numeric (decision 6). |
| Knowledge graphs | One route, a handful of entity types; relational tables already model it. |
| Separate vector database | pgvector in the existing Postgres is enough for a few thousand cases and avoids another service. |
| LLM-written SQL | Unbounded, hard to validate and a data-access risk; agents only use typed MCP tools and fixed queries. |
