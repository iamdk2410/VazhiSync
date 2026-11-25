# VazhiSync backend

Read the root `../CLAUDE.md` first: it has the project summary and the non-negotiable rules.
Everything Python lives in this folder: the API, the agents, the simulator and the MCP servers.

## Stack
- Python 3.12, `uv` for dependencies (one `pyproject.toml` in this folder), FastAPI, Pydantic v2, pydantic-settings
- Postgres + PostGIS + pgvector (case retrieval, B22), SQLAlchemy 2.0 (async, asyncpg), GeoAlchemy2, Alembic
- Redis (pub/sub + ARQ job queue)
- LangGraph (+ langgraph-checkpoint-postgres) for the agent workflow
- MCP: official `mcp` Python SDK v2 (`MCPServer`, streamable HTTP); client: the SDK's `mcp.Client`
  inside the harness (`langchain-mcp-adapters` is incompatible with SDK v2, see docs/decisions.md)
- LLMs: our own gateway (`app/llm/gateway.py`, decision 9): Groq and OpenRouter free tiers over
  their OpenAI-compatible API (httpx), local Ollama (`qwen3:8b`, thinking disabled for structured
  JSON) for scenario generation and as the last model tier, then rules; optional Jev tier (B24)
- Observability: structlog (JSON logs) + Langfuse (Langfuse Cloud, or self-hosted via the
  `observability` compose profile)
- Auth: PyJWT + pwdlib (argon2); rate limiting on public auth routes
- Tests: pytest, pytest-asyncio, httpx; lint: ruff; types: mypy

## Layout
```
backend/
├── pyproject.toml
├── alembic.ini
├── Dockerfile
├── simulator/      world model: step, snapshot, from_observations, fast_forward (pure Python, no I/O)
├── scenario_gen/   scenario schema, plausibility checks, prompt, director (LLM injected), preview,
│                   golden/ scenarios with expected outcomes (pure: never imports app/)
├── mcp_servers/
│   ├── avl_mcp/    get_vehicle_positions, get_headways, route://{id}/shape resource
│   ├── demand_mcp/ get_boarding_estimates, send_driver_instruction (guards in dispatch.py)
│   └── schemas.py  tool input/output models (shared with the B8 client), errors.py (JSON errors)
├── app/
│   ├── api/        FastAPI routers (auth, officer, driver, passenger, lab) + /ws/live websocket; schemas.py
│   ├── live/       live frame schemas + per-role filtering, headways, Redis channels, WebSocket hub
│   ├── auth/       password hashing + JWT (security), require_role (deps), rate limit, user service
│   ├── gtfs/       GTFS import: feed (validated rows), geometry, schedule, builder (validation), writer, snap_shape
│   ├── agents/     headway_monitor, action_generator, impact_simulator, control_advisor,
│   │               specs.py (AgentSpecs, docs/agents.md), rulebook.py (docs/rulebook.md)
│   ├── harness/    the only way agents call tools: specs.py (tools + per-agent allowlists),
│   │               client.py (MCP client, availability circuit), hooks.py (send guard),
│   │               budget.py (steps/time/tokens), harness.py, context.py (what each agent's
│   │               LLM call sees, B9)
│   ├── eval/       evaluation harness: plays scenarios through the system and scores them (B19)
│   ├── graph/      state.py (WorkflowState), deps.py (per-run deps, agent_steps recorder),
│   │               workflow.py (graph, start/resume claims, limits), checkpoint.py (Postgres
│   │               checkpointer, own `langgraph` schema), control.py (API side of resumes)
│   ├── workers/    ARQ jobs: scenario generation (B5), run_workflow + open-event sweep (B10);
│   │               simulations.py: simulation jobs on their own queue/worker (sim-worker, B11)
│   ├── llm/        gateway.py (fallback chain, repair, budgets, llm_usage), openai_compat.py
│   │               (Groq/OpenRouter), ollama.py (local tier, B5), langfuse.py (best-effort traces)
│   ├── lab/        Simulation Lab: scenario library (golden sync, route context)
│   ├── db/         SQLAlchemy models, migrations, seed scripts
│   ├── observability/
│   ├── runner/     live world sim-runner: world loop, GPS layer, bunching detector, route loader
│   ├── healthcheck.py  container health-check CLI (http / redis heartbeat)
│   └── config.py   pydantic-settings
├── docker/         compose support files (postgres-init SQL)
├── devtools/       dev-only pages (live_map.html: watch /ws/live on a map)
├── data/gtfs/      Chennai GTFS zip (not committed) + routes/<id>.json config and snapped GeoJSON (committed); see its README
└── tests/          unit/, integration/, edge_cases/
```
`simulator/` and `scenario_gen/` must not import from `app/` (they stay pure and reusable by the
world runner, the workers and the tests; enforced by `tests/unit/test_layering.py`).

## Config (`../.env`, loaded via pydantic-settings)
`DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, `STALE_GPS_SECONDS=60`,
`BUNCHING_RATIO=0.5`, `CLOSING_RATIO=0.75`, `SIM_TIMEOUT_SECONDS`, `SIM_CONCURRENCY=4`,
`MAX_GRAPH_STEPS`, `CORS_ORIGINS` (the frontend dev URL), Langfuse keys,
`JEV_ENABLED=false`, `JEV_API_KEY` (optional Jev tier, off without a key).
Live world (B6): `WORLD_ROUTE_ID`, `WORLD_SEED`, `WORLD_SPEED`, `WORLD_TICK_SECONDS`,
`WORLD_QUEUE_EVERY_SECONDS`, `WORLD_RETENTION_HOURS`, `WORLD_GPS_DROPOUTS` (JSON list).
LLM gateway (B9): `PRIMARY_MODEL`, `BACKUP_MODEL`, `FALLBACK_MODEL` (`<groq|openrouter>/<model>`),
`LOCAL_MODEL`, `OLLAMA_URL`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `LLM_ENABLED`,
`LLM_TIMEOUT_SECONDS`, `LLM_ATTEMPTS`, `LLM_MAX_OUTPUT_TOKENS`, `LLM_RATE_LIMIT_COOLDOWN_SECONDS`,
`LLM_RUN_TOKEN_BUDGET`, `LLM_AGENT_TOKEN_BUDGET`, `LLM_TIME_RESERVE_SECONDS`.

## Commands (run from `backend/`)
- Start services (from repo root): `docker compose up -d`
  (+ Langfuse: `docker compose --profile observability up -d`; Ollama runs natively on the host)
- Install: `uv sync`
- Migrations: `uv run alembic upgrade head`
- Run API locally: `uv run uvicorn app.main:app --reload`
- Tests: `uv run pytest` (fast unit tests: `uv run pytest tests/unit`).
  `services`-marked tests use Postgres + Redis from compose, on a separate `<db>_test`
  database that is created and migrated automatically (skipped if Postgres is down; CI sets
  `REQUIRE_SERVICES=1`). `stack` tests need the whole compose stack and are excluded by
  default: `uv run pytest -m stack`. `ollama` tests call the real local LLM (B5 acceptance):
  `uv run pytest -m ollama -s` (needs Ollama + qwen3:8b on the host and enough free RAM).
- Lint/format/types: `uv run ruff check . && uv run ruff format . && uv run mypy .`
- MCP servers: run in compose (avl-mcp :8101, demand-mcp :8102, endpoint `/mcp`, streamable HTTP).
  Inspect: `npx @modelcontextprotocol/inspector`, transport "Streamable HTTP",
  URL `http://localhost:8101/mcp` (or 8102). Locally without compose:
  `uv run python -m mcp_servers.avl_mcp.server`.
- Seed users (officer/driver only): `SEED_PASSWORD=... uv run python -m app.db.seed --role officer --email ... [--name ...]`
  (driver: add `--vehicle <fleet_number>`; inside compose: `docker compose exec -e SEED_PASSWORD=... api python -m app.db.seed ...`)
- Import the route (needs the GTFS zip, see data/gtfs/README.md): `uv run python -m app.gtfs.import_gtfs --route 32B [--dry-run]`
- Re-snap route geometry (one-off, calls the public OSRM server; per stop pair, honours
  `data/gtfs/routes/<id>.waypoints.json`, also writes `<id>.turns.geojson`):
  `uv run python -m app.gtfs.snap_shape --route 32B`, then re-import and regenerate the fixture
- Live map (dev): `uv run python -m http.server 5173 --directory devtools`, open
  `http://localhost:5173/live_map.html` and log in (the API must be running)
- Regenerate the simulator's 32B fixture after a re-import: `uv run python -m app.gtfs.sim_route --route 32B --out tests/fixtures/route_32b.json`
- New migration: `uv run alembic revision --autogenerate --rev-id 000N -m "..."`, review it, then `uv run alembic check`

## Conventions
- Small, typed, async functions; type hints everywhere; no bare `except`.
- API responses use Pydantic schemas that the frontend can generate types from (keep the
  OpenAPI schema clean: named models, no untyped dicts).
- Enable CORS only for `CORS_ORIGINS`.
